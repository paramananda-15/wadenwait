import io
import os
import uuid
from pathlib import Path
from typing import Optional


ROOT = (
    Path(__file__)
    .resolve()
    .parent
    .parent
    .parent
)

LOCAL_UPLOADS = ROOT / "data" / "uploads"
LOCAL_UPLOADS.mkdir(parents=True, exist_ok=True)


def get_backend() -> str:
    return os.getenv("STORAGE_BACKEND", "local").strip().lower()


def get_bucket_name() -> str:
    return os.getenv("S3_BUCKET") or os.getenv("PHOTO_BUCKET", "wade-or-wait-photos")


def _get_s3_client():
    import boto3
    from botocore.client import Config
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
    endpoint_url = f"https://s3.{region}.amazonaws.com"
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        config=Config(s3={"addressing_style": "virtual"}, signature_version="s3v4"),
    )


def generate_object_key(prefix: str = "photos", extension: str = "jpg") -> str:
    """Generate a random unique object name."""
    clean_prefix = prefix.strip("/")
    clean_ext = extension.lstrip(".")
    return f"{clean_prefix}/{uuid.uuid4().hex}.{clean_ext}"


def save_file(key: str, data: bytes, content_type: str = "image/jpeg") -> str:
    """
    Save bytes to storage (local disk or S3).
    Returns the key.
    """
    backend = get_backend()
    if backend == "s3":
        client = _get_s3_client()
        bucket = get_bucket_name()
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return key
    else:
        filename = Path(key).name
        target_path = LOCAL_UPLOADS / filename
        target_path.write_bytes(data)
        return key


def get_file(key: str) -> bytes:
    """
    Retrieve file bytes from storage.
    Raises FileNotFoundError if not found.
    """
    backend = get_backend()
    if backend == "s3":
        import botocore.exceptions
        client = _get_s3_client()
        bucket = get_bucket_name()
        try:
            resp = client.get_object(Bucket=bucket, Key=key)
            return resp["Body"].read()
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise FileNotFoundError(f"Key {key} not found in S3 bucket {bucket}")
            raise
    else:
        filename = Path(key).name
        target_path = LOCAL_UPLOADS / filename
        if not target_path.exists():
            raise FileNotFoundError(f"File {target_path} not found")
        return target_path.read_bytes()


def file_exists(key: str) -> bool:
    """Check if key exists in storage."""
    backend = get_backend()
    if backend == "s3":
        import botocore.exceptions
        client = _get_s3_client()
        bucket = get_bucket_name()
        try:
            client.head_object(Bucket=bucket, Key=key)
            return True
        except botocore.exceptions.ClientError:
            return False
    else:
        filename = Path(key).name
        return (LOCAL_UPLOADS / filename).exists()


def get_url(key: str, expires_in: int = 3600) -> str:
    """
    Return accessible URL for reading the object:
    - local: /uploads/<filename>
    - s3: presigned GET URL
    """
    backend = get_backend()
    if backend == "s3":
        client = _get_s3_client()
        bucket = get_bucket_name()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )
    else:
        filename = Path(key).name
        return f"/uploads/{filename}"


def get_upload_url(key: str, expires_in: int = 3600) -> str:
    """
    Return URL for uploading the object.
    In local mode, returns '/upload'.
    In S3 mode, returns presigned PUT URL.
    """
    backend = get_backend()
    if backend == "s3":
        client = _get_s3_client()
        bucket = get_bucket_name()
        return client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": "image/jpeg"},
            ExpiresIn=expires_in,
        )
    else:
        return "/upload"
