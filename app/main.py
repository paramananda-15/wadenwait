import json
import os
import time
import uuid
import math
import urllib.parse
import urllib.request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI,
    File,
    Form,
    UploadFile,
    HTTPException,
    Request,
)

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .common import (
    db,
    geometry,
    perception,
    safety,
    environment,
    detector,
    fusion,
    clear_time,
    storage,
)
from .common.rate_limit import check_rate_limit

ROOT = (
    Path(__file__)
    .resolve()
    .parent
    .parent
)

UPLOADS = ROOT / "data" / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)

db.init()

app = FastAPI(
    title="Wade or Wait",
    version="2.0-road-flood"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MODES = [
    "pedestrian",
    "two_wheeler",
    "hatchback",
    "suv",
    "ambulance",
]

# -------------------------------------------------------------------
# OpenStreetMap / Overpass
# -------------------------------------------------------------------

OVERPASS_URL = os.getenv(
    "OVERPASS_URL",
    "https://overpass-api.de/api/interpreter",
)

ROAD_CACHE = {}
ROAD_CACHE_TTL = 300

ROAD_TYPES = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "service",
    "road",
}

REPORT_MATCH_RADIUS_M = 35.0


class ReportRequest(BaseModel):
    key: str
    lat: float
    lng: float
    accuracy: Optional[float] = None
    capturedAt: Optional[int] = None
    deviceId: Optional[str] = None
    mode: Optional[str] = "hatchback"
    vehicle: Optional[str] = None


class ConfirmRequest(BaseModel):
    status: str
    lat: float
    lng: float
    accuracy: float
    deviceId: Optional[str] = None
    device_id: Optional[str] = None


def haversine_m(lat1, lng1, lat2, lng2):
    earth_radius = 6371000.0

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dlng / 2) ** 2
    )

    return (
        2
        * earth_radius
        * math.atan2(
            math.sqrt(a),
            math.sqrt(1 - a),
        )
    )


def point_segment_distance_m(
    point_lat,
    point_lng,
    a_lat,
    a_lng,
    b_lat,
    b_lng,
):
    """
    Approximate point-to-segment distance using a local
    equirectangular projection. Accurate enough for a
    35 m road-matching radius.
    """
    lat0 = math.radians(point_lat)

    scale_x = 111320.0 * math.cos(lat0)
    scale_y = 111320.0

    px = (point_lng - point_lng) * scale_x
    py = (point_lat - point_lat) * scale_y

    ax = (a_lng - point_lng) * scale_x
    ay = (a_lat - point_lat) * scale_y

    bx = (b_lng - point_lng) * scale_x
    by = (b_lat - point_lat) * scale_y

    dx = bx - ax
    dy = by - ay

    length_sq = dx * dx + dy * dy

    if length_sq == 0:
        return math.sqrt(ax * ax + ay * ay)

    t = (
        ((px - ax) * dx + (py - ay) * dy)
        / length_sq
    )

    t = max(0.0, min(1.0, t))

    cx = ax + t * dx
    cy = ay + t * dy

    return math.sqrt(
        (px - cx) ** 2
        + (py - cy) ** 2
    )


def road_report_distance(road_geometry, report):
    if not road_geometry or len(road_geometry) < 2:
        return float("inf")

    best = float("inf")

    for index in range(len(road_geometry) - 1):
        a = road_geometry[index]
        b = road_geometry[index + 1]

        distance = point_segment_distance_m(
            report["lat"],
            report["lng"],
            a[1],
            a[0],
            b[1],
            b[0],
        )

        if distance < best:
            best = distance

    return best


def bbox_key(west, south, east, north):
    # Round to reduce duplicate Overpass calls while panning.
    return (
        round(west, 3),
        round(south, 3),
        round(east, 3),
        round(north, 3),
    )


