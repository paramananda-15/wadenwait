import json
import math
import time
import urllib.parse
import urllib.request
from typing import Optional, Dict, Any, Tuple, List

from . import clear_time as ct_module


OPEN_METEO_WEATHER = (
    "https://api.open-meteo.com/v1/forecast"
)

CACHE = {}
CACHE_TTL = 300


def _get_json(url):
    with urllib.request.urlopen(
        url,
        timeout=15
    ) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def _cached_json(url, ttl=CACHE_TTL):
    now = time.time()
    cached = CACHE.get(url)

    if cached:
        created_at, value = cached

        if now - created_at < ttl:
            return value

    value = _get_json(url)

    CACHE[url] = (
        now,
        value
    )

    return value


def distance_m(
    lat1,
    lng1,
    lat2,
    lng2
):
    earth_radius = 6371000.0

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dlat = math.radians(
        lat2 - lat1
    )

    dlng = math.radians(
        lng2 - lng1
    )

    a = (
        math.sin(dlat / 2) ** 2
        +
        math.cos(p1)
        *
        math.cos(p2)
        *
        math.sin(dlng / 2) ** 2
    )

    return (
        2
        *
        earth_radius
        *
        math.atan2(
            math.sqrt(a),
            math.sqrt(1 - a)
        )
    )


def get_weather(
    lat,
    lng
):
    params = urllib.parse.urlencode({
        "latitude":
            round(float(lat), 4),

        "longitude":
            round(float(lng), 4),

        "current":
            (
                "precipitation,"
                "rain,"
                "showers,"
                "temperature_2m,"
                "weather_code"
            ),

        "hourly":
            (
                "precipitation,"
                "rain,"
                "showers,"
                "precipitation_probability,"
                "weather_code"
            ),

        "forecast_hours":
            48,

        "timezone":
            "auto",

        "temperature_unit":
            "celsius",

        "precipitation_unit":
            "mm"
    })

    url = (
        OPEN_METEO_WEATHER
        +
        "?"
        +
        params
    )

    return _cached_json(
        url,
        ttl=300
    )


def estimate_reference_depth(
    rainfall_mm
):
    rainfall_mm = max(
        float(rainfall_mm or 0),
        0
    )

    if rainfall_mm <= 2:
        return 4.0

    if rainfall_mm <= 10:
        return 8.0

    if rainfall_mm <= 20:
        return 15.0

    if rainfall_mm <= 35:
        return 25.0

    if rainfall_mm <= 50:
        return 40.0

    if rainfall_mm <= 75:
        return 60.0

    return 85.0


def vehicle_threshold(mode):
    thresholds = {
        "pedestrian": 12.0,
        "two_wheeler": 42.0,
        "hatchback": 32.0,
        "suv": 60.0,
        "ambulance": 45.0,
    }

    return thresholds.get(
        mode,
        32.0,
    )


def vehicle_verdict(
    depth_cm,
    mode,
    rainfall_mm=0
):
    threshold = vehicle_threshold(mode)
    depth_cm = float(depth_cm)

    if mode == "two_wheeler":
        if depth_cm <= 14.0:
            return "GO"
        if depth_cm <= threshold:
            return "CAUTION"
        return "DO NOT CROSS"

    if mode == "suv":
        if depth_cm <= 42.0:
            return "GO"
        if depth_cm <= threshold:
            return "CAUTION"
        return "DO NOT CROSS"

    if depth_cm <= threshold * 0.65:
        return "GO"

    if depth_cm <= threshold:
        return "CAUTION"

    return "DO NOT CROSS"


def vehicle_risk(
    depth_cm,
    mode
):
    threshold = vehicle_threshold(mode)

    if threshold <= 0:
        return 1.0

    return min(
        max(
            float(depth_cm)
            /
            threshold,
            0.0
        ),
        1.0
    )


def _weather_is_raining(
    precipitation,
    probability
):
    precipitation = float(
        precipitation or 0
    )

    probability = float(
        probability or 0
    )

    return (
        precipitation >= 0.1
        or probability >= 50
    )


def _forecast_rain_duration(
    times,
    precipitation,
    probability
):
    """
    Estimate how long rain is likely to continue from now.

    Open-Meteo provides hourly forecast precipitation and
    precipitation probability. Therefore the demo reports
    the estimate in hourly increments (60-minute steps).
    """
    n = min(
        len(times),
        len(precipitation),
        len(probability)
    )

    if n == 0:
        return {
            "rain_minutes":
                0,

            "rain_end":
                None,

            "forecast_rain_mm":
                0.0
        }

    rain_minutes = 0
    forecast_rain = 0.0
    rain_end = None

    started = False

    for index in range(n):
        rain = float(
            precipitation[index] or 0
        )

        probability_value = float(
            probability[index] or 0
        )

        is_rain = _weather_is_raining(
            rain,
            probability_value
        )

        if not started:
            if is_rain:
                started = True
            else:
                # Skip forecast hours before the next
                # expected rain event.
                continue

        if not is_rain:
            break

        rain_minutes += 60
        forecast_rain += rain
        rain_end = times[index]

    return {
        "rain_minutes":
            rain_minutes,

        "rain_end":
            rain_end,

        "forecast_rain_mm":
            round(
                forecast_rain,
                1
            )
    }


def road_drainage_factor(
    road_type
):
    """
    Demo drainage calibration.

    Larger/major roads generally receive a slightly faster
    clearance estimate than small service/residential roads.
    This is a heuristic, not a hydraulic model.
    """
    factors = {
        "motorway": 1.20,
        "trunk": 1.15,
        "primary": 1.10,
        "secondary": 1.00,
        "tertiary": 0.95,
        "unclassified": 0.90,
        "residential": 0.85,
        "living_street": 0.80,
        "service": 0.72,
        "road": 0.85
    }

    return factors.get(
        road_type,
        0.85
    )


