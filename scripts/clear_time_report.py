#!/usr/bin/env python3
"""
Prints estimate vs actual for logged clear time cases.
Usage:
    python scripts/clear_time_report.py
"""

import sys
import os
from datetime import datetime

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common import db


def main():
    db.init()
    logs = db.get_clear_time_logs(limit=200)

    if not logs:
        print("no data yet")
        return

    print("=" * 90)
    print("Clear Time Estimate vs Actual Report")
    print("=" * 90)
    header = f"{'Road / Cell':<20} {'Vehicle':<12} {'Issued At':<18} {'Estimate Range':<22} {'Basis':<10} {'Actual Cleared':<18}"
    print(header)
    print("-" * 90)

    for row in logs:
        road_cell = row.get("road_id") or row.get("cell_key") or f"{row.get('lat', 0):.2f},{row.get('lng', 0):.2f}"
        veh = row.get("vehicle") or "-"
        issued_ts = row.get("issued_at")
        issued_str = datetime.fromtimestamp(issued_ts).strftime("%m-%d %H:%M:%S") if issued_ts else "-"

        earliest = row.get("earliest") or "-"
        latest = row.get("latest") or "-"
        est_range = f"{earliest} - {latest}" if earliest != latest else earliest

        basis = row.get("basis") or "-"
        actual_ts = row.get("actual_cleared_at")
        if actual_ts:
            actual_str = datetime.fromtimestamp(actual_ts).strftime("%m-%d %H:%M:%S")
            diff_min = round((actual_ts - issued_ts) / 60) if issued_ts else None
            actual_display = f"{actual_str} (+{diff_min}m)" if diff_min is not None else actual_str
        else:
            actual_display = "Pending"

        print(f"{str(road_cell)[:19]:<20} {veh:<12} {issued_str:<18} {est_range:<22} {basis:<10} {actual_display:<18}")

    print("=" * 90)


if __name__ == "__main__":
    main()