def fetch_osm_roads(
    west,
    south,
    east,
    north,
):
    key = bbox_key(
        west,
        south,
        east,
        north,
    )

    now = time.time()

    cached = ROAD_CACHE.get(key)

    if cached:
        created_at, data = cached

        if now - created_at < ROAD_CACHE_TTL:
            return data

    query = f"""
    [out:json][timeout:20];
    way["highway"](
        {south},
        {west},
        {north},
        {east}
    );
    out tags geom;
    """

    encoded = urllib.parse.urlencode({
        "data": query
    })

    request = urllib.request.Request(
        OVERPASS_URL,
        data=encoded.encode("utf-8"),
        headers={
            "User-Agent":
                "WadeOrWait-Hackathon/1.0",
            "Content-Type":
                "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=25,
        ) as response:
            raw = response.read().decode(
                "utf-8"
            )

        payload = json.loads(raw)

    except Exception as exc:
        print(
            "Overpass error:",
            repr(exc)
        )
        return []

    roads = []

    for element in payload.get(
        "elements",
        [],
    ):
        tags = element.get(
            "tags",
            {}
        )

        highway = tags.get(
            "highway"
        )

        if highway not in ROAD_TYPES:
            continue

        geometry_points = []

        for point in element.get(
            "geometry",
            [],
        ):
            if (
                "lat" not in point
                or "lon" not in point
            ):
                continue

            geometry_points.append([
                point["lon"],
                point["lat"],
            ])

        if len(geometry_points) < 2:
            continue

        roads.append({
            "id":
                f"osm-{element['id']}",

            "name":
                tags.get(
                    "name",
                    "Unnamed road"
                ),

            "highway":
                highway,

            "geometry":
                geometry_points,
        })

    ROAD_CACHE[key] = (
        now,
        roads,
    )

    return roads


def classify_road(depth_cm, mode):
    threshold = environment.vehicle_threshold(
        mode
    )

    if depth_cm is None:
        return "UNKNOWN"

    depth_cm = float(depth_cm)

    if mode == "two_wheeler":
        if depth_cm <= 14.0:
            return "PASSABLE"
        if depth_cm <= threshold:
            return "CAUTION"
        if depth_cm <= threshold * 1.5:
            return "FLOODED"
        return "BLOCKED"

    if mode == "suv":
        if depth_cm <= 42.0:
            return "PASSABLE"
        if depth_cm <= threshold:
            return "CAUTION"
        if depth_cm <= threshold * 1.5:
            return "FLOODED"
        return "BLOCKED"

    if depth_cm <= threshold * 0.65:
        return "PASSABLE"

    if depth_cm <= threshold:
        return "CAUTION"

    if depth_cm <= threshold * 1.75:
        return "FLOODED"

    return "BLOCKED"


def build_road_features(
    roads,
    reports,
    mode,
):
    features = []

    for road in roads:
        matching = []

        road_lats = [
            point[1]
            for point
            in road["geometry"]
        ]

        road_lngs = [
            point[0]
            for point
            in road["geometry"]
        ]

        for report in reports:
            lat_delta = (
                REPORT_MATCH_RADIUS_M
                / 111320.0
            )

            cos_lat = max(
                math.cos(
                    math.radians(
                        report["lat"]
                    )
                ),
                0.2,
            )

            lng_delta = (
                REPORT_MATCH_RADIUS_M
                / (
                    111320.0
                    * cos_lat
                )
            )

            if (
                report["lat"]
                < min(road_lats) - lat_delta
                or
                report["lat"]
                > max(road_lats) + lat_delta
                or
                report["lng"]
                < min(road_lngs) - lng_delta
                or
                report["lng"]
                > max(road_lngs) + lng_delta
            ):
                continue

            distance = road_report_distance(
                road["geometry"],
                report,
            )

            if distance <= REPORT_MATCH_RADIUS_M:
                matching.append(
                    (
                        distance,
                        report,
                    )
                )

        properties = {
            "road_id":
                road["id"],

            "name":
                road["name"],

            "highway":
                road["highway"],

            "status":
                "UNKNOWN",

            "status_label":
                "Unknown",

            "is_stale":
                False,

            "depth_cm":
                None,

            "reports":
                0,

            "confidence":
                0,

            "last_reported":
                None,

            "weather":
                None,

            "rain_minutes":
                None,

            "rain_end":
                None,

            "forecast_rain_mm":
                None,

            "clear_in_minutes":
                None,

            "clear_time":
                None,

            "passable_depth_cm":
                None,

            "clearance_confidence":
                None,

            "last_verified":
                None,

            "last_verified_minutes":
                None,

            "opacity":
                0.35,

            "report_id":
                None,
        }

        if matching:
            matching.sort(
                key=lambda item:
                    item[0]
            )

            active_matches = [m for m in matching if not m[1].get("cleared")]
            cleared_matches = [m for m in matching if m[1].get("cleared")]
            usable_active = [
                m for m in active_matches
                if (
                    bool(m[1].get("image_usable"))
                    or float(m[1].get("confidence", 0.0)) > 0.0
                    or float(m[1].get("depth_hi", 0.0)) > 0.0
                )
            ]

            now_ts = int(time.time())
            stale_threshold_sec = int(os.getenv("STALE_THRESHOLD_SECONDS", 3600))
            clearance = None

            if usable_active:
                latest_item = max(
                    usable_active,
                    key=lambda item: item[1].get("captured_at") or 0,
                )
                max_depth = (
                    float(latest_item[1]["depth_lo"])
                    + float(latest_item[1]["depth_hi"])
                ) / 2.0

                confidence = max(
                    float(
                        item[1]["confidence"]
                    )
                    for item
                    in usable_active
                )

                timestamps = [
                    item[1]["captured_at"]
                    for item
                    in usable_active
                    if item[1]["captured_at"]
                    is not None
                ]

                closest_report = usable_active[0][1]

                last_verified_ts = max(
                    (item[1].get("last_verified") or item[1].get("captured_at") or now_ts)
                    for item in usable_active
                )
                age_sec = max(0, now_ts - last_verified_ts)
                age_min = round(age_sec / 60)
                fade = max(0.35, min(1.0, 1.0 - (age_sec / (180.0 * 60.0)) * 0.65))

                is_stale = age_sec >= stale_threshold_sec
                classified_status = classify_road(
                    max_depth,
                    mode,
                )

                if is_stale:
                    road_status = "STALE"
                    status_label = "Stale - verify before crossing"
                else:
                    road_status = classified_status
                    status_label = classified_status

                try:
                    clearance = (
                        environment
                        .estimate_road_clearance(
                            closest_report["lat"],
                            closest_report["lng"],
                            max_depth,
                            mode,
                            road["highway"],
                        )
                    )
                except Exception as exc:
                    print(
                        "Road weather/clearance error:",
                        road["id"],
                        repr(exc),
                    )
                    clearance = None

                clear_time_data = None
                if road_status not in ("PASSABLE", "REPORTED_CLEAR", "UNKNOWN"):
                    try:
                        recent_reps = db.get_recent_reports_for_road_or_cell(
                            road_id=road["id"],
                            lat=closest_report["lat"],
                            lng=closest_report["lng"],
                        )
                        physics_ct = clear_time.estimate_clear_time(
                            closest_report["lat"],
                            closest_report["lng"],
                            (
                                float(closest_report.get("depth_lo", max_depth)),
                                float(closest_report.get("depth_hi", max_depth)),
                            ),
                            vehicle=mode,
                            highway=road.get("highway", "residential"),
                            recent_reports=recent_reps,
                        )
                        db.log_clear_time(
                            road_id=road["id"],
                            lat=closest_report["lat"],
                            lng=closest_report["lng"],
                            vehicle=mode,
                            issued_at=now_ts,
                            earliest=physics_ct["earliest"],
                            likely=physics_ct["likely"],
                            latest=physics_ct["latest"],
                            basis=physics_ct["basis"],
                            forecast_available=physics_ct["forecast_available"],
                            earliest_ts=physics_ct.get("earliest_ts"),
                            likely_ts=physics_ct.get("likely_ts"),
                            latest_ts=physics_ct.get("latest_ts"),
                        )
                        clear_time_data = {
                            "vehicle": physics_ct["vehicle"],
                            "earliest": physics_ct["earliest"],
                            "likely": physics_ct["likely"],
                            "latest": physics_ct["latest"],
                            "basis": physics_ct["basis"],
                            "rain_note": physics_ct["rain_note"],
                            "forecast_available": physics_ct["forecast_available"],
                            "display_text": physics_ct["display_text"],
                        }
                    except Exception as ct_exc:
                        print("Error estimating physics clear time:", repr(ct_exc))
                        clear_time_data = None

                properties.update({
                    "status":
                        road_status,

                    "status_label":
                        status_label,

                    "is_stale":
                        is_stale,

                    "flood_status":
                        classified_status,

                    "depth_cm":
                        round(
                            max_depth,
                            1,
                        ),

                    "reports":
                        len(matching),

                    "confidence":
                        round(
                            confidence,
                            2,
                        ),

                    "last_reported":
                        max(timestamps)
                        if timestamps
                        else None,

                    "last_verified":
                        last_verified_ts,

                    "last_verified_minutes":
                        age_min,

                    "opacity":
                        round(fade, 2),

                    "report_id":
                        closest_report.get("id"),
                })

            elif cleared_matches:
                closest_report = cleared_matches[0][1]

                confidence = max(
                    float(
                        item[1]["confidence"]
                    )
                    for item
                    in cleared_matches
                )

                timestamps = [
                    item[1]["captured_at"]
                    for item
                    in cleared_matches
                    if item[1]["captured_at"]
                    is not None
                ]

                last_verified_ts = max(
                    (item[1].get("last_verified") or item[1].get("captured_at") or now_ts)
                    for item in cleared_matches
                )
                age_sec = max(0, now_ts - last_verified_ts)
                age_min = round(age_sec / 60)

                properties.update({
                    "status":
                        "REPORTED_CLEAR",

                    "status_label":
                        "Reported clear (unverified)",

                    "is_stale":
                        False,

                    "flood_status":
                        "PASSABLE",

                    "depth_cm":
                        0.0,

                    "reports":
                        len(matching),

                    "confidence":
                        round(
                            confidence,
                            2,
                        ),

                    "last_reported":
                        max(timestamps)
                        if timestamps
                        else None,

                    "last_verified":
                        last_verified_ts,

                    "last_verified_minutes":
                        age_min,

                    "opacity":
                        0.9,

                    "report_id":
                        closest_report.get("id"),
                })

            if clearance:
                current_weather = (
                    clearance.get(
                        "weather",
                        {}
                    )
                    or {}
                )

                properties.update({
                    "weather": {
                        "temperature_c":
                            current_weather.get(
                                "temperature_2m"
                            ),

                        "precipitation_mm":
                            current_weather.get(
                                "precipitation"
                            ),

                        "rain_mm":
                            current_weather.get(
                                "rain"
                            ),

                        "showers_mm":
                            current_weather.get(
                                "showers"
                            ),

                        "weather_code":
                            current_weather.get(
                                "weather_code"
                            ),
                    },

                    "rain_minutes":
                        clearance.get(
                            "rain_minutes"
                        ),

                    "rain_end":
                        clearance.get(
                            "rain_end"
                        ),

                    "forecast_rain_mm":
                        clearance.get(
                            "forecast_rain_mm"
                        ),

                    "clear_in_minutes":
                        clearance.get(
                            "estimated_clear_minutes"
                        ),

                    "clear_time":
                        clear_time_data,

                    "passable_depth_cm":
                        clearance.get(
                            "passable_depth_cm"
                        ),

                    "clearance_confidence":
                        clearance.get(
                            "confidence"
                        ),
                })

        features.append({
            "type":
                "Feature",

            "geometry": {
                "type":
                    "LineString",

                "coordinates":
                    road["geometry"],
            },

            "properties":
                properties,
        })

    return features


# -------------------------------------------------------------------
# Core API
# -------------------------------------------------------------------

@app.get("/health")
def health_check():
    return {"status": "ok", "healthy": True}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "perception_mode":
            os.getenv(
                "PERCEPTION_MODE",
                "fusion",
            ),
        "ollama_model":
            os.getenv(
                "OLLAMA_MODEL",
                "qwen2.5vl:7b",
            ),
        "map_mode":
            "road-flood",
        "storage_backend":
            storage.get_backend(),
        "db_backend":
            "dynamodb" if db.is_dynamo() else "sqlite",
    }


