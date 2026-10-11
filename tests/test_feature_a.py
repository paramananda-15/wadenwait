import json
import os
import time
import unittest
from fastapi.testclient import TestClient

from app.common import db
from app.main import app


class TestFeatureAPolling(unittest.TestCase):
    def setUp(self):
        # Use an isolated test database
        self.test_db_path = f"data/test_wade_feature_a_{int(time.time()*1000)}.db"
        os.environ["DB_PATH"] = self.test_db_path
        db.DB = self.test_db_path
        db.init()
        self.client = TestClient(app)

    def tearDown(self):
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    def test_changed_since_respects_bbox(self):
        bbox = (77.50, 12.90, 77.60, 13.00)
        # Inside report
        rid_in = db.insert({
            "key": "photos/in.jpg",
            "lat": 12.95,
            "lng": 77.55,
            "depth_lo": 20.0,
            "depth_hi": 40.0,
            "confidence": 0.8,
            "band": "ankle_to_knee",
            "flow": "slow",
            "verdicts": json.dumps({"hatchback": "DO_NOT_CROSS"}),
            "image_usable": True,
        })
        # Outside report (lat 13.50 is north of bbox)
        rid_out = db.insert({
            "key": "photos/out.jpg",
            "lat": 13.50,
            "lng": 77.55,
            "depth_lo": 20.0,
            "depth_hi": 40.0,
            "confidence": 0.8,
            "band": "ankle_to_knee",
            "flow": "slow",
            "verdicts": json.dumps({"hatchback": "DO_NOT_CROSS"}),
            "image_usable": True,
        })

        changes = db.changed_since(bbox, 0)
        rids = [r["id"] for r in changes]
        self.assertIn(rid_in, rids)
        self.assertNotIn(rid_out, rids)

    def test_changed_since_respects_since_ts(self):
        bbox = (77.50, 12.90, 77.60, 13.00)
        t_base = int(time.time()) - 100

        rid = db.insert({
            "key": "photos/p.jpg",
            "lat": 12.95,
            "lng": 77.55,
            "depth_lo": 15.0,
            "depth_hi": 25.0,
            "confidence": 0.75,
            "band": "ankle_to_knee",
            "flow": "still",
            "verdicts": json.dumps({"hatchback": "CAUTION"}),
            "image_usable": True,
        })

        # Manually set updated_at to t_base
        with db.conn() as c:
            c.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (t_base, rid))

        # Query since t_base (exclusive) -> must be empty
        changes_after = db.changed_since(bbox, t_base)
        self.assertEqual(len(changes_after), 0)

        # Query since t_base - 1 -> must include the report
        changes_before = db.changed_since(bbox, t_base - 1)
        self.assertEqual(len(changes_before), 1)
        self.assertEqual(changes_before[0]["id"], rid)

    def test_changed_since_includes_new_report(self):
        bbox = (77.50, 12.90, 77.60, 13.00)
        since_ts = int(time.time()) - 10

        rid = db.insert({
            "key": "photos/new.jpg",
            "lat": 12.94,
            "lng": 77.54,
            "depth_lo": 30.0,
            "depth_hi": 50.0,
            "confidence": 0.85,
            "band": "knee_to_waist",
            "flow": "fast",
            "verdicts": json.dumps({"hatchback": "DO_NOT_CROSS"}),
            "image_usable": True,
        })

        changes = db.changed_since(bbox, since_ts)
        self.assertTrue(any(r["id"] == rid for r in changes))
        report = next(r for r in changes if r["id"] == rid)
        self.assertGreater(report["updated_at"], since_ts)

    def test_changed_since_includes_confirmed_transition(self):
        bbox = (77.50, 12.90, 77.60, 13.00)
        t_create = int(time.time()) - 200

        rid = db.insert({
            "key": "photos/conf.jpg",
            "lat": 12.95,
            "lng": 77.55,
            "depth_lo": 20.0,
            "depth_hi": 30.0,
            "confidence": 0.8,
            "band": "ankle_to_knee",
            "flow": "slow",
            "verdicts": json.dumps({"hatchback": "CAUTION"}),
            "image_usable": True,
        })
        with db.conn() as c:
            c.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (t_create, rid))

        since_ts = t_create + 50
        # No updates yet since since_ts
        self.assertEqual(len(db.changed_since(bbox, since_ts)), 0)

        # Confirm vote
        res = db.add_vote(rid, "device_confirm_1", "still_flooded")
        self.assertTrue(res["ok"])

        # Now changed_since picks up the confirmed report
        changes = db.changed_since(bbox, since_ts)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["id"], rid)
        self.assertGreater(changes[0]["updated_at"], since_ts)

    def test_changed_since_includes_cleared_transition(self):
        bbox = (77.50, 12.90, 77.60, 13.00)
        # Create report older than PHOTO_PROTECTION_SECONDS (600s)
        old_time = int(time.time()) - 1000
        rid = db.insert({
            "key": "photos/clear.jpg",
            "lat": 12.95,
            "lng": 77.55,
            "capturedAt": old_time,
            "depth_lo": 10.0,
            "depth_hi": 20.0,
            "confidence": 0.7,
            "band": "ankle_to_knee",
            "flow": "slow",
            "verdicts": json.dumps({"hatchback": "GO"}),
            "image_usable": True,
        })
        with db.conn() as c:
            c.execute("UPDATE reports SET created_at = ?, captured_at = ?, updated_at = ? WHERE id = ?",
                      (old_time, old_time, old_time, rid))

        since_ts = int(time.time()) - 10
        # First cleared vote
        db.add_vote(rid, "device_clr_1", "cleared")
        # Second cleared vote from different device -> marks cleared
        db.add_vote(rid, "device_clr_2", "cleared")

        changes = db.changed_since(bbox, since_ts)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["id"], rid)
        self.assertEqual(changes[0]["cleared"], 1)

    def test_map_updates_endpoint_returns_server_time_and_no_old_items(self):
        bbox_str = "77.50,12.90,77.60,13.00"
        t_old = int(time.time()) - 300

        rid = db.insert({
            "key": "photos/api.jpg",
            "lat": 12.95,
            "lng": 77.55,
            "depth_lo": 25.0,
            "depth_hi": 40.0,
            "confidence": 0.8,
            "band": "knee_to_waist",
            "flow": "slow",
            "verdicts": json.dumps({"hatchback": "FLOODED"}),
            "image_usable": True,
        })
        with db.conn() as c:
            c.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (t_old, rid))

        # 1. Query with since = t_old - 10: item should be included
        resp = self.client.get(f"/map/updates?bbox={bbox_str}&since={t_old - 10}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("server_time", data)
        self.assertIsInstance(data["server_time"], int)
        self.assertIn("changes", data)
        self.assertEqual(len(data["changes"]), 1)
        item = data["changes"][0]
        self.assertEqual(item["report_id"], rid)
        self.assertIn("status", item)
        self.assertIn("label", item)
        self.assertIn("opacity", item)
        self.assertIn("lat", item)
        self.assertIn("lng", item)
        self.assertIn("depth_band", item)
        self.assertIn("verdicts", item)
        self.assertIn("last_verified", item)
        self.assertIn("change_type", item)

        # 2. Query with since = data["server_time"]: no old items returned
        resp_future = self.client.get(f"/map/updates?bbox={bbox_str}&since={data['server_time']}")
        self.assertEqual(resp_future.status_code, 200)
        data_future = resp_future.json()
        self.assertIn("server_time", data_future)
        self.assertEqual(len(data_future["changes"]), 0)


if __name__ == "__main__":
    unittest.main()
