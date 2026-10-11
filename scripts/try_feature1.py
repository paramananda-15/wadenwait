#!/usr/bin/env python3
"""
Quick CLI test script for Feature 1.
Usage:
    python scripts/try_feature1.py <image_path>
"""
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.common import detector, fusion, geometry, perception


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/try_feature1.py <image_path>")
        sys.exit(1)

    image_path = Path(sys.argv[1]).resolve()
    if not image_path.exists():
        print(f"Error: image not found at {image_path}")
        sys.exit(1)

    print(f"Reading image: {image_path}")
    image_bytes = image_path.read_bytes()

    # 1. Run VLM perception
    print("\n--- 1. VLM Perception ---")
    obs = perception.observe(image_bytes)
    print(f"Image usable: {obs.get('image_usable')}")
    print(f"Detected objects: {obs.get('objects')}")
    print(f"Flow: {obs.get('flow')}")

    if obs.get("image_usable"):
        est = geometry.estimate(obs["objects"], obs["overall_confidence"])
        if est:
            vlm_interval = [est["depth_lo"], est["depth_hi"]]
            vlm_conf = est["conf"]
        else:
            vlm_interval = None
            vlm_conf = 0.0
    else:
        vlm_interval = None
        vlm_conf = 0.0

    print(f"VLM Interval : {vlm_interval} cm (confidence: {vlm_conf})")

    # 2. Run Object Detector & visible-height depth estimate
    print("\n--- 2. Object Detector & Visible-Height Estimate ---")
    det_raw = detector.detect(image_bytes)
    det_est = detector.estimate_depth_visible_height(det_raw)
    det_interval = det_est.get("depth_interval")
    det_conf = det_est.get("confidence", 0.0)

    print(f"Detector Status   : {det_est.get('status')}")
    print(f"Detections count  : {len(det_raw.get('detections', []))}")
    if det_est.get("chosen_detection"):
        chosen = det_est["chosen_detection"]
        print(f"Chosen detection  : {chosen['class']} (score: {chosen['score']})")
        print(f"Box size (px)     : {chosen['box_width_px']} x {chosen['box_height_px']}")
        print(f"Scale             : {chosen['scale_cm_per_px']:.3f} cm/px")
        print(f"Visible height    : {chosen['visible_height_cm']:.1f} cm")
        print(f"Estimated depth   : {chosen['estimated_depth_cm']:.1f} cm")
    else:
        print(f"Diagnostics       : {det_est.get('diagnostics')}")

    print(f"Detector Interval : {det_interval} cm (confidence: {det_conf})")

    # 3. Cross-check
    print("\n--- 3. Cross-Check ---")
    adj_vlm_conf, cross_diag = detector.cross_check_vlm(
        obs.get("objects", []),
        det_raw.get("detections", []),
        vlm_conf,
    )
    if cross_diag.get("penalty_applied"):
        print(f"Penalty applied! VLM confidence adjusted: {vlm_conf} -> {adj_vlm_conf}")
        print(f"Missing references: {cross_diag.get('missing_references')}")
    else:
        print("No cross-check penalty applied.")

    # 4. Fusion
    print("\n--- 4. Fused Result ---")
    modes = ["pedestrian", "two_wheeler", "hatchback", "suv", "ambulance"]
    fused = fusion.fuse(
        vlm_interval=vlm_interval,
        vlm_conf=adj_vlm_conf,
        det_interval=det_interval,
        det_conf=det_conf,
        modes=modes,
        flow=obs.get("flow", "fast"),
    )

    print(f"Agreement         : {fused['agreement']}")
    print(f"Fused Interval    : {fused['fused_interval']} cm")
    print(f"Fused Confidence  : {fused['fused_confidence']}")
    print(f"Verdicts          : {fused['verdicts']}")

    # 5. Write explanation image
    print("\n--- 5. Explanation Image ---")
    out_dir = REPO_ROOT / "data" / "uploads"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"explanation_{image_path.stem}.jpg"
    saved_path, reason = detector.create_explanation_image(
        image_bytes,
        det_est,
        save_path=out_path,
    )
    print(f"Explanation image written to: {saved_path}")
    print(f"Reason: {reason}")


if __name__ == "__main__":
    main()
