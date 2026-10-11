import json
import os
import time
import uuid
import math
import urllib.parse
import urllib.request

from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI,
    File,
    Form,
    UploadFile,
    HTTPException,
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
)

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


class ConfirmRequest(BaseModel):
    status: str
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

            # Conservative road status:
            # use the highest observed depth.
            depths = [
                (
                    float(
                        item[1]["depth_lo"]
                    )
                    +
                    float(
                        item[1]["depth_hi"]
                    )
                ) / 2.0
                for item
                in matching
            ]

            max_depth = max(depths)

            confidence = max(
                float(
                    item[1]["confidence"]
                )
                for item
                in matching
            )

            timestamps = [
                item[1]["captured_at"]
                for item
                in matching
                if item[1]["captured_at"]
                is not None
            ]

            # Use the closest flood observation as the
            # weather location for this road.
            closest_report = matching[0][1]

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

            now_ts = int(time.time())
            last_verified_ts = max(
                (item[1].get("last_verified") or item[1].get("captured_at") or now_ts)
                for item in matching
            )
            age_sec = max(0, now_ts - last_verified_ts)
            age_min = round(age_sec / 60)
            fade = max(0.35, min(1.0, 1.0 - (age_sec / (180.0 * 60.0)) * 0.65))

            properties.update({
                "status":
                    classify_road(
                        max_depth,
                        mode,
                    ),

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
                        clearance.get(
                            "estimated_clear_time"
                        ),

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

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "perception_mode":
            os.getenv(
                "PERCEPTION_MODE",
                "vlm",
            ),
        "ollama_model":
            os.getenv(
                "OLLAMA_MODEL",
                "qwen2.5vl:7b",
            ),
        "map_mode":
            "road-flood",
    }


@app.post("/upload-url")
def upload_url():
    key = (
        "photos/"
        + uuid.uuid4().hex
        + ".jpg"
    )

    return {
        "url":
            "/upload",

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
    path = UPLOADS / filename

    path.write_bytes(data)

    if not path.exists():
        raise HTTPException(
            status_code=500,
            detail="photo was not saved",
        )

    return {
        "ok": True,
        "key": key,
        "filename": filename,
        "size": len(data),
    }


@app.post("/reports")
def report(req: ReportRequest):
    filename = Path(req.key).name
    path = UPLOADS / filename

    print()
    print("========== REPORT ==========")
    print("Requested key :", req.key)
    print("Looking for   :", path)
    print("Exists        :", path.exists())

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="photo not found",
        )

    image = path.read_bytes()
    mode_env = os.getenv("PERCEPTION_MODE", "vlm").strip().lower()

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
            flow=obs.get("flow", "fast"),
        )

        exp_filename = f"explanation_{filename}"
        exp_path = UPLOADS / exp_filename
        _, reason = detector.create_explanation_image(
            image,
            det_est,
            save_path=exp_path,
        )
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
            "flow": obs.get("flow", "fast"),
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
                        "fast",
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
                        "fast",
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
                        "fast",
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
def confirm_report(id: str, req: ConfirmRequest):
    if req.status not in ("still_flooded", "cleared"):
        raise HTTPException(
            status_code=400,
            detail="status must be 'still_flooded' or 'cleared'",
        )

    device_id = req.deviceId or req.device_id or "unknown_device"

    rep = db.get_report(id)
    if not rep:
        raise HTTPException(
            status_code=404,
            detail="report not found",
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
        )
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
        lo = min(
            row["depth_lo"]
            for row
            in rows_in_group
        )

        hi = max(
            row["depth_hi"]
            for row
            in rows_in_group
        )

        confidence = (
            sum(
                row["confidence"]
                for row
                in rows_in_group
            )
            / len(rows_in_group)
        )

        flow = (
            rows_in_group[0]["flow"]
            if "flow"
            in rows_in_group[0].keys()
            else "fast"
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

        pins.append({
            "lat":
                lat,

            "lng":
                lng,

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
                max(
                    (row["last_verified"] if "last_verified" in row.keys() and row["last_verified"] else (row["created_at"] if "created_at" in row.keys() and row["created_at"] else int(time.time())))
                    for row in rows_in_group
                ),

            "last_verified_minutes":
                round(max(0, int(time.time()) - max(
                    (row["last_verified"] if "last_verified" in row.keys() and row["last_verified"] else (row["created_at"] if "created_at" in row.keys() and row["created_at"] else int(time.time())))
                    for row in rows_in_group
                )) / 60),

            "opacity":
                round(max(0.35, min(1.0, 1.0 - (max(0, int(time.time()) - max(
                    (row["last_verified"] if "last_verified" in row.keys() and row["last_verified"] else (row["created_at"] if "created_at" in row.keys() and row["created_at"] else int(time.time())))
                    for row in rows_in_group
                )) / (180.0 * 60.0)) * 0.65)), 2),

            "report_id":
                rows_in_group[0]["id"],
        })

    return {
        "pins":
            pins,
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
        )
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
