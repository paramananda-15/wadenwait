import io
import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException
from moto import mock_aws
from PIL import Image

from app.common import db, dynamo_db, geohash, llm, storage


class TestS3Storage(unittest.TestCase):
    def setUp(self):
        self.mock_aws = mock_aws()
        self.mock_aws.start()
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_SECURITY_TOKEN"] = "testing"
        os.environ["AWS_SESSION_TOKEN"] = "testing"
        os.environ["STORAGE_BACKEND"] = "s3"
        os.environ["S3_BUCKET"] = "test-wade-bucket"
        os.environ["AWS_REGION"] = "ap-south-1"

        self.s3 = boto3.client("s3", region_name="ap-south-1")
        self.s3.create_bucket(
            Bucket="test-wade-bucket",
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )

    def tearDown(self):
        os.environ["STORAGE_BACKEND"] = "local"
        self.mock_aws.stop()

    def test_save_and_get_file(self):
        key = "photos/sample_flood.jpg"
        data = b"\xff\xd8\xff\xe0\x00\x10JFIFfakeimagebytes"

        saved_key = storage.save_file(key, data, content_type="image/jpeg")
        self.assertEqual(saved_key, key)
        self.assertTrue(storage.file_exists(key))

        retrieved = storage.get_file(key)
        self.assertEqual(retrieved, data)

    def test_file_not_found(self):
        self.assertFalse(storage.file_exists("photos/nonexistent.jpg"))
        with self.assertRaises(FileNotFoundError):
            storage.get_file("photos/nonexistent.jpg")

    def test_presigned_urls(self):
        key = "photos/photo123.jpg"
        storage.save_file(key, b"abc")

        get_url = storage.get_url(key)
        self.assertIn("https://", get_url)
        self.assertIn("test-wade-bucket", get_url)
        self.assertIn("photo123.jpg", get_url)

        put_url = storage.get_upload_url("photos/new_photo.jpg")
        self.assertIn("https://", put_url)
        self.assertIn("new_photo.jpg", put_url)