@app.post("/upload-url")
def upload_url():
    key = storage.generate_object_key("photos", "jpg")

    return {
        "url":
            storage.get_upload_url(key),

        "key":
            key,
    }


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    key: Optional[str] = Form(None),
):
    if not key:
        raise HTTPException(
            status_code=400,
            detail="missing upload key",
        )

    if not key.startswith("photos/"):
        raise HTTPException(
            status_code=400,
            detail="invalid upload key",
        )

    data = await file.read()

    max_size = (
        8
        * 1024
        * 1024
    )

    if len(data) > max_size:
        raise HTTPException(
            status_code=413,
            detail="image too large",
        )

    if len(data) == 0:
        raise HTTPException(
            status_code=400,
            detail="empty image",
        )

    filename = Path(key).name
    saved_key = storage.save_file(key, data, content_type="image/jpeg")

    return {
        "ok": True,
        "key": saved_key,
        "filename": filename,
        "size": len(data),
    }


@app.post("/reports")
def report(req: ReportRequest):
    filename = Path(req.key).name
    try:
        image = storage.get_file(req.key)
    except FileNotFoundError:
        try:
            image = storage.get_file(f"photos/{filename}")
        except FileNotFoundError:
            raise HTTPException(
                status_code=404,
                detail="photo not found",
            )

    mode_env = os.getenv("PERCEPTION_MODE", "fusion").strip().lower()

    if mode_env == "fusion":
        obs = perception.observe(image)
        det_raw = detector.detect(image)
        det_est = detector.estimate_depth_visible_height(det_raw)

        adj_vlm_conf, _ = detector.cross_check_vlm(
            obs.get("objects", []),
            det_raw.get("detections", []),
            obs.get("overall_confidence", 0.0),
        )

        if obs.get("image_usable"):
            est = geometry.estimate(obs["objects"], adj_vlm_conf)
            vlm_interval = [est["depth_lo"], est["depth_hi"]] if est else None
            vlm_conf = est["conf"] if est else 0.0
        else:
            est = None
            vlm_interval = None
            vlm_conf = 0.0

        det_interval = det_est.get("depth_interval")
        det_conf = det_est.get("confidence", 0.0)

        fused = fusion.fuse(
            vlm_interval,
            vlm_conf,
            det_interval,
            det_conf,
            modes=MODES,
            flow=obs.get("flow", "still"),
        )

        exp_filename = f"explanation_{filename}"
        exp_key = f"photos/{exp_filename}"
        exp_path = UPLOADS / exp_filename
        _, reason = detector.create_explanation_image(
            image,
            det_est,
            save_path=exp_path,
        )
        if storage.get_backend() == "s3" and exp_path.exists():
            storage.save_file(exp_key, exp_path.read_bytes(), content_type="image/jpeg")
            explanation_url = storage.get_url(exp_key)
        else:
            explanation_url = f"/uploads/{exp_filename}"

        has_depth = fused["fused_interval"] is not None
        depth_cm = fused["fused_interval"] if has_depth else [0, 0]
        band = (
            geometry.band((depth_cm[0] + depth_cm[1]) / 2)
            if has_depth
            else "unknown"
        )

        result = {
            "image_usable": bool(has_depth),
            "band": band,
            "depth_cm": depth_cm,
            "confidence": fused["fused_confidence"],
            "verdicts": fused["verdicts"],
            "flow": obs.get("flow", "still"),
            "reports": 1,
            "perception_mode": "fusion",
            "perception_model": obs.get("model"),
            "perception_reasons": obs.get("reasons_if_unusable", []),
            "vlm_interval": fused["vlm_interval"],
            "detector_interval": fused["detector_interval"],
            "agreement": fused["agreement"],
            "explanation_image_url": explanation_url,
            "explanation_reason": reason,
        }

    else:
        # Default VLM mode: existing behavior unchanged
        obs = perception.observe(image)

        print(
            "Perception mode:",
            obs.get("mode"),
            "model:",
            obs.get("model"),
        )

        if obs["image_usable"]:
            est = geometry.estimate(
                obs["objects"],
                obs["overall_confidence"],
            )
        else:
            est = None

        if not est:
            # Fallback to detector if VLM is unavailable or produced no estimate
            det_raw = detector.detect(image)
            det_est = detector.estimate_depth_visible_height(det_raw)
            if det_est.get("depth_interval"):
                fused = fusion.fuse(
                    None,
                    0.0,
                    det_est.get("depth_interval"),
                    det_est.get("confidence", 0.0),
                    modes=MODES,
                    flow=obs.get("flow", "still"),
                )
                exp_filename = f"explanation_{filename}"
                exp_key = f"photos/{exp_filename}"
                exp_path = UPLOADS / exp_filename
                _, reason = detector.create_explanation_image(
                    image,
                    det_est,
                    save_path=exp_path,
                )
                if storage.get_backend() == "s3" and exp_path.exists():
                    storage.save_file(exp_key, exp_path.read_bytes(), content_type="image/jpeg")
                    explanation_url = storage.get_url(exp_key)
                else:
                    explanation_url = f"/uploads/{exp_filename}"
                depth_cm = fused["fused_interval"]
                band = geometry.band((depth_cm[0] + depth_cm[1]) / 2)
                result = {
                    "image_usable": True,
                    "band": band,
                    "depth_cm": depth_cm,
                    "confidence": fused["fused_confidence"],
                    "verdicts": fused["verdicts"],
                    "flow": obs.get("flow", "still"),
                    "reports": 1,
                    "perception_mode": "fusion_fallback",
                    "perception_model": "detector",
                    "perception_reasons": obs.get("reasons_if_unusable", []),
                    "vlm_interval": None,
                    "detector_interval": fused["detector_interval"],
                    "agreement": "detector_unavailable",
                    "explanation_image_url": explanation_url,
                    "explanation_reason": reason,
                }
            else:
                result = {
                    "image_usable":
                        False,

                    "band":
                        "unknown",

                    "depth_cm":
                        [0, 0],

                    "confidence":
                        0,

                    "verdicts": {
                        mode:
                            "UNKNOWN"
                        for mode in MODES
                    },

                    "flow":
                        obs.get(
                            "flow",
                            "still",
                        ),

                    "reports":
                        1,

                    "perception_mode":
                        obs.get("mode", "vlm"),

                    "perception_model":
                        obs.get("model"),

                    "perception_reasons":
                        obs.get(
                            "reasons_if_unusable",
                            [],
                        ),

                    "vlm_interval":
                        None,

                    "detector_interval":
                        None,

                    "agreement":
                        "detector_unavailable",

                    "explanation_image_url":
                        None,

                    "explanation_reason":
                        None,
                }

        else:
            verdicts = {}

            for mode in MODES:
                verdicts[mode] = safety.verdict(
                    est["depth_lo"],
                    est["depth_hi"],
                    est["conf"],
                    mode,
                    obs.get(
                        "flow",
                        "still",
                    ),
                )

            result = {
                "image_usable":
                    True,

                "band":
                    geometry.band(
                        (
                            est["depth_lo"]
                            + est["depth_hi"]
                        )
                        / 2
                    ),

                "depth_cm":
                    [
                        est["depth_lo"],
                        est["depth_hi"],
                    ],

                "confidence":
                    est["conf"],

                "verdicts":
                    verdicts,

                "flow":
                    obs.get(
                        "flow",
                        "still",
                    ),

                "reports":
                    1,

                "perception_mode":
                    obs.get("mode", "vlm"),

                "perception_model":
                    obs.get("model"),

                "perception_reasons":
                    obs.get(
                        "reasons_if_unusable",
                        [],
                    ),

                "vlm_interval":
                    [
                        est["depth_lo"],
                        est["depth_hi"],
                    ],

                "detector_interval":
                    None,

                "agreement":
                    "detector_unavailable",

                "explanation_image_url":
                    None,

                "explanation_reason":
                    None,
            }

    db.insert({
        "key":
            req.key,

        "lat":
            req.lat,

        "lng":
            req.lng,

        "accuracy":
            req.accuracy,

        "capturedAt":
            (
                req.capturedAt
                or int(time.time())
            ),

        "deviceId":
            req.deviceId,

        "depth_lo":
            result["depth_cm"][0],

        "depth_hi":
            result["depth_cm"][1],

        "confidence":
            result["confidence"],

        "band":
            result["band"],

        "flow":
            result["flow"],

        "verdicts":
            json.dumps(
                result["verdicts"]
            ),

        "image_usable":
            result["image_usable"],
    })

    veh = req.vehicle or req.mode or "hatchback"
    if veh not in MODES:
        veh = "hatchback"

    depth_lo = float(result["depth_cm"][0]) if result.get("depth_cm") else 0.0
    depth_hi = float(result["depth_cm"][1]) if result.get("depth_cm") else 0.0

    recent_reps = db.get_recent_reports_for_road_or_cell(lat=req.lat, lng=req.lng)
    ct = clear_time.estimate_clear_time(
        lat=req.lat,
        lng=req.lng,
        depth_interval=(depth_lo, depth_hi),
        vehicle=veh,
        highway="residential",
        recent_reports=recent_reps,
    )
    result["clear_time"] = {
        "vehicle": ct["vehicle"],
        "earliest": ct["earliest"],
        "likely": ct["likely"],
        "latest": ct["latest"],
        "basis": ct["basis"],
        "rain_note": ct["rain_note"],
        "forecast_available": ct["forecast_available"],
        "display_text": ct["display_text"],
    }
    db.log_clear_time(
        road_id=None,
        lat=req.lat,
        lng=req.lng,
        vehicle=veh,
        issued_at=int(time.time()),
        earliest=ct["earliest"],
        likely=ct["likely"],
        latest=ct["latest"],
        basis=ct["basis"],
        forecast_available=ct["forecast_available"],
        earliest_ts=ct.get("earliest_ts"),
        likely_ts=ct.get("likely_ts"),
        latest_ts=ct.get("latest_ts"),
    )

    print(
        "Analysis complete:",
        result,
    )
    print(
        "============================"
    )
    print()

    return result


