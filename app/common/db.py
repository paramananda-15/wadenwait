import os
import sqlite3
import time
import uuid
from typing import Dict, Any, List, Optional


DB = os.getenv(
    "DB_PATH",
    os.path.join(
        os.path.dirname(
            os.path.dirname(
                os.path.dirname(__file__)
            )
        ),
        "data",
        "wade.db"
    )
)


def conn():
    os.makedirs(
        os.path.dirname(DB),
        exist_ok=True
    )

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                key TEXT,
                lat REAL,
                lng REAL,
                accuracy REAL,
                captured_at INTEGER,
                device_id TEXT,
                depth_lo REAL,
                depth_hi REAL,
                confidence REAL,
                band TEXT,
                flow TEXT,
                verdicts TEXT,
                image_usable INTEGER,
                created_at INTEGER,
                last_verified INTEGER,
                cleared INTEGER DEFAULT 0,
                updated_at INTEGER
            )
        """)

        # Migration: ensure last_verified, cleared, and updated_at exist without touching existing data
        cursor = c.execute("PRAGMA table_info(reports)")
        columns = [row["name"] for row in cursor.fetchall()]

        if "last_verified" not in columns:
            c.execute("ALTER TABLE reports ADD COLUMN last_verified INTEGER")
            c.execute("UPDATE reports SET last_verified = created_at WHERE last_verified IS NULL")

        if "cleared" not in columns:
            c.execute("ALTER TABLE reports ADD COLUMN cleared INTEGER DEFAULT 0")

        if "updated_at" not in columns:
            c.execute("ALTER TABLE reports ADD COLUMN updated_at INTEGER")
            c.execute("UPDATE reports SET updated_at = coalesce(last_verified, created_at, 0) WHERE updated_at IS NULL")

        c.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_reports_geo
            ON reports(lat, lng)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_reports_updated
            ON reports(updated_at)
        """)

        # New votes table behind existing interface
        c.execute("""
            CREATE TABLE IF NOT EXISTS report_votes (
                report_id TEXT,
                device_id TEXT,
                vote TEXT,
                created_at INTEGER,
                PRIMARY KEY (report_id, device_id)
            )
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_votes_report
            ON report_votes(report_id)
        """)


def insert(r: Dict[str, Any]) -> str:
    rid = uuid.uuid4().hex
    now = int(time.time())
    last_verified = r.get("last_verified") or r.get("capturedAt") or now

    with conn() as c:
        c.execute(
            """
            INSERT INTO reports (
                id,
                key,
                lat,
                lng,
                accuracy,
                captured_at,
                device_id,
                depth_lo,
                depth_hi,
                confidence,
                band,
                flow,
                verdicts,
                image_usable,
                created_at,
                last_verified,
                cleared,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?
            )
            """,
            (
                rid,
                r["key"],
                r["lat"],
                r["lng"],
                r.get("accuracy"),
                r.get("capturedAt"),
                r.get("deviceId"),
                r["depth_lo"],
                r["depth_hi"],
                r["confidence"],
                r["band"],
                r["flow"],
                r["verdicts"],
                1 if r["image_usable"] else 0,
                now,
                last_verified,
                0,
                now,
            )
        )

    return rid


def get_report(report_id: str) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()


# Configurable constant: A 'cleared' vote must not override a photo report newer than 10 minutes
PHOTO_PROTECTION_SECONDS = int(
    os.getenv("PHOTO_PROTECTION_SECONDS", 600)
)


