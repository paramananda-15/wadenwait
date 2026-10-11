"""
Detector reference dimensions and thresholds.

All physical dimensions are in centimetres (cm).
NOTE: These values are illustrative heuristic assumptions for the visible-height
estimation method, not official vehicle manufacturer or surveying specifications.
Each entry includes a source citation and assumption rationale.
"""

# Reference dimensions (cm) and canonical aspect ratios (width / height)
# Source and assumption rationale are noted per class.
REFERENCE_DIMENSIONS = {
    "car": {
        # ASSUMPTION: Typical compact/midsize passenger car (e.g. Volkswagen Golf, Toyota Corolla, Maruti Swift).
        # SOURCE: Euro NCAP & ISO 3833 passenger car dimensional envelopes (approx 175-180 cm width, 145-150 cm height).
        "known_width": 178.0,
        "known_full_height": 148.0,
        # ASSUMPTION: Front/rear canonical view aspect ratio (width/height), allowing for water submergence up to ~55%.
        # Oblique (3/4) or pure side views show vehicle length (~400-450 cm) instead of width (aspect ratio > 2.5).
        "aspect_ratio_range": (0.65, 2.20),
    },
    "motorcycle": {
        # ASSUMPTION: Typical commuter motorcycle or scooter handlebar width and ground-to-mirror height.
        # SOURCE: Motorcycle industry dimensional standards (SAE J1100 / ISO 6726: handlebar width ~75-80 cm, height ~105-115 cm).
        "known_width": 78.0,
        "known_full_height": 112.0,
        # ASSUMPTION: Front/rear canonical view aspect ratio, allowing for flood water submergence.
        "aspect_ratio_range": (0.40, 1.80),
    },
    "bicycle": {
        # ASSUMPTION: Standard adult upright commuter bicycle handlebar width and total height.
        # SOURCE: CPSC 16 CFR Part 1512 / ISO 4210 bicycle safety standards (handlebar width ~58-62 cm, height ~100-105 cm).
        "known_width": 60.0,
        "known_full_height": 102.0,
        # ASSUMPTION: Front/rear canonical view aspect ratio, allowing for flood water submergence.
        "aspect_ratio_range": (0.35, 1.60),
    },
    "person": {
        # ASSUMPTION: Average standing adult human biacromial shoulder breadth and standing height.
        # SOURCE: CDC Anthropometric Reference Data & NASA Anthropometric Source Book (NASA-STD-3000: shoulder width ~45-48 cm, height ~170 cm).
        "known_width": 46.0,
        "known_full_height": 170.0,
        # ASSUMPTION: Standing adult front/back view aspect ratio, allowing for water up to waist.
        "aspect_ratio_range": (0.18, 0.75),
    },
    "bus": {
        # ASSUMPTION: Standard single-deck urban transit bus dimensions.
        # SOURCE: UNECE Regulation 107 & Federal Motor Vehicle Safety Standards (width ~250 cm, height ~315-325 cm).
        "known_width": 250.0,
        "known_full_height": 315.0,
        # ASSUMPTION: Front/rear canonical view aspect ratio, allowing for flood water submergence.
        "aspect_ratio_range": (0.60, 2.00),
    },
    "truck": {
        # ASSUMPTION: Medium-duty commercial delivery truck cab dimensions.
        # SOURCE: Commercial vehicle dimensional envelopes (FMVSS / European Type Approval: cab width ~240-250 cm, height ~280-300 cm).
        "known_width": 245.0,
        "known_full_height": 290.0,
        # ASSUMPTION: Front/rear canonical view aspect ratio, allowing for flood water submergence.
        "aspect_ratio_range": (0.60, 2.00),
    },
    "stop sign": {
        # ASSUMPTION: Standard urban octagonal stop sign width and total mounting height from road surface to top of sign.
        # SOURCE: US Federal Highway Administration MUTCD Section 2A.15 (75 cm / 30-inch octagon) & Section 2A.18 (7 ft / ~210 cm mounting height).
        "known_width": 75.0,
        "known_full_height": 210.0,
        # ASSUMPTION: Vertical ground-to-sign aspect ratio, allowing for water submergence up the post.
        "aspect_ratio_range": (0.20, 0.90),
    },
}

# Detection filtering & abstain thresholds
DETECTION_THRESHOLDS = {
    # Minimum detector confidence to consider a detection
    "min_detector_confidence": 0.40,
    # Edge margin in pixels (boxes closer than this to image boundary are treated as clipped)
    "edge_margin_px": 4,
    # Minimum box width and height in pixels to avoid extreme pixelation error
    "min_box_width_px": 28,
    "min_box_height_px": 28,
    # Minimum box area fraction of total image area
    "min_box_area_ratio": 0.003,
}

# Mapping between VLM object types and detector COCO classes for cross-checking
VLM_TO_DETECTOR_MAP = {
    "car_wheel": ["car", "truck", "bus"],
    "car_door": ["car", "truck", "bus"],
    "two_wheeler_wheel": ["motorcycle", "bicycle"],
}
