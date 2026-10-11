import unittest
from app.common import detector, fusion
from app.common.detector_config import REFERENCE_DIMENSIONS


class TestVisibleHeightEstimator(unittest.TestCase):
    def test_normal_car_depth_calculation(self):
        # Image size 800x600. Car centered, box 178 px wide by 100 px high.
        # Known car: width = 178 cm, full height = 148 cm.
        # Scale = 178 cm / 178 px = 1.0 cm/px.
        # Visible height = 100 px * 1.0 cm/px = 100.0 cm.
        # Raw depth = 148 - 100 = 48.0 cm.
        det_result = {
            "status": "ok",
            "image_size": (800, 600),
            "detections": [{
                "class": "car",
                "score": 0.88,
                "box": [300.0, 200.0, 478.0, 300.0],
                "box_width_px": 178.0,
                "box_height_px": 100.0,
            }],
        }
        res = detector.estimate_depth_visible_height(det_result)
        self.assertEqual(res["status"], "ok")
        chosen = res["chosen_detection"]
        self.assertIsNotNone(chosen)
        self.assertAlmostEqual(chosen["scale_cm_per_px"], 1.0, places=1)
        self.assertAlmostEqual(chosen["visible_height_cm"], 100.0, places=1)
        self.assertAlmostEqual(chosen["estimated_depth_cm"], 48.0, places=1)
        self.assertIsNotNone(res["depth_interval"])
        lo, hi = res["depth_interval"]
        self.assertLessEqual(lo, 48.0)
        self.assertGreaterEqual(hi, 48.0)
        self.assertGreater(res["confidence"], 0.5)

    def test_abstain_box_touches_image_edge(self):
        # Box touching left edge (x1 = 0)
        det_result = {
            "status": "ok",
            "image_size": (800, 600),
            "detections": [{
                "class": "car",
                "score": 0.90,
                "box": [0.0, 100.0, 200.0, 250.0],
                "box_width_px": 200.0,
                "box_height_px": 150.0,
            }],
        }
        res = detector.estimate_depth_visible_height(det_result)
        self.assertEqual(res["status"], "abstain")
        self.assertIsNone(res["depth_interval"])
        eval_dets = res["diagnostics"]["evaluated"]
        self.assertEqual(eval_dets[0]["abstain_reason"], "box_touches_image_edge")

    def test_abstain_box_too_small(self):
        # Box width 15 px, height 12 px (tiny)
        det_result = {
            "status": "ok",
            "image_size": (800, 600),
            "detections": [{
                "class": "car",
                "score": 0.85,
                "box": [100.0, 100.0, 115.0, 112.0],
                "box_width_px": 15.0,
                "box_height_px": 12.0,
            }],
        }
        res = detector.estimate_depth_visible_height(det_result)
        self.assertEqual(res["status"], "abstain")
        self.assertIsNone(res["depth_interval"])
        eval_dets = res["diagnostics"]["evaluated"]
        self.assertEqual(eval_dets[0]["abstain_reason"], "box_too_small")

    def test_abstain_oblique_view(self):
        # Car side view: width 400px, height 100px -> aspect ratio 4.0 (normal front is ~0.65-1.45)
        det_result = {
            "status": "ok",
            "image_size": (800, 600),
            "detections": [{
                "class": "car",
                "score": 0.89,
                "box": [50.0, 100.0, 450.0, 200.0],
                "box_width_px": 400.0,
                "box_height_px": 100.0,
            }],
        }
        res = detector.estimate_depth_visible_height(det_result)
        self.assertEqual(res["status"], "abstain")
        self.assertIsNone(res["depth_interval"])
        eval_dets = res["diagnostics"]["evaluated"]
        self.assertIn("oblique", eval_dets[0]["abstain_reason"])


class TestFusionRules(unittest.TestCase):
    def setUp(self):
        self.modes = ["pedestrian", "two_wheeler", "hatchback", "suv", "ambulance"]

    def test_overlapping_intervals_intersect_and_raise_confidence(self):
        # VLM: [15, 30] conf 0.70
        # Det: [20, 40] conf 0.75
        # Overlap is [20, 30]
        res = fusion.fuse(
            vlm_interval=[15.0, 30.0],
            vlm_conf=0.70,
            det_interval=[20.0, 40.0],
            det_conf=0.75,
            modes=self.modes,
        )
        self.assertEqual(res["agreement"], "agree")
        self.assertEqual(res["fused_interval"], [20.0, 30.0])
        # Confidence must be raised beyond max(0.70, 0.75)
        self.assertGreater(res["fused_confidence"], 0.75)

    def test_non_overlapping_intervals_union_and_stricter_verdict(self):
        # VLM shallow: [5, 10] conf 0.80 -> Hatchback limit is 25cm -> GO
        # Det deep: [30, 45] conf 0.75 -> Hatchback limit is 25cm -> DO_NOT_CROSS
        res = fusion.fuse(
            vlm_interval=[5.0, 10.0],
            vlm_conf=0.80,
            det_interval=[30.0, 45.0],
            det_conf=0.75,
            modes=self.modes,
            flow="still",
        )
        self.assertEqual(res["agreement"], "disagree")
        self.assertEqual(res["fused_interval"], [5.0, 45.0])
        # Confidence must be lowered
        self.assertLess(res["fused_confidence"], 0.75)
        # Stricter verdict for hatchback: DO_NOT_CROSS takes precedence over GO
        self.assertEqual(res["verdicts"]["hatchback"], "DO_NOT_CROSS")

    def test_detector_unavailable_fallback_penalty(self):
        # Detector unavailable
        res = fusion.fuse(
            vlm_interval=[12.0, 22.0],
            vlm_conf=0.80,
            det_interval=None,
            det_conf=0.0,
            modes=self.modes,
        )
        self.assertEqual(res["agreement"], "detector_unavailable")
        self.assertEqual(res["fused_interval"], [12.0, 22.0])
        # Confidence penalized
        self.assertLess(res["fused_confidence"], 0.80)

    def test_cross_check_reduces_vlm_confidence(self):
        vlm_objects = [{"type": "car_wheel", "waterline": "at_hub", "confidence": 0.85}]
        # Detector found person, but no car
        detections = [{"class": "person", "score": 0.90, "box": [50, 50, 100, 200]}]
        adj_conf, diag = detector.cross_check_vlm(vlm_objects, detections, base_vlm_confidence=0.85)
        self.assertTrue(diag["penalty_applied"])
        self.assertLess(adj_conf, 0.85)


if __name__ == "__main__":
    unittest.main()
