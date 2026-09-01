#!/usr/bin/env python3
"""Geometry checks for the probe lattice in scrape.py.

These are the assertions that keep the sweep honest without hitting the API:
the lattice has to actually cover the country, and the mask has to actually
contain the places Chemist Warehouse trades in. Run with `./scrape.sh --check`
or directly.
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scrape  # noqa: E402


def haversine_km(lat1, lon1, lat2, lon2):
    radians = math.radians
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(radians(lat1)) * math.cos(radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * scrape.EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# Regional and remote towns with, or plausibly with, a Chemist Warehouse. The
# mask has to contain all of them - a mask that quietly excludes Port Hedland
# is exactly the silent failure PRINCIPLES.md section 6 is about. (An earlier
# draft did exactly that.)
TOWNS = {
    "Sydney": (-33.87, 151.21), "Melbourne": (-37.81, 144.96),
    "Brisbane": (-27.47, 153.03), "Perth": (-31.95, 115.86),
    "Adelaide": (-34.93, 138.60), "Hobart": (-42.88, 147.33),
    "Canberra": (-35.28, 149.13), "Darwin": (-12.46, 130.84),
    "Alice Springs": (-23.70, 133.88), "Kalgoorlie": (-30.75, 121.47),
    "Mount Isa": (-20.72, 139.49), "Broome": (-17.96, 122.24),
    "Port Hedland": (-20.31, 118.61), "Karratha": (-20.74, 116.85),
    "Exmouth": (-21.93, 114.13), "Carnarvon": (-24.88, 113.66),
    "Geraldton": (-28.77, 114.61), "Esperance": (-33.86, 121.89),
    "Albany": (-35.02, 117.88), "Bunbury": (-33.33, 115.64),
    "Ceduna": (-32.13, 133.67), "Port Lincoln": (-34.73, 135.86),
    "Mount Gambier": (-37.83, 140.78), "Broken Hill": (-31.96, 141.47),
    "Cairns": (-16.92, 145.77), "Townsville": (-19.26, 146.82),
    "Weipa": (-12.63, 141.87), "Mackay": (-21.14, 149.19),
    "Rockhampton": (-23.38, 150.51), "Longreach": (-23.44, 144.25),
    "Charleville": (-26.40, 146.24), "Katherine": (-14.47, 132.26),
    "Tennant Creek": (-19.65, 134.19), "Nhulunbuy": (-12.19, 136.78),
    "Launceston": (-41.44, 147.14), "Burnie": (-41.05, 145.91),
    "Devonport": (-41.18, 146.35), "Wagga Wagga": (-35.11, 147.37),
    "Dubbo": (-32.25, 148.60), "Coffs Harbour": (-30.30, 153.11),
    "Byron Bay": (-28.64, 153.61), "Mildura": (-34.19, 142.16),
    "Whyalla": (-33.03, 137.56), "Bourke": (-30.09, 145.94),
    "Cooktown": (-15.47, 145.25), "Halls Creek": (-18.23, 127.67),
    "Coober Pedy": (-29.01, 134.75), "Yulara": (-25.24, 130.99),
}

failures = []


def check(condition, message):
    print(f"{'PASS' if condition else 'FAIL'}  {message}")
    if not condition:
        failures.append(message)


cells = scrape.full_sweep_cells()
centres = {cell: scrape.cell_centre(cell) for cell in cells}
print(f"full sweep: {len(cells)} probe points at {scrape.SPACING_KM:g} km spacing\n")

# 1. Every cell centre maps back to its own cell, so the targeted sweep really
#    is a subset of the full one.
roundtrip = [c for c, (lat, lon) in centres.items() if scrape.lattice_cell(lat, lon) != c]
check(not roundtrip, f"lattice round-trips for all {len(cells)} cells ({len(roundtrip)} failures)")

# 2. Nowhere in the swept area is further than RADIUS_KM from a probe point.
#    This walks every cell and samples a grid across it, rather than sampling
#    the country at random - the gaps a sheared lattice opens up are small and
#    a random sample can step straight over them.
SAMPLES_PER_AXIS = 7
worst, worst_at = 0.0, None
for cell in cells:
    lat_c, lon_c = centres[cell]
    lon_step = scrape._col_step_deg(lat_c)
    candidates = [centres[c] for c in [cell] + scrape.neighbours(cell) if c in centres]
    for i in range(SAMPLES_PER_AXIS):
        lat = lat_c + scrape.ROW_STEP_DEG * (i / (SAMPLES_PER_AXIS - 1) - 0.5)
        for j in range(SAMPLES_PER_AXIS):
            lon = lon_c + lon_step * (j / (SAMPLES_PER_AXIS - 1) - 0.5)
            distance = min(haversine_km(lat, lon, *p) for p in candidates)
            if distance > worst:
                worst, worst_at = distance, (lat, lon)
check(worst <= scrape.RADIUS_KM,
      f"worst-covered point in {len(cells)} cells is {worst:.2f} km from a probe "
      f"(radius {scrape.RADIUS_KM:g} km) at {worst_at[0]:.3f},{worst_at[1]:.3f}")

# 3. Neighbouring probes stay close enough together that no pair of adjacent
#    circles can come apart. Two circles of radius r overlap while their
#    centres are under 2r apart; the check above is the real guarantee, this
#    one just localises a regression to the lattice rather than the mask.
worst_gap, worst_gap_at = 0.0, None
for cell in cells:
    lat_c, lon_c = centres[cell]
    for n in scrape.neighbours(cell):
        if n not in centres:
            continue
        gap = haversine_km(lat_c, lon_c, *centres[n])
        if abs(n[0] - cell[0]) + abs(n[1] - cell[1]) == 1 and gap > worst_gap:
            worst_gap, worst_gap_at = gap, cell
check(worst_gap < 2 * scrape.RADIUS_KM,
      f"widest orthogonal neighbour gap is {worst_gap:.2f} km "
      f"(circles part at {2 * scrape.RADIUS_KM:g} km)")

# 4. Every town is inside the swept mask.
missed = sorted(name for name, (lat, lon) in TOWNS.items()
                if scrape.lattice_cell(lat, lon) not in cells)
check(not missed, f"all {len(TOWNS)} sample towns are inside the mask"
                  + (f" - missed {missed}" if missed else ""))

# 5. Coordinates go to the API at 4dp; that must not move a probe meaningfully.
worst_rounding = max(
    haversine_km(lat, lon, round(lat, 4), round(lon, 4)) for lat, lon in centres.values()
)
check(worst_rounding < 0.02, f"4dp coordinate rounding moves a probe at most "
                             f"{worst_rounding * 1000:.0f} m")

# 6. The daily discovery slices must partition the lattice exactly - every cell
#    swept, none swept twice, and the same partition on every run (Python's own
#    hash() is salted per process and would reshuffle it).
slices = [scrape.discovery_shard(cells, i) for i in range(scrape.DISCOVERY_SHARDS)]
covered = set().union(*slices)
overlap = sum(len(s) for s in slices) - len(covered)
sizes = sorted(len(s) for s in slices)
check(covered == cells and overlap == 0,
      f"{scrape.DISCOVERY_SHARDS} discovery slices partition all {len(cells)} cells "
      f"({len(covered)} covered, {overlap} swept twice)")
check(sizes[-1] - sizes[0] < len(cells) * 0.02 / scrape.DISCOVERY_SHARDS * 14,
      f"slices are even, {sizes[0]}-{sizes[-1]} cells each")
check(scrape.discovery_shard(cells, 3) == scrape.discovery_shard(cells, 3 + scrape.DISCOVERY_SHARDS),
      "the slice index wraps, so a rotating day number is safe")

print()
if failures:
    sys.exit(f"{len(failures)} check(s) failed")
print("all checks passed")
