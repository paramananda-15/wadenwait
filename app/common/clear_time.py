"""
Physics-based Road Clearance Estimation (Torricelli Water-Balance Model).

Calculates the estimated time until a flooded road reaches the safe 'GO'
threshold for a given vehicle using a discrete water-balance simulation:
    dh/dt = rain_in(t) - drain_out(h)

where:
    rain_in(t)  = forecast rain intensity * runoff coefficient
    drain_out(h) = k * sqrt(h)   (Torricelli-like gravity drainage)

All coefficients are assumptions, documented in clear_time_config.py.
"""

import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from . import clear_time_config as cfg
from . import safety


# Weather cache per ~0.1 degree cell (approx 11 km). TTL: 12 minutes (720s).
WEATHER_CACHE: Dict[Tuple[float, float], Tuple[float, Dict[str, Any]]] = {}
WEATHER_CACHE_TTL = 720  # 12 minutes (between 10-15 minutes)

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def get_cell_key(lat: float, lng: float) -> Tuple[float, float]:
    """Returns a ~0.1 degree grid cell coordinate key."""
    return (round(float(lat), 1), round(float(lng), 1))


def fetch_weather_forecast(lat: float, lng: float) -> Tuple[Optional[Dict[str, Any]], bool]:
    """
    Fetch weather forecast from Open-Meteo with 24h past precipitation (past_days=1)
    and 24h hourly forecast.
    Cached per ~0.1 degree cell for 10-15 minutes.
    If the API fails, returns (None, False).
    """
    cell = get_cell_key(lat, lng)
    now = time.time()

    cached = WEATHER_CACHE.get(cell)
    if cached:
        cache_ts, data = cached
        if now - cache_ts < WEATHER_CACHE_TTL:
            return data, True

    params = urllib.parse.urlencode({
        "latitude": cell[0],
        "longitude": cell[1],
        "hourly": "precipitation,precipitation_probability",
        "past_days": 1,
        "forecast_days": 2,
        "timezone": "auto",
        "precipitation_unit": "mm",
    })
    url = f"{OPEN_METEO_FORECAST_URL}?{params}"

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "WadeOrWait-ClearTime/2.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            WEATHER_CACHE[cell] = (now, data)
            return data, True
    except Exception as exc:
        print(f"Weather API error for cell {cell}: {repr(exc)}")
        return None, False


