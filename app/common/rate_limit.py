import time
from collections import defaultdict
from typing import Dict, List
from fastapi import HTTPException

# Default rate limits: 5 votes per minute per device, 10 votes per minute per IP
RATE_LIMIT_DEVICE_PER_MINUTE = 5
RATE_LIMIT_IP_PER_MINUTE = 10
RATE_LIMIT_WINDOW_SECONDS = 60

_device_requests: Dict[str, List[float]] = defaultdict(list)
_ip_requests: Dict[str, List[float]] = defaultdict(list)


def check_rate_limit(device_id: str, client_ip: str) -> None:
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS

    # 1. Check per-device rate limit
    dev_times = [t for t in _device_requests[device_id] if t > cutoff]
    if len(dev_times) >= RATE_LIMIT_DEVICE_PER_MINUTE:
        raise HTTPException(
            status_code=429,
            detail=f"Device rate limit exceeded: max {RATE_LIMIT_DEVICE_PER_MINUTE} votes per minute.",
        )

    # 2. Check per-IP rate limit
    ip_times = [t for t in _ip_requests[client_ip] if t > cutoff]
    if len(ip_times) >= RATE_LIMIT_IP_PER_MINUTE:
        raise HTTPException(
            status_code=429,
            detail=f"IP rate limit exceeded: max {RATE_LIMIT_IP_PER_MINUTE} votes per minute.",
        )

    # Record current request timestamps
    dev_times.append(now)
    _device_requests[device_id] = dev_times
    ip_times.append(now)
    _ip_requests[client_ip] = ip_times


def reset_rate_limits() -> None:
    """Clear in-memory rate limiting stores (useful for unit testing)."""
    _device_requests.clear()
    _ip_requests.clear()
