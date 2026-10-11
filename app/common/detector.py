import io
import math
import os
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from PIL import Image, ImageDraw, ImageFont

from .detector_config import (
    REFERENCE_DIMENSIONS,
    DETECTION_THRESHOLDS,
    VLM_TO_DETECTOR_MAP,
)

# Global lazy-loaded model state
_MODEL = None
_CATEGORIES = None
_MODEL_FAILED = False


def _get_model():
    """
    Lazy-load the object detector model.
    Uses torchvision ssdlite320_mobilenet_v3_large with COCO_V1 weights.
    Weights license: PyTorch Torchvision BSD 3-Clause, COCO annotations CC BY 4.0.
    Returns (model, categories) or raises an Exception.
    """
    global _MODEL, _CATEGORIES, _MODEL_FAILED

    if _MODEL_FAILED:
        return None, None

    if _MODEL is not None and _CATEGORIES is not None:
        return _MODEL, _CATEGORIES

    try:
        import torch
        from torchvision.models.detection import (
            ssdlite320_mobilenet_v3_large,
            SSDLite320_MobileNet_V3_Large_Weights,
        )

        weights = SSDLite320_MobileNet_V3_Large_Weights.DEFAULT
        _CATEGORIES = weights.meta["categories"]
        model = ssdlite320_mobilenet_v3_large(weights=weights)
        model.eval()
        _MODEL = model
        return _MODEL, _CATEGORIES
    except Exception as exc:
        print("Warning: Object detector failed to load:", repr(exc))
        _MODEL_FAILED = True
        return None, None


def detect(image_bytes: bytes) -> Dict[str, Any]:
    """
    Run the object detector on image bytes.
    Returns:
      {
        "status": "ok" | "unavailable",
        "detections": [...],
        "image_size": (width, height)
      }
    """
    model, categories = _get_model()
    if model is None or categories is None:
        return {
            "status": "unavailable",
            "detections": [],
            "image_size": (0, 0),
            "reason": "Detector model unavailable or failed to load",
        }

    try:
        import torch
        import torchvision.transforms.functional as TF

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        tensor = TF.to_tensor(img)

        with torch.no_grad():
            outputs = model([tensor])[0]

        boxes = outputs["boxes"].cpu().numpy()
        scores = outputs["scores"].cpu().numpy()
        labels = outputs["labels"].cpu().numpy()

        target_classes = set(REFERENCE_DIMENSIONS.keys())
        min_conf = DETECTION_THRESHOLDS["min_detector_confidence"]

        detections = []
        for box, score, label in zip(boxes, scores, labels):
            score_val = float(score)
            if score_val < min_conf:
                continue

            label_idx = int(label)
            if label_idx < len(categories):
                class_name = categories[label_idx]
            else:
                continue

            if class_name not in target_classes:
                continue

            x1, y1, x2, y2 = [float(v) for v in box]
            detections.append({
                "class": class_name,
                "score": round(score_val, 3),
                "box": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                "box_width_px": round(max(0.0, x2 - x1), 1),
                "box_height_px": round(max(0.0, y2 - y1), 1),
            })

        return {
            "status": "ok",
            "detections": detections,
            "image_size": (w, h),
        }

    except Exception as exc:
        print("Detector inference error:", repr(exc))
        return {
            "status": "unavailable",
            "detections": [],
            "image_size": (0, 0),
            "reason": f"Detector inference failed: {str(exc)[:120]}",
        }


