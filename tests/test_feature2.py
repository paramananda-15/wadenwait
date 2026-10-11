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
from app.common.rate_limit import reset_rate_limits
from app.main import app, build_road_features


class TestVoteLogic(unittest.TestCase):
    def setUp(self):
        reset_rate_limits()
        db.init()
        # Insert a fresh test report (1 hour old)
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
        reset_rate_limits()

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

    def test_photo_protection_newer_than_10_minutes(self):
        # A photo report newer than 10 minutes (e.g. 2 minutes old)
        recent_id = db.insert({
            "key": "photos/recent_flood.jpg",
            "lat": 12.9716,
            "lng": 77.5946,
            "accuracy": 5.0,
            "capturedAt": int(time.time()) - 120,  # 2 minutes ago (< 10 min)
            "deviceId": "dev_recent",
            "depth_lo": 25.0,
            "depth_hi": 40.0,
            "confidence": 0.9,
            "band": "deep",
            "flow": "slow",
            "verdicts": '{"hatchback": "FLOODED"}',
            "image_usable": True,
        })

        # Vote 1: cleared
        v1 = db.add_vote(recent_id, device_id="voter_1", vote="cleared")
        self.assertTrue(v1["ok"])
        self.assertFalse(v1["cleared"])
        self.assertTrue(v1["photo_protected"])

        # Vote 2: cleared from a different device -> even with 2 votes, must NOT clear
        v2 = db.add_vote(recent_id, device_id="voter_2", vote="cleared")
        self.assertTrue(v2["ok"])
        self.assertFalse(v2["cleared"])
        self.assertTrue(v2["photo_protected"])

        # Confirm road is still marked flooded in db
        rep = db.get_report(recent_id)
        self.assertEqual(rep["cleared"], 0)

    def test_distance_validation_rejects_greater_than_200m(self):
        # Within 200m: report is at (12.9716, 77.5946)
        resp_close = self.client.post(
            f"/reports/{self.report_id}/confirm",
            json={
                "status": "still_flooded",
                "deviceId": "dev_near",
                "lat": 12.9718,  # ~22m away
                "lng": 77.5946,
                "accuracy": 5.0,
            },
        )
        self.assertEqual(resp_close.status_code, 200)

        # Greater than 200m: ~490m away
        resp_far = self.client.post(
            f"/reports/{self.report_id}/confirm",
            json={
                "status": "still_flooded",
                "deviceId": "dev_far",
                "lat": 12.9760,  # ~490m away
                "lng": 77.5946,
                "accuracy": 5.0,
            },
        )
        self.assertEqual(resp_far.status_code, 400)
        self.assertIn("> 200m", resp_far.json()["detail"])

    def test_rate_limits_device_and_ip(self):
        # Device limit is 5 votes per minute
        for i in range(5):
            resp = self.client.post(
                f"/reports/{self.report_id}/confirm",
                json={
                    "status": "still_flooded",
                    "deviceId": "spam_device",
                    "lat": 12.9716,
                    "lng": 77.5946,
                    "accuracy": 5.0,
                },
                headers={"X-Forwarded-For": f"10.0.0.{i+1}"},
            )
            self.assertEqual(resp.status_code, 200)

        # 6th vote from same device should trigger HTTP 429
        resp_blocked = self.client.post(
            f"/reports/{self.report_id}/confirm",
            json={
                "status": "still_flooded",
                "deviceId": "spam_device",
                "lat": 12.9716,
                "lng": 77.5946,
                "accuracy": 5.0,
            },
            headers={"X-Forwarded-For": "10.0.0.99"},
        )
        self.assertEqual(resp_blocked.status_code, 429)
        self.assertIn("Device rate limit exceeded", resp_blocked.json()["detail"])

        # IP limit is 10 votes per minute
        reset_rate_limits()
        for i in range(10):
            resp = self.client.post(
                f"/reports/{self.report_id}/confirm",
                json={
                    "status": "still_flooded",
                    "deviceId": f"unique_dev_{i}",
                    "lat": 12.9716,
                    "lng": 77.5946,
                    "accuracy": 5.0,
                },
                headers={"X-Forwarded-For": "192.168.1.100"},
            )
            self.assertEqual(resp.status_code, 200)

        # 11th vote from same IP should trigger HTTP 429
        resp_ip_blocked = self.client.post(
            f"/reports/{self.report_id}/confirm",
            json={
                "status": "still_flooded",
                "deviceId": "unique_dev_11",
                "lat": 12.9716,
                "lng": 77.5946,
                "accuracy": 5.0,
            },
            headers={"X-Forwarded-For": "192.168.1.100"},
        )
        self.assertEqual(resp_ip_blocked.status_code, 429)
        self.assertIn("IP rate limit exceeded", resp_ip_blocked.json()["detail"])

    def test_road_status_reported_clear_and_stale(self):
        road = {
            "id": "osm-12345",
            "name": "Test Road",
            "highway": "primary",
            "geometry": [[77.5940, 12.9716], [77.5950, 12.9716]],
        }

        # Case 1: Stale road (report older than STALE_THRESHOLD_SECONDS = 3600s)
        stale_report = {
            "id": "rep_stale",
            "lat": 12.9716,
            "lng": 77.5945,
            "depth_lo": 25.0,
            "depth_hi": 40.0,
            "confidence": 0.8,
            "captured_at": int(time.time()) - 7200,  # 2 hours old
            "last_verified": int(time.time()) - 7200,
            "cleared": False,
        }
        features = build_road_features([road], [stale_report], mode="hatchback")
        self.assertEqual(len(features), 1)
        props = features[0]["properties"]
        self.assertEqual(props["status"], "STALE")
        self.assertEqual(props["status_label"], "Stale - verify before crossing")
        self.assertTrue(props["is_stale"])

        # Case 2: Road cleared by votes
        cleared_report = {
            "id": "rep_cleared",
            "lat": 12.9716,
            "lng": 77.5945,
            "depth_lo": 25.0,
            "depth_hi": 40.0,
            "confidence": 0.8,
            "captured_at": int(time.time()) - 7200,
            "last_verified": int(time.time()) - 60,
            "cleared": True,
        }
        features_cleared = build_road_features([road], [cleared_report], mode="hatchback")
        self.assertEqual(len(features_cleared), 1)
        props_cleared = features_cleared[0]["properties"]
        self.assertEqual(props_cleared["status"], "REPORTED_CLEAR")
        self.assertEqual(props_cleared["status_label"], "Reported clear (unverified)")
        self.assertFalse(props_cleared["is_stale"])

    def test_confirm_endpoint_http_api(self):
        # Valid confirmation requires lat, lng, accuracy
        resp = self.client.post(
            f"/reports/{self.report_id}/confirm",
            json={
                "status": "still_flooded",
                "deviceId": "dev_client_1",
                "lat": 12.9716,
                "lng": 77.5946,
                "accuracy": 5.0,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["ignored"])

        # Repeat vote ignored
        resp_repeat = self.client.post(
            f"/reports/{self.report_id}/confirm",
            json={
                "status": "still_flooded",
                "deviceId": "dev_client_1",
                "lat": 12.9716,
                "lng": 77.5946,
                "accuracy": 5.0,
            },
        )
        self.assertEqual(resp_repeat.status_code, 200)
        self.assertTrue(resp_repeat.json()["ignored"])

        # Invalid status returns 400
        resp_bad = self.client.post(
            f"/reports/{self.report_id}/confirm",
            json={
                "status": "invalid_status",
                "deviceId": "dev_client_2",
                "lat": 12.9716,
                "lng": 77.5946,
                "accuracy": 5.0,
            },
        )
        self.assertEqual(resp_bad.status_code, 400)

        # Missing lat/lng/accuracy returns 422
        resp_missing = self.client.post(
            f"/reports/{self.report_id}/confirm",
            json={"status": "still_flooded", "deviceId": "dev_client_missing"},
        )
        self.assertEqual(resp_missing.status_code, 422)

        # Non-existent report returns 404
        resp_404 = self.client.post(
            "/reports/nonexistent_report_id/confirm",
            json={
                "status": "cleared",
                "deviceId": "dev_client_3",
                "lat": 12.9716,
                "lng": 77.5946,
                "accuracy": 5.0,
            },
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
