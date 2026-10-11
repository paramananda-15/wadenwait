from typing import Dict, List, Optional, Tuple, Any

from . import safety

VERDICT_STRICTNESS = {
    "DO_NOT_CROSS": 3,
    "CAUTION": 2,
    "GO": 1,
    "UNKNOWN": 0,
}


def _stricter_verdict(v1: str, v2: str) -> str:
    s1 = VERDICT_STRICTNESS.get(v1, 0)
    s2 = VERDICT_STRICTNESS.get(v2, 0)
    return v1 if s1 >= s2 else v2


def fuse(
    vlm_interval: Optional[List[float]],
    vlm_conf: float,
    det_interval: Optional[List[float]],
    det_conf: float,
    modes: List[str],
    flow: str = "still",
) -> Dict[str, Any]:
    """
    Fuse VLM depth interval and detector depth interval.

    Rules:
    - Intervals overlap -> intersect and raise confidence;
    - No overlap -> union, lower confidence, use the stricter verdict;
    - One source unavailable -> fall back to the other with a confidence penalty.

    Returns:
      {
        "fused_interval": [lo, hi] or None,
        "fused_confidence": float,
        "agreement": "agree" | "disagree" | "detector_unavailable",
        "verdicts": {mode: "GO" | "CAUTION" | "DO_NOT_CROSS" | "UNKNOWN"},
        "vlm_interval": vlm_interval,
        "detector_interval": det_interval,
      }
    """
    has_vlm = vlm_interval is not None and len(vlm_interval) == 2
    has_det = det_interval is not None and len(det_interval) == 2

    # Case 1: Both sources available
    if has_vlm and has_det:
        lo_v, hi_v = float(vlm_interval[0]), float(vlm_interval[1])
        lo_d, hi_d = float(det_interval[0]), float(det_interval[1])

        # Check overlap
        inter_lo = max(lo_v, lo_d)
        inter_hi = min(hi_v, hi_d)
        is_overlap = inter_lo <= inter_hi

        if is_overlap:
            agreement = "agree"
            fused_interval = [round(inter_lo, 1), round(inter_hi, 1)]
            # Raise confidence on agreement
            fused_conf = min(1.0, round(max(vlm_conf, det_conf) + 0.10, 2))

            verdicts = {}
            for mode in modes:
                verdicts[mode] = safety.verdict(
                    fused_interval[0],
                    fused_interval[1],
                    fused_conf,
                    mode,
                    flow,
                )

        else:
            agreement = "disagree"
            # Union of intervals
            union_lo = min(lo_v, lo_d)
            union_hi = max(hi_v, hi_d)
            fused_interval = [round(union_lo, 1), round(union_hi, 1)]
            # Lower confidence on disagreement
            fused_conf = max(0.15, round(min(vlm_conf, det_conf) * 0.70, 2))

            verdicts = {}
            for mode in modes:
                v_vlm = safety.verdict(lo_v, hi_v, vlm_conf, mode, flow)
                v_det = safety.verdict(lo_d, hi_d, det_conf, mode, flow)
                v_union = safety.verdict(union_lo, union_hi, fused_conf, mode, flow)
                # Pick the strictest verdict among VLM, detector, and union
                verdicts[mode] = _stricter_verdict(
                    _stricter_verdict(v_vlm, v_det),
                    v_union,
                )

    # Case 2: One source unavailable (detector unavailable or abstained)
    elif has_vlm and not has_det:
        agreement = "detector_unavailable"
        fused_interval = [round(float(vlm_interval[0]), 1), round(float(vlm_interval[1]), 1)]
        # Confidence penalty for single source
        fused_conf = max(0.10, round(vlm_conf * 0.85, 2))

        verdicts = {}
        for mode in modes:
            verdicts[mode] = safety.verdict(
                fused_interval[0],
                fused_interval[1],
                fused_conf,
                mode,
                flow,
            )

    # Case 3: VLM unusable, detector available
    elif has_det and not has_vlm:
        agreement = "detector_unavailable"
        fused_interval = [round(float(det_interval[0]), 1), round(float(det_interval[1]), 1)]
        # Confidence penalty for single source
        fused_conf = max(0.10, round(det_conf * 0.85, 2))

        verdicts = {}
        for mode in modes:
            verdicts[mode] = safety.verdict(
                fused_interval[0],
                fused_interval[1],
                fused_conf,
                mode,
                flow,
            )

    # Case 4: Neither source available
    else:
        agreement = "detector_unavailable"
        fused_interval = None
        fused_conf = 0.0
        verdicts = {mode: "UNKNOWN" for mode in modes}

    return {
        "fused_interval": fused_interval,
        "fused_confidence": fused_conf,
        "agreement": agreement,
        "verdicts": verdicts,
        "vlm_interval": vlm_interval,
        "detector_interval": det_interval,
    }