def check_abstain(
    box: List[float],
    class_name: str,
    img_w: int,
    img_h: int,
) -> Optional[str]:
    """
    Check if a detection box should be abstained.
    Returns None if valid, or a string describing the abstain reason.
    Abstains on:
    1. Boxes touching the image edge.
    2. Tiny boxes.
    3. Oblique / non-canonical views.
    """
    if class_name not in REFERENCE_DIMENSIONS:
        return "unsupported_class"

    cfg = REFERENCE_DIMENSIONS[class_name]
    x1, y1, x2, y2 = box
    box_w = x2 - x1
    box_h = y2 - y1

    if box_w <= 0 or box_h <= 0 or img_w <= 0 or img_h <= 0:
        return "invalid_dimensions"

    edge_margin = DETECTION_THRESHOLDS["edge_margin_px"]

    # 1. Image edge check (boundary clipping)
    if (
        x1 <= edge_margin
        or y1 <= edge_margin
        or x2 >= (img_w - edge_margin)
        or y2 >= (img_h - edge_margin)
    ):
        return "box_touches_image_edge"

    # 2. Tiny box check
    min_w = DETECTION_THRESHOLDS["min_box_width_px"]
    min_h = DETECTION_THRESHOLDS["min_box_height_px"]
    min_area_ratio = DETECTION_THRESHOLDS["min_box_area_ratio"]

    box_area = box_w * box_h
    img_area = img_w * img_h

    if box_w < min_w or box_h < min_h or (box_area / img_area) < min_area_ratio:
        return "box_too_small"

    # 3. Oblique view check using canonical aspect ratio
    aspect_ratio = box_w / box_h
    min_ar, max_ar = cfg["aspect_ratio_range"]
    if aspect_ratio < min_ar or aspect_ratio > max_ar:
        return f"oblique_or_noncanonical_view (aspect ratio {aspect_ratio:.2f} not in [{min_ar}, {max_ar}])"

    return None


