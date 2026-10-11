import os
import tempfile
import time
import unittest
from fastapi.testclient import TestClient

# Use a temporary test database
test_db_file = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
test_db_file.close()
os.environ["DB_PATH"] = test_db_file.name

from app.common import db
from app.main import app


class TestVoteLogic(unittest.TestCase):
    def setUp(self):
        db.init()
        # Insert a fresh test report
        self.report_id = db.insert({
            "key": "photos/test_flood.jpg",
            "lat": 12.9716,
            "lng": 77.5946,
            "accuracy": 5.0,
            "capturedAt": int(time.time()) - 3600,  # 1 hour ago
            "deviceId": "dev_initial",
            "depth_lo": 20.0,
            "depth_hi": 35.0,
            "confidence": 0.85,
            "band": "moderate",
            "flow": "slow",
            "verdicts": '{"hatchback": "CAUTION"}',
            "image_usable": True,
        })
        self.client = TestClient(app)

    def tearDown(self):
        pass

    def test_still_flooded_refreshes_last_verified(self):
        initial_rep = db.get_report(self.report_id)
        old_verified = initial_rep["last_verified"]

        time.sleep(1)
        res = db.add_vote(self.report_id, device_id="dev_user_1", vote="still_flooded")
        self.assertTrue(res["ok"])
        self.assertFalse(res["ignored"])
        self.assertFalse(res["cleared"])
        self.assertGreater(res["last_verified"], old_verified)

        updated_rep = db.get_report(self.report_id)
        self.assertEqual(updated_rep["last_verified"], res["last_verified"])
        self.assertEqual(updated_rep["cleared"], 0)

    def test_ignore_repeat_vote_from_same_device(self):
        res1 = db.add_vote(self.report_id, device_id="dev_user_repeat", vote="still_flooded")
        self.assertTrue(res1["ok"])
        self.assertFalse(res1["ignored"])

        # Repeat vote from same device
        res2 = db.add_vote(self.report_id, device_id="dev_user_repeat", vote="still_flooded")
        self.assertTrue(res2["ok"])
        self.assertTrue(res2["ignored"])
        self.assertEqual(res2["reason"], "already_voted")

    def test_two_cleared_votes_from_different_devices_mark_cleared(self):
        # Initial state: should be returned in nearby
        rows = db.nearby((77.50, 12.90, 77.70, 13.00))
        matching = [r for r in rows if r["id"] == self.report_id]
        self.assertEqual(len(matching), 1)

        # Vote 1: cleared from dev_A -> NOT yet marked cleared (threshold is 2)
        res1 = db.add_vote(self.report_id, device_id="dev_A", vote="cleared")
        self.assertTrue(res1["ok"])
        self.assertFalse(res1["cleared"])

        rows_after_1 = db.nearby((77.50, 12.90, 77.70, 13.00))
        matching_after_1 = [r for r in rows_after_1 if r["id"] == self.report_id]
        self.assertEqual(len(matching_after_1), 1)

        # Vote 2: cleared from dev_B -> marked cleared!
        res2 = db.add_vote(self.report_id, device_id="dev_B", vote="cleared")
        self.assertTrue(res2["ok"])
        self.assertTrue(res2["cleared"])

        # Road now marked cleared -> nearby excludes it by default
        rows_after_2 = db.nearby((77.50, 12.90, 77.70, 13.00))
        matching_after_2 = [r for r in rows_after_2 if r["id"] == self.report_id]
        self.assertEqual(len(matching_after_2), 0)

        # If include_cleared=True, it is still present in history
        rows_all = db.nearby((77.50, 12.90, 77.70, 13.00), include_cleared=True)
        matching_all = [r for r in rows_all if r["id"] == self.report_id]
        self.assertEqual(len(matching_all), 1)

    def test_confirm_endpoint_http_api(self):
        # Valid confirmation
        resp = self.client.post(
            f"/reports/{self.report_id}/confirm",
            json={"status": "still_flooded", "deviceId": "dev_client_1"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["ignored"])

        # Repeat vote ignored
        resp_repeat = self.client.post(
            f"/reports/{self.report_id}/confirm",
            json={"status": "still_flooded", "deviceId": "dev_client_1"},
        )
        self.assertEqual(resp_repeat.status_code, 200)
        self.assertTrue(resp_repeat.json()["ignored"])

        # Invalid status returns 400
        resp_bad = self.client.post(
            f"/reports/{self.report_id}/confirm",
            json={"status": "invalid_status", "deviceId": "dev_client_2"},
        )
        self.assertEqual(resp_bad.status_code, 400)

        # Non-existent report returns 404
        resp_404 = self.client.post(
            "/reports/nonexistent_report_id/confirm",
            json={"status": "cleared", "deviceId": "dev_client_3"},
        )
        self.assertEqual(resp_404.status_code, 404)

    @classmethod
    def tearDownClass(cls):
        try:
            if os.path.exists(test_db_file.name):
                os.remove(test_db_file.name)
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
