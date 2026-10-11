# Illustrative geometry for the demo. Replace with validated field measurements before real-world use.
TABLE = [
    ("two_wheeler_wheel", "at_axle"),
    ("two_wheeler_wheel", "at_hub"),
    ("two_wheeler_wheel", "above_hub"),
    ("car_wheel", "at_hub"),
    ("car_wheel", "above_hub"),
    ("car_door", "below_bottom"),
    ("car_door", "at_sill"),
    ("car_door", "above_sill"),
    ("road_curb", "below_half"),
    ("road_curb", "at_half"),
    ("road_curb", "above_half"),
    ("traffic_cone", "at_quarter"),
    ("traffic_cone", "at_half"),
    ("traffic_cone", "near_top"),
]

BANDS = [
    (0, 10, "very shallow"),
    (10, 20, "shallow"),
    (20, 35, "moderate"),
    (35, 50, "deep"),
    (50, 80, "very deep"),
    (80, 1000, "extreme"),
]

ESTIMATES = {
    ("two_wheeler_wheel", "at_axle"): (8, 15),
    ("two_wheeler_wheel", "at_hub"): (20, 35),
    ("two_wheeler_wheel", "above_hub"): (35, 55),
    ("car_wheel", "at_hub"): (10, 18),
    ("car_wheel", "above_hub"): (18, 30),
    ("car_door", "below_bottom"): (8, 15),
    ("car_door", "at_sill"): (25, 40),
    ("car_door", "above_sill"): (40, 65),
    ("road_curb", "below_half"): (8, 15),
    ("road_curb", "at_half"): (15, 30),
    ("road_curb", "above_half"): (30, 50),
    ("traffic_cone", "at_quarter"): (8, 12),
    ("traffic_cone", "at_half"): (15, 22),
    ("traffic_cone", "near_top"): (28, 38),
}

def band(depth_cm):
    for lo, hi, name in BANDS:
        if lo <= depth_cm < hi:
            return name
    return "extreme"

def estimate(objects, overall_confidence):
    candidates = []
    for obj in objects:
        key = (obj.get("type"), obj.get("waterline"))
        if key in ESTIMATES:
            lo, hi = ESTIMATES[key]
            conf = max(0.0, min(1.0, float(obj.get("confidence", 0))))
            candidates.append((lo, hi, conf))
    if not candidates:
        return None
    # Favor higher-confidence observations while retaining a conservative range.
    candidates.sort(key=lambda x: x[2], reverse=True)
    lo = min(x[0] for x in candidates[:3])
    hi = max(x[1] for x in candidates[:3])
    conf = max(0.0, min(1.0, float(overall_confidence)))
    if len(candidates) > 1:
        conf = min(1.0, conf + 0.05)
    return {"depth_lo": lo, "depth_hi": hi, "conf": conf}
