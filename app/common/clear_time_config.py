"""
Physics-based Road Clearance Configuration.

All coefficients and parameter defaults are assumptions based on simplified
urban hydrology heuristics and Torricelli-like orifice/weir drainage principles.
Every value is marked with assumption, unvalidated.
"""

# Runoff coefficient per OpenStreetMap highway class.
# Represents the proportion of rainfall in the catchment contributing to surface ponding.
# Values are assumptions, unvalidated.
RUNOFF_COEFFICIENTS = {
    "motorway": 1.20,       # assumption, unvalidated: wide impervious surface, rapid runoff into low points
    "trunk": 1.15,          # assumption, unvalidated: wide engineered highway catchment
    "primary": 1.10,        # assumption, unvalidated: arterial multi-lane road catchment
    "secondary": 1.00,      # assumption, unvalidated: standard urban collector road
    "tertiary": 0.95,       # assumption, unvalidated: local connecting road
    "residential": 0.85,    # assumption, unvalidated: neighborhood street with standard curb gutters
    "living_street": 0.80,  # assumption, unvalidated: narrow shared street with pedestrian surfaces
    "service": 0.75,        # assumption, unvalidated: alleyway or private service road
    "default": 0.85,        # assumption, unvalidated: fallback runoff factor
}

# Base drain rate coefficient k (in cm/h per sqrt(cm)) for dh/dt = - k * sqrt(h).
# Models gravity-driven storm drain outflow where flow velocity is proportional to sqrt(depth).
# Values are assumptions, unvalidated.
DRAIN_RATE_K = {
    "motorway": 4.0,        # assumption, unvalidated: modern high-capacity highway drainage
    "trunk": 3.6,           # assumption, unvalidated: major highway storm culverts
    "primary": 3.2,         # assumption, unvalidated: municipal arterial storm sewers
    "secondary": 2.8,       # assumption, unvalidated: collector road gutters and drains
    "tertiary": 2.4,        # assumption, unvalidated: local road catch basins
    "residential": 2.0,     # assumption, unvalidated: standard curbside storm drains prone to partial debris
    "living_street": 1.8,   # assumption, unvalidated: small street surface drainage
    "service": 1.5,         # assumption, unvalidated: narrow service lane with minimal drainage
    "default": 2.2,         # assumption, unvalidated: generic default road drainage coefficient
}

# Drainage speed multipliers for interval bounds (earliest vs likely vs latest).
# Values are assumptions, unvalidated.
DRAIN_RATE_MULTIPLIERS = {
    "fast": 1.30,           # assumption, unvalidated: optimistic drain rate under optimal flow conditions
    "nominal": 1.00,        # assumption, unvalidated: expected nominal drain rate
    "slow": 0.70,           # assumption, unvalidated: pessimistic drain rate under partially silted drains
}

# Antecedent precipitation (past 24h) impact on storm drain saturation.
# Values are assumptions, unvalidated.
SATURATED_SOIL_THRESHOLDS = {
    "heavy_past_rain_mm": 25.0,            # assumption, unvalidated: antecedent rain depth indicating saturated catchment
    "very_heavy_past_rain_mm": 50.0,       # assumption, unvalidated: antecedent rain depth indicating severe waterlogging
    "saturation_reduction_factor": 0.75,   # assumption, unvalidated: reduces k by 25% when past 24h rain > 25mm
    "severe_reduction_factor": 0.60,       # assumption, unvalidated: reduces k by 40% when past 24h rain > 50mm
}

# Bounds for fitting observed drain rate k from consecutive reports.
# Values are assumptions, unvalidated.
OBSERVED_K_BOUNDS = {
    "min_k": 0.5,           # assumption, unvalidated: minimum sane drain rate coefficient
    "max_k": 8.0,           # assumption, unvalidated: maximum sane drain rate coefficient
}

# Safety buffer time added after water depth drops to the vehicle GO threshold.
# Value is assumption, unvalidated.
TIME_BUFFER_MINUTES = 15    # assumption, unvalidated: safety time margin before confirming clearance

# Simulation step size and time horizon.
# Values are assumptions, unvalidated.
SIMULATION_STEP_MINUTES = 5 # assumption, unvalidated: 5-minute stepping provides smooth discrete simulation
SIMULATION_HORIZON_HOURS = 24 # assumption, unvalidated: maximum estimation horizon

# Heavy rain threshold (mm/hour) for continuous rain condition.
# Value is assumption, unvalidated.
HEAVY_RAIN_THRESHOLD_MM_PER_H = 8.0 # assumption, unvalidated: rain rate at or above which drainage is outpaced