def parse_weather_series(weather_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Extract past 24h precipitation sum and next 24h forecast series.
    """
    if not weather_data or "hourly" not in weather_data:
        return {
            "past_24h_precip_mm": 0.0,
            "forecast_hourly_mm": [],
            "forecast_probability": [],
            "forecast_available": False,
        }

    hourly = weather_data.get("hourly", {})
    times = hourly.get("time", [])
    precip = hourly.get("precipitation", [])
    prob = hourly.get("precipitation_probability", [])

    if not times or not precip:
        return {
            "past_24h_precip_mm": 0.0,
            "forecast_hourly_mm": [],
            "forecast_probability": [],
            "forecast_available": False,
        }

    # Because past_days=1, the first ~24 elements are the past 24 hours.
    # Find current hour index or default to index 24.
    cur_idx = 24 if len(precip) >= 48 else max(0, len(precip) // 2)

    past_slice = precip[:cur_idx]
    past_24h_mm = sum(float(p or 0.0) for p in past_slice)

    forecast_slice = [float(p or 0.0) for p in precip[cur_idx: cur_idx + 24]]
    prob_slice = [float(p or 0.0) for p in prob[cur_idx: cur_idx + 24]]

    return {
        "past_24h_precip_mm": round(past_24h_mm, 2),
        "forecast_hourly_mm": forecast_slice,
        "forecast_probability": prob_slice,
        "forecast_available": True,
    }


def get_vehicle_go_threshold(vehicle: str) -> float:
    """
    Target safe depth threshold (in cm) for the vehicle mode from safety.py.
    In safety.py, hi < limit * 0.8 is GO.
    """
    limit = safety.LIMITS.get(vehicle, 20.0)
    return float(limit * 0.8)


def get_road_coefficients(highway: Optional[str]) -> Tuple[float, float]:
    """
    Returns (runoff_coeff, base_k) based on highway class.
    """
    hw = (highway or "").lower()
    runoff = cfg.RUNOFF_COEFFICIENTS.get(hw, cfg.RUNOFF_COEFFICIENTS["default"])
    base_k = cfg.DRAIN_RATE_K.get(hw, cfg.DRAIN_RATE_K["default"])
    return runoff, base_k


def adjust_k_for_antecedent_rain(k: float, past_24h_mm: float) -> float:
    """
    Reduces drainage coefficient k when antecedent (past 24h) rain was heavy,
    simulating saturated catchments and overwhelmed storm sewers.
    """
    sat = cfg.SATURATED_SOIL_THRESHOLDS
    if past_24h_mm >= sat["very_heavy_past_rain_mm"]:
        return k * sat["severe_reduction_factor"]
    elif past_24h_mm >= sat["heavy_past_rain_mm"]:
        return k * sat["saturation_reduction_factor"]
    return k


def fit_observed_k(reports: List[Dict[str, Any]]) -> Optional[float]:
    """
    Fit Torricelli drain coefficient k if at least 2 reports within recent hours
    show falling water depth.
    dh/dt = - k * sqrt(h)  =>  k = (delta_h / delta_t) / sqrt(h_avg)
    Bounded to [MIN_K, MAX_K].
    """
    if not reports or len(reports) < 2:
        return None

    # Filter reports with valid depth and timestamp
    valid = []
    for r in reports:
        ts = r.get("captured_at") or r.get("created_at")
        lo = float(r.get("depth_lo", 0.0) or 0.0)
        hi = float(r.get("depth_hi", 0.0) or 0.0)
        if ts and hi > 0:
            valid.append({
                "ts": int(ts),
                "depth": (lo + hi) / 2.0,
            })

    if len(valid) < 2:
        return None

    # Sort ascending by timestamp
    valid.sort(key=lambda x: x["ts"])

    # Examine pairs within the last 6 hours
    now_ts = int(time.time())
    recent_cutoff = now_ts - (6 * 3600)
    recent = [v for v in valid if v["ts"] >= recent_cutoff]
    if len(recent) < 2:
        recent = valid[-2:]

    # Check for decreasing depth
    for i in range(len(recent) - 1):
        r1 = recent[i]
        r2 = recent[i + 1]
        dt_sec = r2["ts"] - r1["ts"]
        dh = r1["depth"] - r2["depth"]
        # Require at least 6 minutes apart and falling depth
        if dt_sec >= 360 and dh > 0.5:
            dt_hours = dt_sec / 3600.0
            h_avg = (r1["depth"] + r2["depth"]) / 2.0
            if h_avg > 0:
                k_raw = (dh / dt_hours) / math.sqrt(h_avg)
                min_k = cfg.OBSERVED_K_BOUNDS["min_k"]
                max_k = cfg.OBSERVED_K_BOUNDS["max_k"]
                k_bounded = max(min_k, min(max_k, k_raw))
                return round(k_bounded, 2)

    return None


def simulate_water_balance(
    h_init: float,
    k: float,
    runoff_coeff: float,
    hourly_rain_mm: List[float],
    h_target: float,
    buffer_min: int = cfg.TIME_BUFFER_MINUTES,
    horizon_hours: int = cfg.SIMULATION_HORIZON_HOURS,
    step_min: int = cfg.SIMULATION_STEP_MINUTES,
) -> Tuple[Optional[int], bool, List[float]]:
    """
    Simulates water depth h over time using 5-minute discrete stepping:
      dh = (rain_in - drain_out) * dt
    Returns:
      (clear_time_minutes, reached_target, depth_history)
    If initial depth is already <= h_target, returns (buffer_min, True, [h_init]).
    """
    dt_hours = step_min / 60.0
    total_steps = int((horizon_hours * 60) / step_min)

    h = max(0.0, float(h_init))
    depth_history = [round(h, 2)]

    if h <= h_target:
        return buffer_min, True, depth_history

    first_target_step = None

    for step in range(total_steps):
        t_hours = step * dt_hours
        hour_idx = int(t_hours)

        rain_intensity = (
            hourly_rain_mm[hour_idx]
            if hour_idx < len(hourly_rain_mm)
            else 0.0
        )
        # 1 mm rain = 0.1 cm depth contribution
        rain_in_rate = (rain_intensity * 0.1) * runoff_coeff
        drain_out_rate = k * math.sqrt(h) if h > 0 else 0.0

        dh = (rain_in_rate - drain_out_rate) * dt_hours
        h = max(0.0, h + dh)
        depth_history.append(round(h, 2))

        if h <= h_target:
            if first_target_step is None:
                first_target_step = step + 1
        else:
            # If rain caused water to rise back above target, reset
            first_target_step = None

    if first_target_step is not None:
        clear_minutes = (first_target_step * step_min) + buffer_min
        return clear_minutes, True, depth_history

    return None, False, depth_history


def detect_heavy_rain_status(
    hourly_rain_mm: List[float],
    horizon_check_hours: int = 4,
) -> Tuple[bool, Optional[int]]:
    """
    Detects if heavy rain is forecast to continue.
    Returns (is_heavy_continuing, rain_ease_hour_offset).
    """
    if not hourly_rain_mm:
        return False, None

    check_window = hourly_rain_mm[:horizon_check_hours]
    heavy_thresh = cfg.HEAVY_RAIN_THRESHOLD_MM_PER_H

    # Check if first few hours have heavy rain
    has_heavy_start = any(r >= heavy_thresh for r in check_window[:2])
    if not has_heavy_start:
        return False, None

    # Find when rain eases below 1.5 mm/h
    ease_hour = None
    for hr, r in enumerate(hourly_rain_mm):
        if r < 1.5:
            ease_hour = hr
            break

    return True, ease_hour


def format_clock_time(timestamp: int) -> str:
    """Format Unix timestamp into readable clock time e.g. '6:30 pm'."""
    dt = datetime.fromtimestamp(timestamp)
    # Windows strftime '%#I' removes leading zero for 12-hour clock
    try:
        hour_str = dt.strftime("%#I:%M %p").lower()
    except ValueError:
        hour_str = dt.strftime("%I:%M %p").lower().lstrip("0")
    return hour_str


def estimate_clear_time(
    lat: float,
    lng: float,
    depth_interval: Tuple[float, float],
    vehicle: str = "hatchback",
    highway: Optional[str] = "residential",
    recent_reports: Optional[List[Dict[str, Any]]] = None,
    mock_weather: Optional[Dict[str, Any]] = None,
    current_time: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Core entrypoint for physics-based clear time estimation.

    Returns:
    {
        "vehicle": str,
        "earliest": str,
        "likely": str,
        "latest": str,
        "earliest_ts": Optional[int],
        "likely_ts": Optional[int],
        "latest_ts": Optional[int],
        "basis": "observed" | "assumed",
        "drain_rate_label": "observed drain rate" | "assumed drain rate",
        "rain_note": Optional[str],
        "forecast_available": bool,
        "display_text": str,
    }
    """
    now_ts = current_time if current_time is not None else int(time.time())
    lo_depth, hi_depth = float(depth_interval[0]), float(depth_interval[1])
    if lo_depth > hi_depth:
        lo_depth, hi_depth = hi_depth, lo_depth

    h_target = get_vehicle_go_threshold(vehicle)
    runoff_coeff, base_k = get_road_coefficients(highway)

    # 1. Weather input
    if mock_weather is not None:
        weather_info = mock_weather
        forecast_avail = mock_weather.get("forecast_available", True)
    else:
        raw_weather, api_ok = fetch_weather_forecast(lat, lng)
        weather_info = parse_weather_series(raw_weather) if api_ok else {
            "past_24h_precip_mm": 0.0,
            "forecast_hourly_mm": [],
            "forecast_probability": [],
            "forecast_available": False,
        }
        forecast_avail = weather_info["forecast_available"]

    past_24h_mm = weather_info.get("past_24h_precip_mm", 0.0)
    forecast_rain = weather_info.get("forecast_hourly_mm", [])

    # 2. Drainage rate k determination
    observed_k = fit_observed_k(recent_reports or [])
    if observed_k is not None:
        drain_k = observed_k
        basis = "observed"
        rate_label = "observed drain rate"
    else:
        # Assumed k reduced by past 24h heavy rain if saturated
        drain_k = adjust_k_for_antecedent_rain(base_k, past_24h_mm)
        basis = "assumed"
        rate_label = "assumed drain rate"

    # Multipliers
    k_fast = drain_k * cfg.DRAIN_RATE_MULTIPLIERS["fast"]
    k_nominal = drain_k * cfg.DRAIN_RATE_MULTIPLIERS["nominal"]
    k_slow = drain_k * cfg.DRAIN_RATE_MULTIPLIERS["slow"]

    # Depths for interval
    h_low = lo_depth
    h_mid = (lo_depth + hi_depth) / 2.0
    h_high = hi_depth

    # Heavy rain detection
    is_heavy, ease_hr = detect_heavy_rain_status(forecast_rain)
    rain_note = None
    if is_heavy:
        if ease_hr is not None:
            ease_ts = now_ts + (ease_hr * 3600)
            rain_note = f"not before the rain eases (around {format_clock_time(ease_ts)})"
        else:
            rain_note = "not before the rain eases"

    # 3. Simulate scenarios
    if forecast_avail:
        # Normal simulation with forecast
        min_earliest, ok_earliest, _ = simulate_water_balance(
            h_low, k_fast, runoff_coeff, forecast_rain, h_target
        )
        min_likely, ok_likely, _ = simulate_water_balance(
            h_mid, k_nominal, runoff_coeff, forecast_rain, h_target
        )
        min_latest, ok_latest, _ = simulate_water_balance(
            h_high, k_slow, runoff_coeff, forecast_rain, h_target
        )
    else:
        # Forecast unavailable: wider interval scenario
        # Earliest assumes fast drain & zero rain
        min_earliest, ok_earliest, _ = simulate_water_balance(
            h_low, k_fast, runoff_coeff, [0.0] * 24, h_target
        )
        # Likely assumes nominal drain & light 1.0 mm/h rain
        min_likely, ok_likely, _ = simulate_water_balance(
            h_mid, k_nominal, runoff_coeff, [1.0] * 24, h_target
        )
        # Latest assumes slow drain & 3.5 mm/h rain for wider range
        min_latest, ok_latest, _ = simulate_water_balance(
            h_high, k_slow, runoff_coeff, [3.5] * 24, h_target
        )

    # If heavy rain continues, clamp earliest to not before rain eases
    if is_heavy and ease_hr is not None:
        ease_min = ease_hr * 60
        if min_earliest is not None:
            min_earliest = max(min_earliest, ease_min)
        if min_likely is not None:
            min_likely = max(min_likely, ease_min)
        if min_latest is not None:
            min_latest = max(min_latest, ease_min)

    # Construct timestamps and readable strings
    earliest_ts = (now_ts + (min_earliest * 60)) if (min_earliest is not None and ok_earliest) else None
    likely_ts = (now_ts + (min_likely * 60)) if (min_likely is not None and ok_likely) else None
    latest_ts = (now_ts + (min_latest * 60)) if (min_latest is not None and ok_latest) else None

    # Format human strings
    earliest_str = format_clock_time(earliest_ts) if earliest_ts else ("not safe within 24 hours" if not ok_earliest else None)
    likely_str = format_clock_time(likely_ts) if likely_ts else ("not safe within 24 hours" if not ok_likely else None)
    latest_str = format_clock_time(latest_ts) if latest_ts else ("not safe within 24 hours" if not ok_latest else None)

    # Generate display text (ensures never a single exact time)
    if is_heavy and (not ok_earliest or min_earliest is None):
        display_text = rain_note or "not before the rain eases"
    elif not ok_latest and not ok_likely:
        display_text = "not safe within 24 hours"
    elif earliest_str and latest_str:
        if earliest_str == latest_str:
            # Widen so it is never a single exact time
            w_early_ts = max(now_ts + 60, earliest_ts - 900)
            w_late_ts = latest_ts + 900
            display_text = f"about {format_clock_time(w_early_ts)}-{format_clock_time(w_late_ts)}"
        else:
            display_text = f"about {earliest_str}-{latest_str}"
    elif earliest_str:
        display_text = f"starting around {earliest_str}"
    else:
        display_text = "not safe within 24 hours"

    return {
        "vehicle": vehicle,
        "earliest": earliest_str,
        "likely": likely_str,
        "latest": latest_str,
        "earliest_ts": earliest_ts,
        "likely_ts": likely_ts,
        "latest_ts": latest_ts,
        "earliest_minutes": min_earliest,
        "likely_minutes": min_likely,
        "latest_minutes": min_latest,
        "basis": basis,
        "drain_rate_label": rate_label,
        "rain_note": rain_note,
        "forecast_available": forecast_avail,
        "display_text": display_text,
    }