@app.post("/reports/{id}/confirm")
def confirm_report(id: str, req: ConfirmRequest, request: Request):
    if req.status not in ("still_flooded", "cleared"):
        raise HTTPException(
            status_code=400,
            detail="status must be 'still_flooded' or 'cleared'",
        )

    device_id = req.deviceId or req.device_id or "unknown_device"
    client_ip = request.headers.get("x-forwarded-for")
    if client_ip:
        client_ip = client_ip.split(",")[0].strip()
    elif request.client:
        client_ip = request.client.host
    else:
        client_ip = "127.0.0.1"

    # Per-device and per-IP rate limits
    check_rate_limit(device_id, client_ip)

    rep = db.get_report(id)
    if not rep:
        raise HTTPException(
            status_code=404,
            detail="report not found",
        )

    # Reject votes from devices more than 200 m from the report location
    dist = haversine_m(req.lat, req.lng, rep["lat"], rep["lng"])
    if dist > 200.0:
        raise HTTPException(
            status_code=400,
            detail=f"Device location is too far from report ({round(dist, 1)}m > 200m).",
        )

    res = db.add_vote(id, device_id, req.status)
    return res


@app.get("/map")
def map_pins(
    bbox: str,
):
    """
    Keeps the old report API available.
    The new frontend does not use this for map rendering.
    """
    try:
        west, south, east, north = map(
            float,
            bbox.split(","),
        )
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=(
                "bbox must be "
                "west,south,east,north"
            ),
        )

    rows = db.nearby(
        (
            west,
            south,
            east,
            north,
        ),
        include_cleared=True,
    )

    groups = {}

    for row in rows:
        cell = (
            round(row["lat"], 3),
            round(row["lng"], 3),
        )

        groups.setdefault(
            cell,
            [],
        ).append(row)

    pins = []

    for (
        lat,
        lng,
    ), rows_in_group in groups.items():
        active_rows = [r for r in rows_in_group if not bool(r["cleared"] if "cleared" in r.keys() and r["cleared"] else False)]
        cleared_rows = [r for r in rows_in_group if bool(r["cleared"] if "cleared" in r.keys() and r["cleared"] else False)]

        target_rows = active_rows if active_rows else cleared_rows

        lo = min(
            row["depth_lo"]
            for row
            in target_rows
        )

        hi = max(
            row["depth_hi"]
            for row
            in target_rows
        )

        confidence = (
            sum(
                row["confidence"]
                for row
                in target_rows
            )
            / len(target_rows)
        )

        flow = (
            target_rows[0]["flow"]
            if "flow"
            in target_rows[0].keys()
            else "still"
        )

        verdicts = {}

        for mode in MODES:
            verdicts[mode] = safety.verdict(
                lo,
                hi,
                confidence,
                mode,
                flow,
            )

        now_ts = int(time.time())
        stale_threshold_sec = int(os.getenv("STALE_THRESHOLD_SECONDS", 3600))

        last_verified_ts = max(
            (row["last_verified"] if "last_verified" in row.keys() and row["last_verified"] else (row["created_at"] if "created_at" in row.keys() and row["created_at"] else now_ts))
            for row in target_rows
        )
        age_sec = max(0, now_ts - last_verified_ts)
        age_min = round(age_sec / 60)
        is_stale = bool(active_rows) and (age_sec >= stale_threshold_sec)

        if not active_rows and cleared_rows:
            pin_status = "REPORTED_CLEAR"
            pin_status_label = "Reported clear (unverified)"
            fade = 0.9
        elif is_stale:
            pin_status = "STALE"
            pin_status_label = "Stale - verify before crossing"
            fade = max(0.35, min(1.0, 1.0 - (age_sec / (180.0 * 60.0)) * 0.65))
        else:
            pin_status = "ACTIVE"
            pin_status_label = "Active"
            fade = max(0.35, min(1.0, 1.0 - (age_sec / (180.0 * 60.0)) * 0.65))

        pins.append({
            "lat":
                lat,

            "lng":
                lng,

            "status":
                pin_status,

            "status_label":
                pin_status_label,

            "is_stale":
                is_stale,

            "cleared":
                not bool(active_rows),

            "band":
                geometry.band(
                    (lo + hi) / 2
                ),

            "depth_cm":
                [lo, hi],

            "reports":
                len(rows_in_group),

            "confidence":
                confidence,

            "flow":
                flow,

            "verdicts":
                verdicts,

            "last_verified":
                last_verified_ts,

            "last_verified_minutes":
                age_min,

            "opacity":
                round(fade, 2),

            "report_id":
                target_rows[0]["id"],
        })

    return {
        "pins":
            pins,
    }


