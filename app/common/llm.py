import base64
import hashlib
import io
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

import requests
from fastapi import HTTPException
from PIL import Image

from . import db


_VLM_CACHE: Dict[str, Dict[str, Any]] = {}


def _get_image_hash(image_bytes: bytes) -> str:
    """Compute SHA-256 hash of image bytes for caching."""
    return hashlib.sha256(image_bytes).hexdigest()


def _prepare_image_for_bedrock(image_bytes: bytes, max_long_edge: int = 1024, quality: int = 85) -> bytes:
    """
    Resize image so long edge is at most ~1024 px, and compress to JPEG.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > max_long_edge:
            scale = max_long_edge / float(long_edge)
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception:
        return image_bytes


def _clean_code_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _validate_perception_schema(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Model output is not a JSON object")
    if "image_usable" not in data:
        raise ValueError("Missing 'image_usable' in model output")
    if not isinstance(data["image_usable"], bool):
        if isinstance(data["image_usable"], str):
            data["image_usable"] = data["image_usable"].lower() in ("true", "1")
        else:
            raise ValueError("'image_usable' must be boolean")
    if not isinstance(data.get("objects", []), list):
        raise ValueError("'objects' must be a list")
    for obj in data.get("objects", []):
        if not isinstance(obj, dict):
            raise ValueError("Each item in 'objects' must be a dictionary")
        if "type" not in obj or "waterline" not in obj:
            raise ValueError("Object in 'objects' must contain 'type' and 'waterline'")
    return data


def _extract_json(text: Any) -> Dict[str, Any]:
    if isinstance(text, dict):
        return text

    text = (text or "").strip()

    try:
        return json.loads(text)
    except Exception:
        match = re.search(
            r"\{.*\}",
            text,
            re.S,
        )

        if not match:
            raise ValueError(
                "Model did not return JSON"
            )

        return json.loads(match.group(0))


def _chat_openai_compat(
    prompt: str,
    image_bytes: bytes,
    model: Optional[str] = None,
    timeout: int = 25,
) -> Tuple[Dict[str, Any], str]:
    api_key = os.getenv("VISION_API_KEY", "").strip()
    base_url = os.getenv("VISION_API_BASE_URL", "").strip().rstrip("/")
    model_name = model or os.getenv("VISION_MODEL", "gpt-4o-mini").strip()

    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="analysis unavailable, retry: VISION_API_KEY environment variable is missing",
        )
    if not base_url:
        raise HTTPException(
            status_code=503,
            detail="analysis unavailable, retry: VISION_API_BASE_URL environment variable is missing",
        )

    endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"

    # Resize/compress image before sending
    compressed_image = _prepare_image_for_bedrock(image_bytes, max_long_edge=1024)
    b64_image = base64.b64encode(compressed_image).decode("ascii")
    data_url = f"data:image/jpeg;base64,{b64_image}"

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_url,
                        },
                    },
                ],
            }
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }

    # NEVER log api_key
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    last_error_reason = ""
    # Timeout 25s, at most one retry (2 attempts max)
    for attempt in range(2):
        try:
            resp = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            if resp.status_code == 200:
                resp_json = resp.json()
                choices = resp_json.get("choices", [])
                if not choices:
                    raise ValueError("Vision API returned empty choices")
                content = choices[0].get("message", {}).get("content", "")
                cleaned = _clean_code_fences(content)
                parsed = _extract_json(cleaned)
                validated = _validate_perception_schema(parsed)
                return validated, model_name

            elif resp.status_code in (429, 500, 502, 503, 504):
                last_error_reason = f"HTTP {resp.status_code}"
                if attempt == 0:
                    time.sleep(1)
                    continue
                break
            else:
                last_error_reason = f"HTTP {resp.status_code}"
                break

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error_reason = f"timeout or connection error ({type(exc).__name__})"
            if attempt == 0:
                time.sleep(1)
                continue
            break
        except Exception as exc:
            last_error_reason = f"invalid response ({str(exc)})"
            break

    raise HTTPException(
        status_code=503,
        detail=f"analysis unavailable, retry ({last_error_reason})",
    )


def _chat_bedrock(prompt: str, image_bytes: bytes, model: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    import boto3
    from botocore.exceptions import ClientError

    region = os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
    model_id = model or os.getenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")

    # Resize/compress image before sending
    compressed_image = _prepare_image_for_bedrock(image_bytes, max_long_edge=1024)

    client = boto3.client("bedrock-runtime", region_name=region)

    tool_config = {
        "tools": [{
            "toolSpec": {
                "name": "record_flood_observation",
                "description": "Record flood-scene visual perception observations as structured JSON",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "image_usable": {"type": "boolean"},
                            "reasons_if_unusable": {"type": "array", "items": {"type": "string"}},
                            "objects": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string"},
                                        "waterline": {"type": "string"},
                                        "confidence": {"type": "number"},
                                    },
                                    "required": ["type", "waterline", "confidence"],
                                },
                            },
                            "flow": {"type": "string", "enum": ["still", "slow", "fast"]},
                            "water_clarity": {"type": "string", "enum": ["clear", "muddy"]},
                            "overall_confidence": {"type": "number"},
                        },
                        "required": ["image_usable", "objects", "flow", "water_clarity", "overall_confidence"],
                    },
                },
            },
        }],
        "toolChoice": {
            "tool": {
                "name": "record_flood_observation",
            },
        },
    }

    messages = [{
        "role": "user",
        "content": [
            {
                "image": {
                    "format": "jpeg",
                    "source": {"bytes": compressed_image},
                },
            },
            {
                "text": prompt,
            },
        ],
    }]

    try:
        try:
            # First attempt with structured tool use
            resp = client.converse(
                modelId=model_id,
                messages=messages,
                inferenceConfig={"temperature": 0.0, "maxTokens": 1024},
                toolConfig=tool_config,
            )
        except ClientError as ce:
            # If model does not support tool use, fallback to plain text with temperature 0
            err_code = ce.response.get("Error", {}).get("Code")
            if err_code in ("ValidationException", "ModelNotImplementedException"):
                resp = client.converse(
                    modelId=model_id,
                    messages=messages,
                    inferenceConfig={"temperature": 0.0, "maxTokens": 1024},
                )
            else:
                raise

        content_blocks = resp.get("output", {}).get("message", {}).get("content", [])
        for block in content_blocks:
            if "toolUse" in block:
                input_data = block["toolUse"].get("input")
                if input_data:
                    return _extract_json(input_data), model_id
            elif "text" in block:
                return _extract_json(block["text"]), model_id

        raise ValueError("Bedrock model returned empty content")

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        if code in ("AccessDeniedException", "ResourceNotFoundException", "UnrecognizedClientException"):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"AWS Bedrock model access is not enabled or credentials lack permissions for "
                    f"'{model_id}' in region '{region}'. Please enable model access in the AWS Bedrock "
                    f"Console -> Model access. ({code}: {msg})"
                ),
            )
        raise HTTPException(
            status_code=502,
            detail=f"Bedrock Converse API call failed ({code}): {msg}",
        )


def _chat_ollama(prompt: str, image_bytes: bytes, model: Optional[str] = None, budget: int = 120) -> Tuple[Dict[str, Any], str]:
    base = os.getenv(
        "OLLAMA_URL",
        "http://127.0.0.1:11434",
    ).rstrip("/")

    model = model or os.getenv(
        "OLLAMA_MODEL",
        "qwen2.5vl:7b",
    )

    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": prompt,
            "images": [
                base64.b64encode(
                    image_bytes
                ).decode("ascii")
            ],
        }],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.2,
        },
    }

    response = requests.post(
        f"{base}/api/chat",
        json=payload,
        timeout=budget,
    )

    response.raise_for_status()

    data = response.json()

    content = (
        data.get("message", {})
        .get("content", "")
    )

    return _extract_json(content), model


def chat_json(
    prompt: str,
    image_bytes: bytes,
    model: Optional[str] = None,
    budget: int = 120,
) -> Tuple[Dict[str, Any], str]:
    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()

    # Cache VLM results by image hash
    img_hash = _get_image_hash(image_bytes)
    if img_hash in _VLM_CACHE:
        cached_result, cached_model = _VLM_CACHE[img_hash]
        return cached_result, cached_model

    if provider == "openai_compat":
        # Check and increment global atomic daily analysis cap (default 100 for openai_compat)
        daily_cap = int(os.getenv("DAILY_ANALYSIS_CAP", 100))
        db.check_and_increment_daily_cap(max_cap=daily_cap)

        result, used_model = _chat_openai_compat(prompt, image_bytes, model=model)
        _VLM_CACHE[img_hash] = (result, used_model)
        return result, used_model

    elif provider == "bedrock":
        # Check and increment global atomic daily analysis cap
        daily_cap = int(os.getenv("DAILY_ANALYSIS_CAP", 300))
        db.check_and_increment_daily_cap(max_cap=daily_cap)

        result, used_model = _chat_bedrock(prompt, image_bytes, model=model)
        _VLM_CACHE[img_hash] = (result, used_model)
        return result, used_model
    else:
        result, used_model = _chat_ollama(prompt, image_bytes, model=model, budget=budget)
        _VLM_CACHE[img_hash] = (result, used_model)
        return result, used_model
