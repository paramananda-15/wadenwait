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
                cleared INTEGER DEFAULT 0
            )
        """)

        # Migration: ensure last_verified and cleared exist without touching existing data
        cursor = c.execute("PRAGMA table_info(reports)")
        columns = [row["name"] for row in cursor.fetchall()]

        if "last_verified" not in columns:
            c.execute("ALTER TABLE reports ADD COLUMN last_verified INTEGER")
            c.execute("UPDATE reports SET last_verified = created_at WHERE last_verified IS NULL")

        if "cleared" not in columns:
            c.execute("ALTER TABLE reports ADD COLUMN cleared INTEGER DEFAULT 0")

        c.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_reports_geo
            ON reports(lat, lng)
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
                cleared
            )
            VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?
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
            )
        )

    return rid


def get_report(report_id: str) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()


def add_vote(report_id: str, device_id: str, vote: str) -> Dict[str, Any]:
    """
    Record a vote ("still_flooded" | "cleared").
    One vote per device per report; repeats are ignored.
    - "still_flooded" refreshes report last_verified.
    - two or more "cleared" votes from different devices mark report as cleared.
    """
    if vote not in ("still_flooded", "cleared"):
        raise ValueError("Invalid vote. Must be 'still_flooded' or 'cleared'.")

    now = int(time.time())

    with conn() as c:
        rep = c.execute(
            "SELECT id, last_verified, cleared FROM reports WHERE id = ?",
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

        if vote == "still_flooded":
            c.execute(
                "UPDATE reports SET last_verified = ? WHERE id = ?",
                (now, report_id),
            )
            is_cleared = bool(rep["cleared"])
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

            if cleared_cnt >= 2:
                c.execute(
                    "UPDATE reports SET cleared = 1, last_verified = ? WHERE id = ?",
                    (now, report_id),
                )
                is_cleared = True
                last_verified = now
            else:
                c.execute(
                    "UPDATE reports SET last_verified = ? WHERE id = ?",
                    (now, report_id),
                )
                is_cleared = bool(rep["cleared"])
                last_verified = now

    return {
        "ok": True,
        "ignored": False,
        "vote": vote,
        "report_id": report_id,
        "cleared": is_cleared,
        "last_verified": last_verified,
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