@app.get("/map/updates")
def map_updates(
    bbox: str,
    since: Optional[int] = 0,
    mode: Optional[str] = "hatchback",
):
    try:
        west, south, east, north = map(
            float,
            bbox.split(","),
        )
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=(
                "bbox must be "
                "west,south,east,north"
            ),
        )

    server_time = int(time.time())
    since_ts = int(since or 0)
    stale_threshold_sec = int(os.getenv("STALE_THRESHOLD_SECONDS", 3600))
    selected_mode = mode if mode in MODES else "hatchback"

    rows = db.changed_since(
        (west, south, east, north),
        since_ts,
    )

    try:
        roads_list = fetch_osm_roads(
            west,
            south,
            east,
            north,
        )
    except Exception:
        roads_list = []

    changes = []
    MAX_CHANGES = 100

    for row in rows[:MAX_CHANGES]:
        matched_road_id = None
        best_dist = float("inf")
        rep_dict = {
            "lat": float(row["lat"]),
            "lng": float(row["lng"]),
        }

        for r in roads_list:
            dist = road_report_distance(
                r.get("geometry", []),
                rep_dict,
            )
            if dist <= REPORT_MATCH_RADIUS_M and dist < best_dist:
                best_dist = dist
                matched_road_id = r.get("id")

        last_verified_ts = (
            row.get("last_verified")
            or row.get("created_at")
            or row.get("updated_at")
            or server_time
        )
        age_sec = max(0, server_time - last_verified_ts)
        is_stale = age_sec >= stale_threshold_sec
        fade = max(0.35, min(1.0, 1.0 - (age_sec / (180.0 * 60.0)) * 0.65))

        is_cleared = bool(row.get("cleared"))

        if is_cleared:
            status = "REPORTED_CLEAR"
            label = "Reported clear (unverified)"
            opacity = 0.9
            change_type = "cleared"
        elif is_stale:
            status = "STALE"
            label = "Stale - verify before crossing"
            opacity = round(fade, 2)
            created_ts = row.get("created_at") or 0
            change_type = "new" if created_ts > since_ts else "updated"
        else:
            depth_hi = float(row.get("depth_hi", 0) or 0)
            depth_lo = float(row.get("depth_lo", 0) or 0)
            if row.get("image_usable") and (depth_hi > 0 or float(row.get("confidence", 0) or 0) > 0):
                depth = (depth_lo + depth_hi) / 2.0
                status = classify_road(depth, selected_mode)
                label = status.capitalize()
            else:
                status = "UNKNOWN"
                label = "Unknown"
            opacity = round(fade, 2)
            created_ts = row.get("created_at") or 0
            change_type = "new" if created_ts > since_ts else "updated"

        raw_verdicts = row.get("verdicts")
        if isinstance(raw_verdicts, str):
            try:
                verdicts = json.loads(raw_verdicts)
            except Exception:
                verdicts = {}
        elif isinstance(raw_verdicts, dict):
            verdicts = raw_verdicts
        else:
            verdicts = {}

        band_val = row.get("band") or "unknown"

        clear_time_data = None
        if status in ("CAUTION", "FLOODED", "BLOCKED", "STALE"):
            try:
                ct = clear_time.estimate_clear_time(
                    float(row["lat"]),
                    float(row["lng"]),
                    (float(row.get("depth_lo", 0) or 0), float(row.get("depth_hi", 0) or 0)),
                    vehicle=selected_mode,
                )
                clear_time_data = {
                    "vehicle": ct["vehicle"],
                    "earliest": ct["earliest"],
                    "likely": ct["likely"],
                    "latest": ct["latest"],
                    "basis": ct["basis"],
                    "rain_note": ct["rain_note"],
                    "forecast_available": ct["forecast_available"],
                    "display_text": ct["display_text"],
                }
            except Exception:
                clear_time_data = None

        changes.append({
            "report_id": row.get("id"),
            "road_id": matched_road_id,
            "status": status,
            "label": label,
            "opacity": opacity,
            "lat": float(row["lat"]),
            "lng": float(row["lng"]),
            "depth_band": band_val,
            "band": band_val,
            "verdicts": verdicts,
            "last_verified": last_verified_ts,
            "change_type": change_type,
            "clear_time": clear_time_data,
        })

    return {
        "server_time": server_time,
        "changes": changes,
    }