class TestDynamoDBBackend(unittest.TestCase):
    def setUp(self):
        self.mock_aws = mock_aws()
        self.mock_aws.start()
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_SECURITY_TOKEN"] = "testing"
        os.environ["AWS_SESSION_TOKEN"] = "testing"
        os.environ["DB_BACKEND"] = "dynamodb"
        os.environ["AWS_REGION"] = "ap-south-1"
        os.environ["REPORTS_TABLE"] = "test-wade-reports"
        os.environ["VOTES_TABLE"] = "test-wade-votes"
        os.environ["CLEAR_TIME_LOG_TABLE"] = "test-wade-clear-time-log"

        self.dynamo = boto3.resource("dynamodb", region_name="ap-south-1")

        # 1. Reports table with IdIndex GSI and cell PK
        self.reports_table = self.dynamo.create_table(
            TableName="test-wade-reports",
            KeySchema=[
                {"AttributeName": "cell", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "cell", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "IdIndex",
                    "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # 2. Votes table
        self.votes_table = self.dynamo.create_table(
            TableName="test-wade-votes",
            KeySchema=[
                {"AttributeName": "report_id", "KeyType": "HASH"},
                {"AttributeName": "device_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "report_id", "AttributeType": "S"},
                {"AttributeName": "device_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # 3. Clear time log table
        self.ctl_table = self.dynamo.create_table(
            TableName="test-wade-clear-time-log",
            KeySchema=[
                {"AttributeName": "id", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "id", "AttributeType": "S"},
                {"AttributeName": "road_id", "AttributeType": "S"},
                {"AttributeName": "cell_key", "AttributeType": "S"},
                {"AttributeName": "issued_at", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "RoadIndex",
                    "KeySchema": [
                        {"AttributeName": "road_id", "KeyType": "HASH"},
                        {"AttributeName": "issued_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "CellIndex",
                    "KeySchema": [
                        {"AttributeName": "cell_key", "KeyType": "HASH"},
                        {"AttributeName": "issued_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    def tearDown(self):
        os.environ["DB_BACKEND"] = "sqlite"
        self.mock_aws.stop()

    def test_insert_and_get_report_with_ttl_and_history(self):
        now = int(time.time())
        report_data = {
            "key": "photos/test1.jpg",
            "lat": 12.9716,
            "lng": 77.5946,
            "accuracy": 5.0,
            "capturedAt": now - 30,
            "deviceId": "dev-001",
            "depth_lo": 20.0,
            "depth_hi": 35.0,
            "confidence": 0.85,
            "band": "ankle_deep",
            "flow": "still",
            "verdicts": {"suv": "GO", "two_wheeler": "CAUTION"},
            "image_usable": True,
        }

        rid = db.insert(report_data)
        self.assertIsNotNone(rid)

        # Check get_report retrieves it
        rep = db.get_report(rid)
        self.assertIsNotNone(rep)
        self.assertEqual(rep["id"], rid)
        self.assertAlmostEqual(float(rep["lat"]), 12.9716, places=4)
        self.assertAlmostEqual(float(rep["lng"]), 77.5946, places=4)
        self.assertTrue("expiresAt" in rep)

        # Check DynamoDB items directly: should have 1 active (REPORT#) and 1 history (HIST#)
        cell = geohash.encode(12.9716, 77.5946, 5)
        active_item = self.reports_table.get_item(Key={"cell": cell, "sk": f"REPORT#{rid}"}).get("Item")
        self.assertIsNotNone(active_item)
        self.assertTrue("expiresAt" in active_item)

        hist_item = self.reports_table.get_item(Key={"cell": cell, "sk": f"HIST#{rid}"}).get("Item")
        self.assertIsNotNone(hist_item)
        self.assertFalse("expiresAt" in hist_item)

    def test_nearby_and_ttl_filtering(self):
        now = int(time.time())
        # Insert a valid report
        r1 = {
            "key": "photos/r1.jpg",
            "lat": 12.9716,
            "lng": 77.5946,
            "depth_lo": 15.0,
            "depth_hi": 25.0,
            "confidence": 0.8,
            "band": "ankle_deep",
            "flow": "still",
            "verdicts": "{}",
            "image_usable": True,
        }
        rid1 = db.insert(r1)

        # Insert a report with expired TTL (simulating TTL deletion lag)
        cell = geohash.encode(12.9716, 77.5946, 5)
        self.reports_table.put_item(
            Item=dynamo_db._to_dynamo_item({
                "cell": cell,
                "sk": "REPORT#expired_id",
                "id": "expired_id",
                "lat": 12.9716,
                "lng": 77.5946,
                "expiresAt": now - 100,  # Expired in the past
                "created_at": now - 500,
            })
        )

        bbox = (77.55, 12.95, 77.65, 13.00)
        items = db.nearby(bbox)
        ids = [it["id"] for it in items]
        self.assertIn(rid1, ids)
        self.assertNotIn("expired_id", ids)

    def test_changed_since_dynamic_stale_no_write(self):
        now = int(time.time())
        r1 = {
            "key": "photos/r1.jpg",
            "lat": 12.9716,
            "lng": 77.5946,
            "depth_lo": 15.0,
            "depth_hi": 25.0,
            "confidence": 0.8,
            "band": "ankle_deep",
            "flow": "still",
            "verdicts": "{}",
            "image_usable": True,
        }
        rid1 = db.insert(r1)

        bbox = (77.55, 12.95, 77.65, 13.00)
        # Polling since before creation
        changes = db.changed_since(bbox, since_ts=now - 50)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["id"], rid1)

        # Polling since after creation returns nothing
        no_changes = db.changed_since(bbox, since_ts=now + 50)
        self.assertEqual(len(no_changes), 0)

    def test_add_vote_logic(self):
        now = int(time.time())
        # Old photo report (> 10 mins ago) to test cleared override
        r_old = {
            "key": "photos/old.jpg",
            "lat": 12.9716,
            "lng": 77.5946,
            "capturedAt": now - 700,
            "depth_lo": 30.0,
            "depth_hi": 45.0,
            "confidence": 0.8,
            "band": "knee_deep",
            "flow": "still",
            "verdicts": "{}",
            "image_usable": True,
        }
        rid = db.insert(r_old)

        # First vote: still_flooded
        res1 = db.add_vote(rid, "deviceA", "still_flooded")
        self.assertTrue(res1["ok"])
        self.assertFalse(res1["ignored"])

        # Repeat vote from same device should be ignored
        res_repeat = db.add_vote(rid, "deviceA", "still_flooded")
        self.assertTrue(res_repeat["ok"])
        self.assertTrue(res_repeat["ignored"])
        self.assertEqual(res_repeat["reason"], "already_voted")

        # First cleared vote from deviceB: not enough to clear
        res_clr1 = db.add_vote(rid, "deviceB", "cleared")
        self.assertFalse(res_clr1["cleared"])

        # Second cleared vote from deviceC: clears road
        res_clr2 = db.add_vote(rid, "deviceC", "cleared")
        self.assertTrue(res_clr2["cleared"])

        # Verify report is marked cleared in database
        updated_rep = db.get_report(rid)
        self.assertEqual(int(updated_rep["cleared"]), 1)

    def test_daily_analysis_cap(self):
        # Set small cap of 3
        cap = 3
        for _ in range(3):
            db.check_and_increment_daily_cap(max_cap=cap)

        # 4th call should raise HTTPException 429
        with self.assertRaises(HTTPException) as ctx:
            db.check_and_increment_daily_cap(max_cap=cap)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_clear_time_logging(self):
        now = int(time.time())
        log_id = db.log_clear_time(
            road_id="osm-12345",
            lat=12.9716,
            lng=77.5946,
            vehicle="suv",
            issued_at=now,
            earliest="6:30 pm",
            likely="7:00 pm",
            latest="7:30 pm",
            basis="assumed",
        )
        self.assertIsNotNone(log_id)

        logs = db.get_clear_time_logs(limit=10)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["road_id"], "osm-12345")
        self.assertIsNone(logs[0]["actual_cleared_at"])

        # Record cleared
        updated = db.record_actual_cleared(road_id="osm-12345", cleared_at=now + 1800)
        self.assertGreaterEqual(updated, 1)

        logs_after = db.get_clear_time_logs(limit=10)
        self.assertEqual(logs_after[0]["actual_cleared_at"], now + 1800)


class TestBedrockProvider(unittest.TestCase):
    def setUp(self):
        os.environ["LLM_PROVIDER"] = "bedrock"
        os.environ["BEDROCK_MODEL_ID"] = "amazon.nova-lite-v1:0"
        os.environ["BEDROCK_REGION"] = "ap-south-1"
        llm._VLM_CACHE.clear()

    def tearDown(self):
        os.environ["LLM_PROVIDER"] = "ollama"
        llm._VLM_CACHE.clear()

    def test_image_preparation_resizing(self):
        large_img = Image.new("RGB", (2000, 1500), color=(100, 150, 200))
        buf = io.BytesIO()
        large_img.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        prepared = llm._prepare_image_for_bedrock(raw_bytes, max_long_edge=1024)
        out_img = Image.open(io.BytesIO(prepared))
        self.assertLessEqual(max(out_img.size), 1024)

    @patch("boto3.client")
    def test_converse_tool_use_success(self, mock_boto_client):
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "tool_1",
                                "name": "record_flood_observation",
                                "input": {
                                    "image_usable": True,
                                    "reasons_if_unusable": [],
                                    "objects": [
                                        {"type": "car_wheel", "waterline": "at_hub", "confidence": 0.88}
                                    ],
                                    "flow": "still",
                                    "water_clarity": "clear",
                                    "overall_confidence": 0.88,
                                },
                            }
                        }
                    ]
                }
            }
        }

        test_img_bytes = b"\xff\xd8\xff\xe0fakejpegimage"
        result, model = llm.chat_json("Analyze flood", test_img_bytes)

        self.assertEqual(model, "amazon.nova-lite-v1:0")
        self.assertTrue(result["image_usable"])
        self.assertEqual(result["objects"][0]["type"], "car_wheel")
        self.assertEqual(result["flow"], "still")

        mock_client.converse.reset_mock()
        cached_result, cached_model = llm.chat_json("Analyze flood", test_img_bytes)
        self.assertEqual(cached_result, result)
        mock_client.converse.assert_not_called()

    @patch("boto3.client")
    def test_model_access_not_enabled_error(self, mock_boto_client):
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        error_response = {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "You don't have access to the model with the specified model ID.",
            }
        }
        mock_client.converse.side_effect = ClientError(error_response, "Converse")

        test_img_bytes = b"\xff\xd8\xff\xe0fakeuniquebytes"
        with self.assertRaises(HTTPException) as ctx:
            llm.chat_json("Analyze flood", test_img_bytes)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("Bedrock model access is not enabled", ctx.exception.detail)


class TestOpenAICompatProvider(unittest.TestCase):
    def setUp(self):
        os.environ["LLM_PROVIDER"] = "openai_compat"
        os.environ["VISION_API_BASE_URL"] = "https://api.openai.com/v1"
        os.environ["VISION_API_KEY"] = "test-sk-key"
        os.environ["VISION_MODEL"] = "gpt-4o-mini"
        os.environ["DAILY_ANALYSIS_CAP"] = "100"
        llm._VLM_CACHE.clear()

    def tearDown(self):
        os.environ["LLM_PROVIDER"] = "ollama"
        llm._VLM_CACHE.clear()

    @patch("requests.post")
    def test_openai_compat_success_and_caching(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "image_usable": True,
                            "reasons_if_unusable": [],
                            "objects": [
                                {"type": "car_wheel", "waterline": "at_hub", "confidence": 0.92}
                            ],
                            "flow": "still",
                            "water_clarity": "clear",
                            "overall_confidence": 0.92,
                        })
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        img_bytes = b"\xff\xd8\xff\xe0testjpegimage"
        result, model = llm.chat_json("Analyze flood", img_bytes)

        self.assertEqual(model, "gpt-4o-mini")
        self.assertTrue(result["image_usable"])
        self.assertEqual(result["objects"][0]["type"], "car_wheel")

        # Verify payload parameters
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://api.openai.com/v1/chat/completions")
        sent_payload = kwargs["json"]
        self.assertEqual(sent_payload["temperature"], 0.0)
        self.assertEqual(sent_payload["response_format"], {"type": "json_object"})
        self.assertIn("Authorization", kwargs["headers"])
        self.assertEqual(kwargs["timeout"], 25)

        # Verify caching by image hash
        mock_post.reset_mock()
        cached_result, cached_model = llm.chat_json("Analyze flood", img_bytes)
        self.assertEqual(cached_result, result)
        self.assertEqual(cached_model, "gpt-4o-mini")
        mock_post.assert_not_called()

    @patch("requests.post")
    def test_openai_compat_code_fences_stripped(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        fenced_content = """```json
        {
            "image_usable": true,
            "reasons_if_unusable": [],
            "objects": [
                {"type": "curb", "waterline": "submerged", "confidence": 0.85}
            ],
            "flow": "slow",
            "water_clarity": "muddy",
            "overall_confidence": 0.85
        }
        ```"""
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": fenced_content}}]
        }
        mock_post.return_value = mock_resp

        img_bytes = b"\xff\xd8\xff\xe0fencedcontentimage"
        result, _ = llm.chat_json("Analyze flood", img_bytes)
        self.assertTrue(result["image_usable"])
        self.assertEqual(result["objects"][0]["type"], "curb")

    @patch("requests.post")
    def test_openai_compat_invalid_json_503(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "This is totally not JSON at all."}}]
        }
        mock_post.return_value = mock_resp

        img_bytes = b"\xff\xd8\xff\xe0invalidjsonbytes"
        with self.assertRaises(HTTPException) as ctx:
            llm.chat_json("Analyze flood", img_bytes)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("analysis unavailable, retry", ctx.exception.detail)

    @patch("requests.post")
    def test_openai_compat_invalid_schema_503(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Missing 'image_usable' and invalid schema
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"foo": "bar"})}}]
        }
        mock_post.return_value = mock_resp

        img_bytes = b"\xff\xd8\xff\xe0invalidschemabytes"
        with self.assertRaises(HTTPException) as ctx:
            llm.chat_json("Analyze flood", img_bytes)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("analysis unavailable, retry", ctx.exception.detail)

    @patch("time.sleep")
    @patch("requests.post")
    def test_openai_compat_timeout_and_retry(self, mock_post, mock_sleep):
        import requests as req
        mock_post.side_effect = req.exceptions.Timeout("Connection timed out")

        img_bytes = b"\xff\xd8\xff\xe0timeoutbytes"
        with self.assertRaises(HTTPException) as ctx:
            llm.chat_json("Analyze flood", img_bytes)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("analysis unavailable, retry", ctx.exception.detail)
        # Verify exactly one retry occurred (2 calls total)
        self.assertEqual(mock_post.call_count, 2)

    @patch("requests.post")
    def test_openai_compat_cap_exceeded_429(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "image_usable": True,
                            "reasons_if_unusable": [],
                            "objects": [],
                            "flow": "still",
                            "water_clarity": "clear",
                            "overall_confidence": 0.5,
                        })
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        # Set small cap of 2
        os.environ["DAILY_ANALYSIS_CAP"] = "2"
        # Reset local in-memory daily cap for test
        from app.common import db
        db._LOCAL_DAILY_CAPS.clear()

        # 1st call
        llm.chat_json("Analyze 1", b"\xff\xd8\xff\xe0img1")
        # 2nd call
        llm.chat_json("Analyze 2", b"\xff\xd8\xff\xe0img2")

        # 3rd call exceeds cap -> HTTP 429
        with self.assertRaises(HTTPException) as ctx:
            llm.chat_json("Analyze 3", b"\xff\xd8\xff\xe0img3")

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn("cap reached", ctx.exception.detail.lower())
        # mock_post should only have been called twice (for img1 and img2)
        self.assertEqual(mock_post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