def add_vote(report_id: str, device_id: str, vote: str) -> Dict[str, Any]:
    """
    Record a vote ("still_flooded" | "cleared").
    One vote per device per report; repeats are ignored.
    - "still_flooded" refreshes report last_verified and ensures cleared=0.
    - two or more "cleared" votes from different devices mark report as cleared,
      EXCEPT if the photo report is newer than PHOTO_PROTECTION_SECONDS (10 min).
    """
    if vote not in ("still_flooded", "cleared"):
        raise ValueError("Invalid vote. Must be 'still_flooded' or 'cleared'.")

    now = int(time.time())

    with conn() as c:
        rep = c.execute(
            "SELECT id, captured_at, created_at, last_verified, cleared FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()

        if not rep:
            return {"ok": False, "error": "report_not_found"}

        existing = c.execute(
            "SELECT vote FROM report_votes WHERE report_id = ? AND device_id = ?",
            (report_id, device_id),
        ).fetchone()

        if existing:
            # Ignore repeat vote
            return {
                "ok": True,
                "ignored": True,
                "reason": "already_voted",
                "vote": existing["vote"],
                "report_id": report_id,
                "cleared": bool(rep["cleared"]),
                "last_verified": rep["last_verified"] or now,
            }

        # Insert new vote
        c.execute(
            """
            INSERT INTO report_votes (report_id, device_id, vote, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (report_id, device_id, vote, now),
        )

        photo_ts = rep["captured_at"] or rep["created_at"] or now
        photo_age = now - photo_ts
        is_photo_protected = photo_age < PHOTO_PROTECTION_SECONDS

        if vote == "still_flooded":
            c.execute(
                "UPDATE reports SET last_verified = ?, updated_at = ?, cleared = 0 WHERE id = ?",
                (now, now, report_id),
            )
            is_cleared = False
            last_verified = now

        elif vote == "cleared":
            cnt_row = c.execute(
                """
                SELECT COUNT(DISTINCT device_id) AS cnt
                FROM report_votes
                WHERE report_id = ? AND vote = 'cleared'
                """,
                (report_id,),
            ).fetchone()
            cleared_cnt = cnt_row["cnt"] if cnt_row else 0

            # Must not override photo report newer than 10 minutes (configurable constant)
            if cleared_cnt >= 2 and not is_photo_protected:
                c.execute(
                    "UPDATE reports SET cleared = 1, last_verified = ?, updated_at = ? WHERE id = ?",
                    (now, now, report_id),
                )
                is_cleared = True
                last_verified = now
            else:
                # Vote is counted in report_votes, but photo is recent: keep road marked flooded
                c.execute(
                    "UPDATE reports SET last_verified = ?, updated_at = ? WHERE id = ?",
                    (now, now, report_id),
                )
                is_cleared = bool(rep["cleared"]) and not is_photo_protected
                last_verified = now

    return {
        "ok": True,
        "ignored": False,
        "vote": vote,
        "report_id": report_id,
        "cleared": is_cleared,
        "last_verified": last_verified,
        "photo_protected": is_photo_protected,
    }


def nearby(bbox, include_cleared: bool = False) -> List[sqlite3.Row]:
    west, south, east, north = bbox

    with conn() as c:
        if include_cleared:
            query = """
                SELECT *
                FROM reports
                WHERE lng BETWEEN ? AND ?
                AND lat BETWEEN ? AND ?
                ORDER BY created_at DESC
                LIMIT 500
            """
        else:
            query = """
                SELECT *
                FROM reports
                WHERE lng BETWEEN ? AND ?
                AND lat BETWEEN ? AND ?
                AND (cleared IS NULL OR cleared = 0)
                ORDER BY created_at DESC
                LIMIT 500
            """

        rows = c.execute(
            query,
            (
                west,
                east,
                south,
                north,
            ),
        ).fetchall()

    return rows


def refresh_stale_reports(now: Optional[int] = None) -> int:
    """
    Checks active reports whose age since last_verified has crossed the stale threshold.
    Updates their updated_at to the transition timestamp (last_verified + stale_threshold)
    so polling queries pick up the stale transition.
    """
    now_ts = now if now is not None else int(time.time())
    stale_sec = int(os.getenv("STALE_THRESHOLD_SECONDS", 3600))
    with conn() as c:
        cursor = c.execute(
            """
            UPDATE reports
            SET updated_at = last_verified + ?
            WHERE (cleared IS NULL OR cleared = 0)
              AND last_verified IS NOT NULL
              AND (last_verified + ?) <= ?
              AND (updated_at IS NULL OR updated_at < last_verified + ?)
            """,
            (stale_sec, stale_sec, now_ts, stale_sec),
        )
        return cursor.rowcount


def changed_since(bbox, since_ts: int) -> List[Dict[str, Any]]:
    """
    Returns reports created, confirmed, cleared, or transitioned to stale after since_ts inside the bbox.
    Uses one updated_at value per report for clean polling.
    Keep signature simple so a DynamoDB version can be added later.
    """
    if isinstance(bbox, str):
        parts = [float(x.strip()) for x in bbox.split(",")]
        west, south, east, north = parts[0], parts[1], parts[2], parts[3]
    else:
        west, south, east, north = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])

    refresh_stale_reports()

    with conn() as c:
        query = """
            SELECT *
            FROM reports
            WHERE lng BETWEEN ? AND ?
              AND lat BETWEEN ? AND ?
              AND updated_at > ?
            ORDER BY updated_at ASC
            LIMIT 500
        """
        rows = c.execute(
            query,
            (
                min(west, east),
                max(west, east),
                min(south, north),
                max(south, north),
                int(since_ts or 0),
            ),
        ).fetchall()
        return [dict(row) for row in rows]