@app.get("/weather")
def weather(
    lat: float,
    lng: float,
):
    try:
        data = environment.get_weather(
            lat,
            lng,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Weather service unavailable: "
                + str(exc)
            ),
        )

    return {
        "lat":
            lat,

        "lng":
            lng,

        "current":
            data.get(
                "current",
                {}
            ),

        "hourly":
            {
                "time":
                    data.get(
                        "hourly",
                        {}
                    ).get(
                        "time",
                        []
                    )[:24],

                "precipitation":
                    data.get(
                        "hourly",
                        {}
                    ).get(
                        "precipitation",
                        []
                    )[:24],

                "precipitation_probability":
                    data.get(
                        "hourly",
                        {}
                    ).get(
                        "precipitation_probability",
                        []
                    )[:24],
            }
    }


@app.get("/roads")
def roads(
    bbox: str,
    mode: str = "hatchback",
):
    """
    Returns real OpenStreetMap road geometries.

    Each road becomes a LineString. Flood reports are matched
    to nearby road geometry and the road receives a status:
      UNKNOWN, PASSABLE, CAUTION, FLOODED, BLOCKED
    """
    if mode not in MODES:
        mode = "hatchback"

    try:
        west, south, east, north = map(
            float,
            bbox.split(","),
        )
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=(
                "bbox must be "
                "west,south,east,north"
            ),
        )

    # Prevent accidental huge Overpass requests.
    if (
        east - west > 0.08
        or north - south > 0.08
    ):
        return {
            "type":
                "FeatureCollection",

            "features":
                [],

            "message":
                "Zoom in to load roads.",
        }

    osm_roads = fetch_osm_roads(
        west,
        south,
        east,
        north,
    )

    rows = db.nearby(
        (
            west - 0.002,
            south - 0.002,
            east + 0.002,
            north + 0.002,
        ),
        include_cleared=True,
    )

    reports = []

    for row in rows:
        reports.append({
            "id":
                row["id"],

            "lat":
                float(row["lat"]),

            "lng":
                float(row["lng"]),

            "depth_lo":
                float(row["depth_lo"]),

            "depth_hi":
                float(row["depth_hi"]),

            "confidence":
                float(row["confidence"]),

            "captured_at":
                row["captured_at"],

            "last_verified":
                row["last_verified"] if "last_verified" in row.keys() and row["last_verified"] else row["captured_at"],

            "cleared":
                bool(row["cleared"]) if "cleared" in row.keys() and row["cleared"] else False,
        })

    features = build_road_features(
        osm_roads,
        reports,
        mode,
    )

    counts = {
        "PASSABLE": 0,
        "CAUTION": 0,
        "FLOODED": 0,
        "BLOCKED": 0,
        "UNKNOWN": 0,
        "REPORTED_CLEAR": 0,
        "STALE": 0,
    }

    for feature in features:
        status = feature[
            "properties"
        ]["status"]

        counts[status] = (
            counts.get(status, 0)
            + 1
        )

    return {
        "type":
            "FeatureCollection",

        "features":
            features,

        "mode":
            mode,

        "counts":
            counts,

        "generated_at":
            int(time.time()),

        "weather_enabled":
            True,

        "clearance_model":
            "weather-plus-depth-road-type-estimate",
    }


