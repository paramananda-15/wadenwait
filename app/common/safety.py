# Illustrative demo limits only. Do not use these values to make real-world crossing decisions.
LIMITS = {
    "pedestrian": 15,
    "two_wheeler": 42,
    "hatchback": 32,
    "suv": 60,
    "ambulance": 45,
}

def verdict(lo, hi, confidence, mode, flow="still"):
    if confidence < 0.45:
        return "UNKNOWN"
    limit = LIMITS.get(mode, 32)
    # Fast flow is treated more conservatively.
    if flow == "fast":
        limit *= 0.85
    elif flow == "slow":
        limit *= 0.95

    mid = (float(lo) + float(hi)) / 2.0

    if mode == "two_wheeler":
        if mid >= limit or lo >= limit:
            return "DO_NOT_CROSS"
        if mid > 14.0 or hi > 18.0:
            return "CAUTION"
        return "GO"

    if mode == "suv":
        if mid >= limit or lo >= limit:
            return "DO_NOT_CROSS"
        if mid > 42.0 or lo > 42.0:
            return "CAUTION"
        return "GO"

    if mid >= limit or lo >= limit:
        return "DO_NOT_CROSS"
    if mid >= limit * 0.65 or hi >= limit:
        return "CAUTION"
    return "GO"

