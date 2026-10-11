import json
import os
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
from fastapi import HTTPException

from . import geohash


TTL_SECONDS = int(os.getenv("DYNAMODB_TTL_SECONDS", 14 * 86400))  # 14 days default
PHOTO_PROTECTION_SECONDS = int(os.getenv("PHOTO_PROTECTION_SECONDS", 600))


def _get_dynamo_resource():
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
    endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")
    if endpoint_url:
        return boto3.resource("dynamodb", region_name=region, endpoint_url=endpoint_url)
    return boto3.resource("dynamodb", region_name=region)


def _get_reports_table():
    table_name = os.getenv("REPORTS_TABLE", "wade-reports")
    return _get_dynamo_resource().Table(table_name)


def _get_votes_table():
    table_name = os.getenv("VOTES_TABLE", "wade-votes")
    return _get_dynamo_resource().Table(table_name)


def _get_clear_time_log_table():
    table_name = os.getenv("CLEAR_TIME_LOG_TABLE", "wade-clear-time-log")
    return _get_dynamo_resource().Table(table_name)


def _to_dynamo_item(d: Dict[str, Any]) -> Dict[str, Any]:
    """Convert floats and nested floats to Decimal for DynamoDB serialization."""
    out = {}
    for k, v in d.items():
        if v is None:
            out[k] = None
        elif isinstance(v, float):
            out[k] = Decimal(str(round(v, 6)))
        elif isinstance(v, (int, str, bool)):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [_to_dynamo_item({"_": item})["_"] if isinstance(item, (dict, float)) else item for item in v]
        elif isinstance(v, dict):
            out[k] = _to_dynamo_item(v)
        else:
            out[k] = str(v)
    return out


