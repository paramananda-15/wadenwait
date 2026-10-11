import os
from common import geometry, llm


def _vocab():
    d = {}
    for t, w in geometry.TABLE:
        d.setdefault(t, []).append(w)
    return "\n".join(f"- {t}: waterline is one of {', '.join(ws)}" for t, ws in d.items())


PROMPT = f"""Look at this photo of a flooded street. Do NOT estimate depth in centimetres.
Report only what you can see. Objects you may report (use these exact names):
{_vocab()}
For each visible object give the waterline landmark showing how far up it the water reaches, plus your confidence 0-1.
Also report flow (still, slow or fast) and water_clarity (clear or muddy).
If the photo is unusable (no flood water, too dark, blurred, no reference object), set image_usable to false and list reasons.
Reply with ONLY this JSON:
{{"image_usable": true, "reasons_if_unusable": [], "objects": [{{"type": "...", "waterline": "...", "confidence": 0.0}}], "flow": "still", "water_clarity": "clear", "overall_confidence": 0.0}}"""


def sanitize(raw):
    """Validate model output: unknown objects/landmarks are dropped, numbers clamped.
    Missing/invalid flow defaults to 'fast' (the stricter limits) - err on the danger side."""
    bad = {"image_usable": False, "reasons_if_unusable": ["unreadable model output"], "objects": [],
           "flow": "fast", "water_clarity": "muddy", "overall_confidence": 0.0}
    try:
        clamp = lambda x: max(0.0, min(1.0, float(x)))
        objs = [{"type": o["type"], "waterline": o["waterline"], "confidence": clamp(o.get("confidence", 0))}
                for o in (raw.get("objects") or []) if (o.get("type"), o.get("waterline")) in geometry.TABLE]
        return {"image_usable": bool(raw.get("image_usable", False)),
                "reasons_if_unusable": [str(r)[:120] for r in (raw.get("reasons_if_unusable") or [])[:5]],
                "objects": objs,
                "flow": raw["flow"] if raw.get("flow") in ("still", "slow", "fast") else "fast",
                "water_clarity": "clear" if raw.get("water_clarity") == "clear" else "muddy",
                "overall_confidence": clamp(raw.get("overall_confidence", 0))}
    except Exception:
        return bad


def observe(body, image_bytes=None):
    if os.environ.get("PERCEPTION_MODE", "openrouter") == "stub":
        return sanitize(body.get("debug_perception") or {
            "image_usable": True, "reasons_if_unusable": [],
            "objects": [{"type": "two_wheeler_wheel", "waterline": "at_hub", "confidence": 0.7}],
            "flow": "still", "water_clarity": "muddy", "overall_confidence": 0.7})
    models = [os.environ.get("MODEL_PRIMARY", "openrouter/free"), os.environ.get("MODEL_FALLBACK", "openrouter/free")]
    raw, used = llm.chat_json(PROMPT, image_bytes, models, budget=20)
    print("perception model:", used)
    return sanitize(raw)