def estimate_road_clearance(
    lat,
    lng,
    current_depth_cm,
    mode,
    road_type="residential"
):
    """
    Estimate when a road becomes PASSABLE for the selected vehicle.

    Inputs:
      - current observed flood depth
      - current/future precipitation
      - road type
      - vehicle water-depth threshold

    The model simulates the depth hour by hour:
      next_depth =
          current_depth
          - drainage
          + rainfall contribution

    This is intentionally a hackathon estimation model.
    It is NOT an official flood prediction or hydraulic simulation.
    """
    weather = get_weather(
        lat,
        lng
    )

    current = weather.get(
        "current",
        {}
    )

    hourly = weather.get(
        "hourly",
        {}
    )

    times = hourly.get(
        "time",
        []
    )

    precipitation = hourly.get(
        "precipitation",
        []
    )

    probability = hourly.get(
        "precipitation_probability",
        []
    )

    rain_info = _forecast_rain_duration(
        times,
        precipitation,
        probability
    )

    depth = max(
        float(current_depth_cm),
        0.0
    )

    threshold = vehicle_threshold(
        mode
    )

    # The road is considered cleared when it reaches
    # the same PASSABLE threshold used by the map.
    passable_depth = (
        threshold * 0.65
    )

    drainage_factor = road_drainage_factor(
        road_type
    )

    # Base recession is deliberately conservative.
    base_recession_cm_per_hour = 1.5

    elapsed_minutes = 0
    rain_until_minutes = rain_info[
        "rain_minutes"
    ]

    rain_total_mm = 0.0
    clear_time = None
    depth_after_rain = depth

    # If it is already passable, it is effectively clear now.
    if depth <= passable_depth:
        return {
            "estimated_clear_minutes":
                0,

            "estimated_clear_time":
                "now",

            "rain_minutes":
                rain_until_minutes,

            "rain_end":
                rain_info["rain_end"],

            "forecast_rain_mm":
                rain_info["forecast_rain_mm"],

            "current_depth_cm":
                round(depth, 1),

            "passable_depth_cm":
                round(
                    passable_depth,
                    1
                ),

            "road_drainage_factor":
                drainage_factor,

            "weather":
                current,

            "confidence":
                "high"
        }

    n = min(
        len(times),
        len(precipitation),
        len(probability)
    )

    for index in range(n):
        rain = float(
            precipitation[index] or 0
        )

        probability_value = float(
            probability[index] or 0
        )

        is_rain = _weather_is_raining(
            rain,
            probability_value
        )

        # Drainage grows slightly with water depth.
        drainage = (
            base_recession_cm_per_hour
            +
            depth * 0.10
        ) * drainage_factor

        # Rainfall is converted to a local flood-depth
        # contribution using a demo runoff factor.
        rainfall_contribution = (
            rain * 0.80
        )

        if not is_rain:
            rainfall_contribution = 0.0

        depth = max(
            0.0,
            depth
            - drainage
            + rainfall_contribution
        )

        if is_rain:
            rain_total_mm += rain

        elapsed_minutes += 60

        depth_after_rain = depth

        if (
            clear_time is None
            and depth <= passable_depth
        ):
            clear_time = times[index]
            break

    if clear_time is None:
        # Continue simulating dry weather after the forecast
        # rain period. This prevents "unknown" for roads that
        # simply need several hours to drain.
        for _ in range(72):
            drainage = (
                base_recession_cm_per_hour
                + depth * 0.10
            ) * drainage_factor

            depth = max(
                0.0,
                depth - drainage
            )

            elapsed_minutes += 60

            if depth <= passable_depth:
                clear_time = (
                    f"+{elapsed_minutes} min"
                )
                break

    return {
        "estimated_clear_minutes":
            elapsed_minutes
            if clear_time is not None
            else None,

        "estimated_clear_time":
            clear_time,

        "rain_minutes":
            rain_until_minutes,

        "rain_end":
            rain_info["rain_end"],

        "forecast_rain_mm":
            round(
                max(
                    rain_info[
                        "forecast_rain_mm"
                    ],
                    rain_total_mm
                ),
                1
            ),

        "current_depth_cm":
            round(
                float(current_depth_cm),
                1
            ),

        "projected_depth_after_forecast_cm":
            round(
                depth_after_rain,
                1
            ),

        "passable_depth_cm":
            round(
                passable_depth,
                1
            ),

        "road_drainage_factor":
            drainage_factor,

        "weather":
            current,

        "confidence":
            "estimated",

        "clear_time":
            ct_module.estimate_clear_time(
                lat,
                lng,
                (float(current_depth_cm), float(current_depth_cm)),
                vehicle=mode,
                highway=road_type,
            ),
    }


get_clear_time = ct_module.estimate_clear_time


def forecast_reopening(
    lat,
    lng,
    current_depth=None
):
    weather = get_weather(
        lat,
        lng
    )

    current = weather.get(
        "current",
        {}
    )

    if current_depth is None:
        rainfall = float(
            current.get(
                "precipitation",
                0
            )
            or 0
        )

        current_depth = (
            estimate_reference_depth(
                rainfall
            )
        )

    modes = [
        "pedestrian",
        "two_wheeler",
        "hatchback",
        "suv",
        "ambulance"
    ]

    result = {}

    for mode in modes:
        result[mode] = estimate_road_clearance(
            lat,
            lng,
            current_depth,
            mode,
            "residential"
        )

    return {
        "current_depth_cm":
            round(
                float(current_depth),
                1
            ),

        "vehicles":
            result,

        "weather":
            current
    }