def _from_dynamo_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Decimal values back to int/float for application use."""
    if not item:
        return {}
    out = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            if v % 1 == 0:
                out[k] = int(v)
            else:
                out[k] = float(v)
        elif isinstance(v, list):
            out[k] = [float(x) if isinstance(x, Decimal) and x % 1 != 0 else (int(x) if isinstance(x, Decimal) else x) for x in v]
        elif isinstance(v, dict):
            out[k] = _from_dynamo_item(v)
        else:
            out[k] = v
    return out


def init():
    """DynamoDB tables are managed via IaC (CloudFormation/SAM)."""
    pass


def insert(r: Dict[str, Any]) -> str:
    rid = uuid.uuid4().hex
    now = int(time.time())
    last_verified = r.get("last_verified") or r.get("capturedAt") or now
    expires_at = now + TTL_SECONDS

    lat = float(r["lat"])
    lng = float(r["lng"])
    cell = geohash.encode(lat, lng, precision=5)

    base_record = {
        "id": rid,
        "cell": cell,
        "key": r["key"],
        "lat": lat,
        "lng": lng,
        "accuracy": float(r["accuracy"]) if r.get("accuracy") is not None else None,
        "captured_at": int(r["capturedAt"]) if r.get("capturedAt") is not None else None,
        "device_id": str(r.get("deviceId", "")),
        "depth_lo": float(r["depth_lo"]),
        "depth_hi": float(r["depth_hi"]),
        "confidence": float(r["confidence"]),
        "band": str(r["band"]),
        "flow": str(r["flow"]),
        "verdicts": r["verdicts"] if isinstance(r["verdicts"], str) else json.dumps(r["verdicts"]),
        "image_usable": 1 if r.get("image_usable") else 0,
        "created_at": now,
        "last_verified": int(last_verified),
        "cleared": 0,
        "updated_at": now,
    }

    # 1. Ephemeral active record with TTL
    active_item = dict(base_record)
    active_item["sk"] = f"REPORT#{rid}"
    active_item["expiresAt"] = expires_at

    # 2. History record without TTL for permanent record
    history_item = dict(base_record)
    history_item["sk"] = f"HIST#{rid}"

    table = _get_reports_table()
    table.put_item(Item=_to_dynamo_item(active_item))
    table.put_item(Item=_to_dynamo_item(history_item))

    return rid


def get_report(report_id: str) -> Optional[Dict[str, Any]]:
    table = _get_reports_table()
    now = int(time.time())
    # Query using IdIndex GSI
    try:
        resp = table.query(
            IndexName="IdIndex",
            KeyConditionExpression=Key("id").eq(report_id),
            Limit=2,
        )
        items = resp.get("Items", [])
        for item in items:
            raw = _from_dynamo_item(item)
            # Filter ephemeral items whose TTL expired but not yet deleted
            if raw.get("sk", "").startswith("REPORT#"):
                if raw.get("expiresAt") and raw["expiresAt"] < now:
                    continue
                return raw
        # If no active item found, return history item if available
        if items:
            return _from_dynamo_item(items[0])
        return None
    except Exception as exc:
        # Fallback to scan if IdIndex is not yet created in ad-hoc testing
        resp = table.scan(
            FilterExpression=Attr("id").eq(report_id),
            Limit=2,
        )
        items = resp.get("Items", [])
        if items:
            return _from_dynamo_item(items[0])
        return None


def add_vote(report_id: str, device_id: str, vote: str) -> Dict[str, Any]:
    if vote not in ("still_flooded", "cleared"):
        raise ValueError("Invalid vote. Must be 'still_flooded' or 'cleared'.")

    now = int(time.time())
    rep = get_report(report_id)
    if not rep:
        return {"ok": False, "error": "report_not_found"}

    votes_table = _get_votes_table()
    expires_at = now + TTL_SECONDS

    # Attempt to insert vote with conditional check for uniqueness
    try:
        votes_table.put_item(
            Item={
                "report_id": report_id,
                "device_id": device_id,
                "vote": vote,
                "created_at": now,
                "expiresAt": expires_at,
            },
            ConditionExpression="attribute_not_exists(report_id) AND attribute_not_exists(device_id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Repeated vote ignored
            existing = votes_table.get_item(Key={"report_id": report_id, "device_id": device_id}).get("Item", {})
            return {
                "ok": True,
                "ignored": True,
                "reason": "already_voted",
                "vote": existing.get("vote", vote),
                "report_id": report_id,
                "cleared": bool(rep.get("cleared")),
                "last_verified": rep.get("last_verified") or now,
            }
        raise

    photo_ts = rep.get("captured_at") or rep.get("created_at") or now
    photo_age = now - photo_ts
    is_photo_protected = photo_age < PHOTO_PROTECTION_SECONDS

    reports_table = _get_reports_table()
    cell = rep.get("cell") or geohash.encode(float(rep["lat"]), float(rep["lng"]), 5)
    sk = f"REPORT#{report_id}"

    if vote == "still_flooded":
        reports_table.update_item(
            Key={"cell": cell, "sk": sk},
            UpdateExpression="SET last_verified = :lv, updated_at = :ua, cleared = :cl",
            ExpressionAttributeValues={":lv": now, ":ua": now, ":cl": 0},
        )
        is_cleared = False
        last_verified = now
        rep_lat = None
        rep_lng = None

    elif vote == "cleared":
        # Count cleared votes for this report
        resp = votes_table.query(
            KeyConditionExpression=Key("report_id").eq(report_id),
        )
        cleared_cnt = sum(1 for v in resp.get("Items", []) if v.get("vote") == "cleared")

        if cleared_cnt >= 2 and not is_photo_protected:
            reports_table.update_item(
                Key={"cell": cell, "sk": sk},
                UpdateExpression="SET cleared = :cl, last_verified = :lv, updated_at = :ua",
                ExpressionAttributeValues={":cl": 1, ":lv": now, ":ua": now},
            )
            is_cleared = True
            last_verified = now
            rep_lat = rep.get("lat")
            rep_lng = rep.get("lng")
        else:
            reports_table.update_item(
                Key={"cell": cell, "sk": sk},
                UpdateExpression="SET last_verified = :lv, updated_at = :ua",
                ExpressionAttributeValues={":lv": now, ":ua": now},
            )
            is_cleared = bool(rep.get("cleared")) and not is_photo_protected
            last_verified = now
            rep_lat = None
            rep_lng = None

    if is_cleared and rep_lat is not None and rep_lng is not None:
        record_actual_cleared(lat=rep_lat, lng=rep_lng, cleared_at=now)

    return {
        "ok": True,
        "ignored": False,
        "vote": vote,
        "report_id": report_id,
        "cleared": is_cleared,
        "last_verified": last_verified,
        "photo_protected": is_photo_protected,
    }


def nearby(bbox, include_cleared: bool = False) -> List[Dict[str, Any]]:
    west, south, east, north = map(float, bbox)
    min_lng, max_lng = min(west, east), max(west, east)
    min_lat, max_lat = min(south, north), max(south, north)

    cells = geohash.bbox_cells(min_lng, min_lat, max_lng, max_lat, precision=5)
    table = _get_reports_table()
    now = int(time.time())

    items = []
    for cell in cells:
        resp = table.query(
            KeyConditionExpression=Key("cell").eq(cell) & Key("sk").begins_with("REPORT#"),
        )
        for raw in resp.get("Items", []):
            item = _from_dynamo_item(raw)
            # Filter expired TTL lag items
            if item.get("expiresAt") and item["expiresAt"] < now:
                continue
            lat = float(item["lat"])
            lng = float(item["lng"])
            if not (min_lat <= lat <= max_lat and min_lng <= lng <= max_lng):
                continue
            if not include_cleared and bool(item.get("cleared")):
                continue
            items.append(item)

    items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return items[:500]


def refresh_stale_reports(now: Optional[int] = None) -> int:
    """No-op for DynamoDB: STALE status is computed on read without writes."""
    return 0


def changed_since(bbox, since_ts: int) -> List[Dict[str, Any]]:
    """
    Returns reports created, confirmed, cleared, or transitioned to stale after since_ts.
    Computes STALE transitions dynamically at read-time without writing to DynamoDB.
    """
    if isinstance(bbox, str):
        parts = [float(x.strip()) for x in bbox.split(",")]
        west, south, east, north = parts[0], parts[1], parts[2], parts[3]
    else:
        west, south, east, north = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])

    min_lng, max_lng = min(west, east), max(west, east)
    min_lat, max_lat = min(south, north), max(south, north)

    cells = geohash.bbox_cells(min_lng, min_lat, max_lng, max_lat, precision=5)
    table = _get_reports_table()
    now_ts = int(time.time())
    stale_sec = int(os.getenv("STALE_THRESHOLD_SECONDS", 3600))
    since_ts = int(since_ts or 0)

    changes = []
    for cell in cells:
        resp = table.query(
            KeyConditionExpression=Key("cell").eq(cell) & Key("sk").begins_with("REPORT#"),
        )
        for raw in resp.get("Items", []):
            item = _from_dynamo_item(raw)
            # Filter expired TTL lag
            if item.get("expiresAt") and item["expiresAt"] < now_ts:
                continue

            lat = float(item["lat"])
            lng = float(item["lng"])
            if not (min_lat <= lat <= max_lat and min_lng <= lng <= max_lng):
                continue

            # Compute dynamic STALE status and effective updated_at
            last_verified = item.get("last_verified") or item.get("created_at") or now_ts
            age_sec = max(0, now_ts - last_verified)
            is_stale = age_sec >= stale_sec and not bool(item.get("cleared"))

            effective_updated_at = item.get("updated_at") or item.get("created_at") or 0
            stale_transition_ts = last_verified + stale_sec
            if is_stale and stale_transition_ts > effective_updated_at:
                effective_updated_at = stale_transition_ts

            if effective_updated_at > since_ts:
                item_copy = dict(item)
                item_copy["updated_at"] = effective_updated_at
                item_copy["is_stale"] = is_stale
                changes.append(item_copy)

    changes.sort(key=lambda x: x.get("updated_at") or 0)
    return changes[:500]


def log_clear_time(
    road_id: Optional[str],
    lat: float,
    lng: float,
    vehicle: str,
    issued_at: int,
    earliest: Optional[str],
    likely: Optional[str],
    latest: Optional[str],
    basis: str,
    forecast_available: bool = True,
    earliest_ts: Optional[int] = None,
    likely_ts: Optional[int] = None,
    latest_ts: Optional[int] = None,
) -> str:
    log_id = uuid.uuid4().hex
    cell_key = f"{round(float(lat), 1)}_{round(float(lng), 1)}"
    now = int(time.time())
    expires_at = now + TTL_SECONDS

    table = _get_clear_time_log_table()
    item = {
        "id": log_id,
        "road_id": road_id or "unknown",
        "lat": float(lat),
        "lng": float(lng),
        "cell_key": cell_key,
        "vehicle": vehicle,
        "issued_at": int(issued_at),
        "earliest": earliest,
        "likely": likely,
        "latest": latest,
        "earliest_ts": earliest_ts,
        "likely_ts": likely_ts,
        "latest_ts": latest_ts,
        "basis": basis,
        "forecast_available": 1 if forecast_available else 0,
        "actual_cleared_at": None,
        "expiresAt": expires_at,
    }
    table.put_item(Item=_to_dynamo_item(item))
    return log_id


def record_actual_cleared(
    road_id: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    cleared_at: Optional[int] = None,
) -> int:
    now = cleared_at if cleared_at is not None else int(time.time())
    table = _get_clear_time_log_table()
    updated = 0

    items_to_update = []
    if road_id:
        try:
            resp = table.query(
                IndexName="RoadIndex",
                KeyConditionExpression=Key("road_id").eq(road_id),
            )
            for it in resp.get("Items", []):
                if it.get("actual_cleared_at") is None:
                    items_to_update.append(it["id"])
        except Exception:
            # Fallback scan if Index not ready in ad-hoc testing
            resp = table.scan(
                FilterExpression=Attr("road_id").eq(road_id) & Attr("actual_cleared_at").not_exists(),
            )
            for it in resp.get("Items", []):
                items_to_update.append(it["id"])

    if lat is not None and lng is not None:
        cell_key = f"{round(float(lat), 1)}_{round(float(lng), 1)}"
        try:
            resp = table.query(
                IndexName="CellIndex",
                KeyConditionExpression=Key("cell_key").eq(cell_key),
            )
            for it in resp.get("Items", []):
                if it.get("actual_cleared_at") is None:
                    items_to_update.append(it["id"])
        except Exception:
            resp = table.scan(
                FilterExpression=Attr("cell_key").eq(cell_key) & Attr("actual_cleared_at").not_exists(),
            )
            for it in resp.get("Items", []):
                items_to_update.append(it["id"])

    for item_id in set(items_to_update):
        table.update_item(
            Key={"id": item_id},
            UpdateExpression="SET actual_cleared_at = :ac",
            ExpressionAttributeValues={":ac": now},
        )
        updated += 1

    return updated


def get_clear_time_logs(limit: int = 200) -> List[Dict[str, Any]]:
    table = _get_clear_time_log_table()
    resp = table.scan(Limit=limit)
    items = [_from_dynamo_item(it) for it in resp.get("Items", [])]
    items.sort(key=lambda x: x.get("issued_at") or 0, reverse=True)
    return items[:limit]


def get_recent_reports_for_road_or_cell(
    road_id: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    max_age_sec: int = 6 * 3600,
) -> List[Dict[str, Any]]:
    if lat is None or lng is None:
        return []

    now_ts = int(time.time())
    cutoff = now_ts - max_age_sec
    table = _get_reports_table()

    cell = geohash.encode(float(lat), float(lng), precision=5)
    resp = table.query(
        KeyConditionExpression=Key("cell").eq(cell) & Key("sk").begins_with("REPORT#"),
    )

    results = []
    for raw in resp.get("Items", []):
        it = _from_dynamo_item(raw)
        if it.get("expiresAt") and it["expiresAt"] < now_ts:
            continue
        rep_lat = float(it["lat"])
        rep_lng = float(it["lng"])
        if abs(rep_lat - lat) <= 0.02 and abs(rep_lng - lng) <= 0.02:
            ts = it.get("captured_at") or it.get("created_at") or 0
            if ts >= cutoff:
                results.append(it)

    results.sort(key=lambda x: x.get("captured_at") or x.get("created_at") or 0)
    return results


def check_and_increment_daily_cap(max_cap: int = 300) -> None:
    """
    Atomic counter in DynamoDB to cap vision model invocations.
    Raises HTTPException 429 when max_cap is exceeded.
    """
    today_str = time.strftime("%Y-%m-%d", time.gmtime())
    table = _get_reports_table()
    key = {"cell": "DAILY_CAP", "sk": f"DATE#{today_str}"}

    try:
        table.update_item(
            Key=key,
            UpdateExpression="ADD call_count :inc",
            ConditionExpression="attribute_not_exists(call_count) OR call_count < :cap",
            ExpressionAttributeValues={":inc": 1, ":cap": max_cap},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise HTTPException(
                status_code=429,
                detail=f"Daily vision analysis cap reached ({max_cap} requests/day). Please try again tomorrow.",
            )
        raise
