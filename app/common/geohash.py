"""
Pure Python Geohash Implementation for DynamoDB Cell Partitioning.
Supports encode, decode, and bounding-box cell coverage without external C libraries.
"""
from typing import List, Tuple, Set


_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DEC_MAP = {c: i for i, c in enumerate(_BASE32)}


def encode(lat: float, lng: float, precision: int = 5) -> str:
    """Encode latitude and longitude into a geohash string."""
    lat_interval = [-90.0, 90.0]
    lng_interval = [-180.0, 180.0]
    geohash = []
    bits = [16, 8, 4, 2, 1]
    bit = 0
    ch = 0
    is_lng = True

    while len(geohash) < precision:
        if is_lng:
            mid = (lng_interval[0] + lng_interval[1]) / 2.0
            if lng > mid:
                ch |= bits[bit]
                lng_interval[0] = mid
            else:
                lng_interval[1] = mid
        else:
            mid = (lat_interval[0] + lat_interval[1]) / 2.0
            if lat > mid:
                ch |= bits[bit]
                lat_interval[0] = mid
            else:
                lat_interval[1] = mid

        is_lng = not is_lng
        if bit < 4:
            bit += 1
        else:
            geohash.append(_BASE32[ch])
            bit = 0
            ch = 0

    return "".join(geohash)


def decode_bbox(geohash: str) -> Tuple[float, float, float, float]:
    """
    Decode geohash to its bounding box: (south, west, north, east).
    """
    lat_interval = [-90.0, 90.0]
    lng_interval = [-180.0, 180.0]
    is_lng = True

    for c in geohash.lower():
        cd = _DEC_MAP.get(c, 0)
        for mask in [16, 8, 4, 2, 1]:
            if is_lng:
                mid = (lng_interval[0] + lng_interval[1]) / 2.0
                if cd & mask:
                    lng_interval[0] = mid
                else:
                    lng_interval[1] = mid
            else:
                mid = (lat_interval[0] + lat_interval[1]) / 2.0
                if cd & mask:
                    lat_interval[0] = mid
                else:
                    lat_interval[1] = mid
            is_lng = not is_lng

    return (lat_interval[0], lng_interval[0], lat_interval[1], lng_interval[1])


def bbox_cells(west: float, south: float, east: float, north: float, precision: int = 5) -> List[str]:
    """
    Returns the set of geohashes of given precision covering the bounding box.
    Clamps steps to prevent runaway iterations on large boxes.
    """
    min_lng = min(west, east)
    max_lng = max(west, east)
    min_lat = min(south, north)
    max_lat = max(south, north)

    # Approximate step sizes for geohash precisions:
    # precision 4: lat ~0.176 deg, lng ~0.352 deg (~19km x 39km)
    # precision 5: lat ~0.044 deg, lng ~0.044 deg (~4.9km x 4.9km)
    if precision <= 4:
        d_lat, d_lng = 0.15, 0.30
    else:
        d_lat, d_lng = 0.035, 0.035

    cells: Set[str] = set()
    lat = min_lat
    while lat <= max_lat:
        lng = min_lng
        while lng <= max_lng:
            cells.add(encode(lat, lng, precision=precision))
            lng += d_lng
        cells.add(encode(lat, max_lng, precision=precision))
        lat += d_lat

    # Add north boundary row
    lng = min_lng
    while lng <= max_lng:
        cells.add(encode(max_lat, lng, precision=precision))
        lng += d_lng
    cells.add(encode(max_lat, max_lng, precision=precision))

    return sorted(list(cells))
