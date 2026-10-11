#!/usr/bin/env python3
"""
Test script for Physics-based Clear Time estimation.
Usage:
    python scripts/try_clear_time.py <lat> <lng> <depth_low_cm> <depth_high_cm> <vehicle>

Example:
    python scripts/try_clear_time.py 12.9716 77.5946 25 35 hatchback
"""

import sys
import os

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common import clear_time, db, safety


def main():
    if len(sys.argv) < 6:
        print("Usage: python scripts/try_clear_time.py <lat> <lng> <depth_low_cm> <depth_high_cm> <vehicle>")
        sys.exit(1)

    lat = float(sys.argv[1])
    lng = float(sys.argv[2])
    depth_low = float(sys.argv[3])
    depth_high = float(sys.argv[4])
    vehicle = sys.argv[5].lower()

    db.init()
    recent = db.get_recent_reports_for_road_or_cell(lat=lat, lng=lng)

    print("=" * 60)
    print("Wade or Wait - Physics-Based Clear Time Estimation")
    print("=" * 60)
    print(f"Location:           {lat:.4f}, {lng:.4f}")
    print(f"Depth Interval:     {depth_low:.1f} - {depth_high:.1f} cm")
    print(f"Vehicle:            {vehicle}")
    threshold = clear_time.get_vehicle_go_threshold(vehicle)
    print(f"Vehicle GO Limit:   {threshold:.1f} cm (safety.py threshold)")
    print("-" * 60)

    result = clear_time.estimate_clear_time(
        lat=lat,
        lng=lng,
        depth_interval=(depth_low, depth_high),
        vehicle=vehicle,
        highway="residential",
        recent_reports=recent,
    )

    print(f"Forecast Available: {result['forecast_available']}")
    print(f"Drain Rate Basis:   {result['basis']} ({result['drain_rate_label']})")
    print(f"Earliest Safe:      {result['earliest']}")
    print(f"Likely Safe:        {result['likely']}")
    print(f"Latest Safe:        {result['latest']}")
    if result.get("rain_note"):
        print(f"Rain Note:          {result['rain_note']}")
    print(f"Display Range:      {result['display_text']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