def estimate_depth_visible_height(
    detection_result: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Estimate flood water depth using the visible-height method.
    scale = known_width / box_width_px
    visible_height = box_height_px * scale
    depth = known_full_height - visible_height
    """
    status = detection_result.get("status", "unavailable")
    if status != "ok":
        return {
            "status": "unavailable",
            "depth_lo": None,
            "depth_hi": None,
            "depth_interval": None,
            "confidence": 0.0,
            "diagnostics": {
                "reason": detection_result.get("reason", "Detector unavailable"),
                "detections_count": 0,
            },
            "chosen_detection": None,
        }

    detections = detection_result.get("detections", [])
    img_w, img_h = detection_result.get("image_size", (0, 0))

    if not detections:
        return {
            "status": "abstain",
            "depth_lo": None,
            "depth_hi": None,
            "depth_interval": None,
            "confidence": 0.0,
            "diagnostics": {
                "reason": "no_target_objects_detected",
                "detections_count": 0,
            },
            "chosen_detection": None,
        }

    evaluated = []
    candidates = []

    for d in detections:
        cls_name = d["class"]
        box = d["box"]
        abstain_reason = check_abstain(box, cls_name, img_w, img_h)

        if abstain_reason:
            evaluated.append({
                "class": cls_name,
                "box": box,
                "score": d["score"],
                "abstain_reason": abstain_reason,
            })
            continue

        cfg = REFERENCE_DIMENSIONS[cls_name]
        known_w = cfg["known_width"]
        known_h = cfg["known_full_height"]
        box_w = max(1.0, box[2] - box[0])
        box_h = max(1.0, box[3] - box[1])

        scale = known_w / box_w  # cm per pixel
        visible_h = box_h * scale  # cm
        raw_depth = known_h - visible_h  # cm

        # Depth cannot be less than 0
        depth_cm = max(0.0, raw_depth)

        # Build conservative depth interval
        # Uncertainty band based on physical variance and resolution
        margin = max(6.0, known_h * 0.12)
        lo = max(0.0, round(depth_cm - margin, 1))
        hi = min(known_h, max(lo + 5.0, round(depth_cm + margin, 1)))

        # Confidence heuristic from detector score and size
        conf = float(d["score"])
        # Slight penalty if box is on the smaller side
        if box_w < 60:
            conf *= 0.85
        conf = max(0.2, min(0.95, round(conf, 2)))

        candidate_info = {
            "class": cls_name,
            "score": d["score"],
            "box": box,
            "box_width_px": round(box_w, 1),
            "box_height_px": round(box_h, 1),
            "scale_cm_per_px": round(scale, 3),
            "visible_height_cm": round(visible_h, 1),
            "estimated_depth_cm": round(depth_cm, 1),
            "depth_interval": [lo, hi],
            "confidence": conf,
            "known_width": known_w,
            "known_full_height": known_h,
        }

        candidates.append(candidate_info)
        evaluated.append(candidate_info)

    if not candidates:
        return {
            "status": "abstain",
            "depth_lo": None,
            "depth_hi": None,
            "depth_interval": None,
            "confidence": 0.0,
            "diagnostics": {
                "reason": "all_detections_abstained",
                "evaluated": evaluated,
            },
            "chosen_detection": None,
        }

    # Pick the detection with the highest confidence
    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    best = candidates[0]

    return {
        "status": "ok",
        "depth_lo": best["depth_interval"][0],
        "depth_hi": best["depth_interval"][1],
        "depth_interval": best["depth_interval"],
        "confidence": best["confidence"],
        "diagnostics": {
            "reason": "visible_height_calculated",
            "chosen_class": best["class"],
            "scale_cm_per_px": best["scale_cm_per_px"],
            "visible_height_cm": best["visible_height_cm"],
            "estimated_depth_cm": best["estimated_depth_cm"],
            "evaluated_count": len(evaluated),
            "valid_candidates_count": len(candidates),
        },
        "chosen_detection": best,
        "all_detections": detections,
    }


def cross_check_vlm(
    vlm_objects: List[Dict[str, Any]],
    detections: List[Dict[str, Any]],
    base_vlm_confidence: float,
) -> Tuple[float, Dict[str, Any]]:
    """
    Cross-check: if the vision-LLM names a reference object that the detector
    does not find, reduce confidence.
    """
    detected_classes = {
        d["class"] for d in detections
        if d.get("score", 0) >= DETECTION_THRESHOLDS["min_detector_confidence"]
    }

    missing_refs = []
    for obj in vlm_objects:
        obj_type = obj.get("type", "")
        expected_detector_classes = VLM_TO_DETECTOR_MAP.get(obj_type)
        if expected_detector_classes:
            found = any(c in detected_classes for c in expected_detector_classes)
            if not found:
                missing_refs.append({
                    "vlm_object": obj_type,
                    "expected": expected_detector_classes,
                })

    adjusted_confidence = base_vlm_confidence
    if missing_refs and detected_classes:
        # Detector ran and found other objects, but completely missed VLM's reference object
        penalty = 0.75
        adjusted_confidence = max(0.1, round(base_vlm_confidence * penalty, 2))

    return adjusted_confidence, {
        "missing_references": missing_refs,
        "detector_classes": list(detected_classes),
        "penalty_applied": bool(missing_refs and detected_classes),
    }


def create_explanation_image(
    image_bytes: bytes,
    det_result: Dict[str, Any],
    save_path: Optional[Path] = None,
) -> Tuple[Optional[str], str]:
    """
    Draw boxes, labels, and an explanatory caption banner on the photo, save it,
    and return (saved_file_url, one_sentence_reason).
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size

        chosen = det_result.get("chosen_detection")
        all_dets = det_result.get("all_detections") or []
        depth_int = det_result.get("depth_interval")

        # Try to load a font, fallback to default
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

        # Draw all detections lightly
        for d in all_dets:
            b = d.get("box")
            if not b:
                continue
            draw.rectangle(
                [b[0], b[1], b[2], b[3]],
                outline=(100, 180, 255),
                width=2,
            )

        caption = "Flood observation image."

        if chosen:
            box = chosen["box"]
            cls_name = chosen["class"].capitalize()
            depth_cm = chosen["estimated_depth_cm"]
            scale = chosen["scale_cm_per_px"]

            # Highlight chosen detection in bright emerald/amber
            draw.rectangle(
                [box[0], box[1], box[2], box[3]],
                outline=(34, 197, 94),
                width=4,
            )

            # Label near box
            lbl = f"{cls_name} (scale: {scale:.1f} cm/px)"
            draw.rectangle(
                [box[0], max(0, box[1] - 18), box[0] + len(lbl) * 7, box[1]],
                fill=(34, 197, 94),
            )
            draw.text((box[0] + 3, max(0, box[1] - 16)), lbl, fill=(0, 0, 0), font=font)

            caption = (
                f"{cls_name} width used as scale; "
                f"~{int(depth_cm)} cm of the {cls_name.lower()} is under water."
            )
        else:
            reason = det_result.get("diagnostics", {}).get("reason", "no suitable reference object detected")
            caption = f"Detector abstained ({reason}); relying on reference landmarks."

        # Draw top banner for caption
        banner_height = 36
        overlay = Image.new("RGBA", (w, banner_height), (12, 12, 12, 210))
        img.paste(overlay, (0, 0), overlay)

        draw = ImageDraw.Draw(img)
        draw.text((10, 10), caption, fill=(245, 245, 245), font=font)

        if save_path:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(save_path, "JPEG", quality=88)
            return str(save_path), caption

        return None, caption

    except Exception as exc:
        print("Failed to create explanation image:", repr(exc))
        return None, "Explanation image generation failed."