@app.get("/forecast-crossing")
def forecast_crossing(
    lat: float,
    lng: float,
):
    rows = db.nearby(
        (
            lng - 0.01,
            lat - 0.01,
            lng + 0.01,
            lat + 0.01,
        )
    )

    current_depth = None

    if rows:
        nearest = min(
            rows,
            key=lambda row:
                environment.distance_m(
                    lat,
                    lng,
                    row["lat"],
                    row["lng"],
                ),
        )

        current_depth = (
            float(
                nearest["depth_lo"]
            )
            +
            float(
                nearest["depth_hi"]
            )
        ) / 2.0

    return environment.forecast_reopening(
        lat,
        lng,
        current_depth,
    )


@app.get("/hotspots")
def hotspots():
    rows = db.nearby(
        (
            -180,
            -90,
            180,
            90,
        )
    )

    groups = {}

    for row in rows:
        cell = (
            round(row["lat"], 2),
            round(row["lng"], 2),
        )

        groups.setdefault(
            cell,
            [],
        ).append(row)

    out = []

    sorted_groups = sorted(
        groups.items(),
        key=lambda item:
            len(item[1]),
        reverse=True,
    )

    for (
        lat,
        lng,
    ), rows_in_group in sorted_groups[:20]:
        out.append({
            "lat":
                lat,

            "lng":
                lng,

            "reports":
                len(rows_in_group),

            "avg_confidence":
                round(
                    sum(
                        row["confidence"]
                        for row
                        in rows_in_group
                    )
                    / len(rows_in_group),
                    2,
                ),

            "max_depth_cm":
                max(
                    row["depth_hi"]
                    for row
                    in rows_in_group
                ),
        })

    return {
        "hotspots":
            out,

        "generated_at":
            int(time.time()),
    }


@app.get("/hotspots.json")
def hotspots_json():
    return hotspots()


STATIC_DIR = ROOT / "static"

app.mount(
    "/static",
    StaticFiles(
        directory=STATIC_DIR,
    ),
    name="static",
)

if storage.get_backend() != "s3":
    app.mount(
        "/uploads",
        StaticFiles(
            directory=UPLOADS,
        ),
        name="uploads",
    )


@app.get("/")
def index():
    return FileResponse(
        STATIC_DIR / "index.html"
    )
