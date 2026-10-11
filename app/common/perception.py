import os

from fastapi import HTTPException

from . import geometry
from . import llm


def _vocab():
    d = {}
    for object_type, waterline in geometry.TABLE:
        d.setdefault(object_type, []).append(waterline)

    return "\n".join(
        f"- {object_type}: waterline is one of {', '.join(values)}"
        for object_type, values in d.items()
    )


PROMPT = f"""You are the flood-scene perception module for a road safety application.

Analyze the supplied photograph itself. Do not use a fixed/default answer and do not guess a water depth in centimetres.

Your job is to identify visible physical reference objects and determine where the waterline reaches them.

Allowed object names and allowed waterline landmarks:
{_vocab()}

For every object you are genuinely able to see:
- use one of the exact object names above
- use one of the exact waterline landmarks above
- give a confidence from 0.0 to 1.0 based on how clearly the photograph supports that observation

Confidence rules:
- 0.90-1.00: landmark is very clear and unambiguous
- 0.75-0.89: clear but with some uncertainty
- 0.50-0.74: partially visible or moderately uncertain
- below 0.50: weak evidence
Do not repeatedly use the same confidence value unless the visual evidence genuinely supports it.

Also classify:
- flow: still, slow, or fast
- water_clarity: clear or muddy

If there is no usable flood-water evidence, no suitable reference object, the image is too dark, badly blurred, or otherwise unreliable, set image_usable to false and explain why.

Important: report only what is visually supported by this particular image. Never invent a car, wheel, curb, cone, or waterline that is not visible.

Reply with ONLY valid JSON in this exact structure:
{{
  "image_usable": true,
  "reasons_if_unusable": [],
  "objects": [
    {{"type": "car_wheel", "waterline": "at_hub", "confidence": 0.82}}
  ],
  "flow": "slow",
  "water_clarity": "muddy",
  "overall_confidence": 0.82
}}"""


def _clamp(value):
    return max(0.0, min(1.0, float(value)))


def sanitize(raw):
    bad = {
        "image_usable": False,
        "reasons_if_unusable": ["unreadable model output"],
        "objects": [],
        "flow": "still",
        "water_clarity": "muddy",
        "overall_confidence": 0.0,
        "model": os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b"),
        "mode": "ollama",
    }

    try:
        if not isinstance(raw, dict):
            return bad

        allowed = set(geometry.TABLE)
        objects = []

        for obj in raw.get("objects") or []:
            if not isinstance(obj, dict):
                continue

            key = (
                obj.get("type"),
                obj.get("waterline"),
            )

            if key not in allowed:
                continue

            try:
                confidence = _clamp(
                    obj.get("confidence", 0)
                )
            except Exception:
                confidence = 0.0

            objects.append({
                "type": obj["type"],
                "waterline": obj["waterline"],
                "confidence": confidence,
            })

        try:
            overall_confidence = _clamp(
                raw.get("overall_confidence", 0)
            )
        except Exception:
            overall_confidence = 0.0

        return {
            "image_usable": bool(
                raw.get("image_usable", False)
            ),
            "reasons_if_unusable": [
                str(reason)[:160]
                for reason in (
                    raw.get("reasons_if_unusable") or []
                )[:5]
            ],
            "objects": objects,
            "flow": (
                raw.get("flow")
                if raw.get("flow") in (
                    "still",
                    "slow",
                    "fast",
                )
                else "still"
            ),
            "water_clarity": (
                "clear"
                if raw.get("water_clarity") == "clear"
                else "muddy"
            ),
            "overall_confidence": overall_confidence,
            "model": os.getenv(
                "OLLAMA_MODEL",
                "qwen2.5vl:7b",
            ),
            "mode": "ollama",
        }

    except Exception:
        return bad


def observe(image_bytes):
    mode = os.getenv(
        "PERCEPTION_MODE",
        "ollama",
    ).strip().lower()

    if mode == "stub":
        print(
            "WARNING: PERCEPTION_MODE=stub; "
            "image is NOT being analyzed by Ollama."
        )
        return {
            "image_usable": True,
            "reasons_if_unusable": [],
            "objects": [{
                "type": "two_wheeler_wheel",
                "waterline": "at_hub",
                "confidence": 0.75,
            }],
            "flow": "slow",
            "water_clarity": "muddy",
            "overall_confidence": 0.75,
            "model": "stub",
            "mode": "stub",
        }

    try:
        raw, used_model = llm.chat_json(
            PROMPT,
            image_bytes,
        )

        result = sanitize(raw)
        result["model"] = used_model
        result["mode"] = "ollama"

        print(
            "perception model:",
            used_model,
        )
        print(
            "perception objects:",
            result["objects"],
        )
        print(
            "perception confidence:",
            result["overall_confidence"],
        )

        return result

    except HTTPException:
        raise
    except Exception as exc:
        print(
            "Ollama perception error:",
            repr(exc),
        )

        return {
            "image_usable": False,
            "reasons_if_unusable": [
                "Ollama vision analysis failed: "
                + str(exc)[:180]
            ],
            "objects": [],
            "flow": "still",
            "water_clarity": "muddy",
            "overall_confidence": 0.0,
            "model": os.getenv(
                "OLLAMA_MODEL",
                "qwen2.5vl:7b",
            ),
            "mode": "ollama",
        }
