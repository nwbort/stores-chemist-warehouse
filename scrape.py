#!/usr/bin/env python3
"""Scrape Chemist Warehouse's Australian store network into stores.json.

Why a grid search (SCRAPING.md rung 6)
--------------------------------------
Chemist Warehouse's locator is a thin frontend over one JSON endpoint:

    GET https://api.chemistwarehouse.com.au/web/v1/channels/cwr-cw-au/en/radius
        ?channel-type=store&latitude=..&longitude=..
        &search-type=store-locator&offset=0&limit=100

It is unauthenticated and needs no cookie, but it only ever answers "what is
near this point", and probing established that none of that is negotiable:

  * the radius is fixed at exactly 26.0 km. `radius`, `distance`,
    `maxDistance`, `searchRadius`, `range` and friends are all ignored;
  * `limit` is capped at 100 (anything larger is a 400), and results come back
    sorted by distance, so dense areas genuinely truncate - Melbourne CBD has
    144 stores inside 26 km and needs two pages;
  * `offset` paging works, so the truncation is recoverable;
  * latitude/longitude are rejected beyond ~6 decimal places (400
    "Invalid coordinates parameter(s) supplied"), so probe points are emitted
    at 4dp - about 11 m, far below anything that matters here;
  * there is no list-all sibling. /stores, /all, /list, /search and a
    by-channel-key lookup are all 405, and the endpoint 500s without
    coordinates.

The rest of the site is no help either: www.chemistwarehouse.com.au sits behind
a Cloudflare interactive challenge, so /robots.txt and /sitemap.xml are 403 to
anything without a solved `cf_clearance` cookie. That rules out rungs 2-5 for
anything that has to run unattended in CI. api.chemistwarehouse.com.au is not
challenged, which is what makes this approach durable.

So: cover the country in overlapping 26 km circles and dedupe by store key.

Coverage
--------
Probe points sit on a square lattice with per-row longitude steps, so that
neighbouring points are SPACING_KM apart both north-south and east-west. The
worst-covered spot is then a cell corner, sqrt(2)/2 * SPACING_KM from the
nearest probe, which is what bounds SPACING_KM below 26 * sqrt(2) = 36.8 km.

A triangular lattice would need about 30% fewer points for the same guarantee,
and an earlier draft used one. It does not survive contact with the globe: the
lattice only wins if adjacent rows are offset by exactly half a step, and in
any lon/lat scheme the east-west step is a function of cos(latitude), so the
half-step offset shears apart as you move away from the origin meridian - about
20 km of drift per row at Sydney's longitude, which reopens the gaps it was
supposed to close. Projecting properly would fix it and cost 30 lines of
Lambert azimuthal maths to save four minutes a week. Not worth it.

Two sweeps, because the data has two tiers (PRINCIPLES.md section 4):

  full      Every lattice cell whose centre is on or near Australian land,
            plus the external territories: 7,738 points, about 12 minutes.
            This is the sweep that can discover a store in a town where we
            have never seen one. Weekly.

  targeted  Only the cells that already contain a known store, plus the eight
            lattice neighbours of each - so it still sees anything new within
            ~26 km of the existing network, which is where nearly every new
            store turns up. About 480 points and under a minute, seeded from
            the committed stores.json, so a store the weekly sweep discovers
            is covered by every daily run afterwards. Falls back to a full
            sweep when there is no stores.json to seed from.

            Run back to back against the full sweep, the two produce
            byte-identical files.

Usage:  ./scrape.sh [--full | --targeted]
"""

import argparse
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://api.chemistwarehouse.com.au/web/v1/channels/cwr-cw-au/en/radius"
OUTPUT = "stores.json"

# Measured, not guessed: binary-searching a lone regional store in and out of
# range puts the cutoff at 26.002 km with a 6371.0088 km mean earth radius.
RADIUS_KM = 26.0

# Lattice spacing. The gapless bound is RADIUS_KM * sqrt(2) = 36.77 km; 34 km
# leaves the worst-covered point 24.04 km from a probe centre, a 7.5% margin
# for the earth not being a sphere and for coordinate rounding. Verified
# numerically by tests/check_coverage.py.
SPACING_KM = 34.0

PAGE_SIZE = 100          # hard API cap
MAX_PAGES = 20           # a single point returning 2000 stores is a bug, not data
WORKERS = 8              # PRINCIPLES.md section 6: parallelism is for finishing, not racing
MAX_ATTEMPTS = 5
TIMEOUT = 30

# A drop this large is a broken scraper, not a mass closure.
MIN_RETAINED_FRACTION = 0.5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-AU,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    "origin": "https://www.chemistwarehouse.com.au",
    "referer": "https://www.chemistwarehouse.com.au/",
    "user-agent": USER_AGENT,
}

EARTH_RADIUS_KM = 6371.0088
KM_PER_DEG_LAT = math.pi * EARTH_RADIUS_KM / 180.0  # ~111.19

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# --------------------------------------------------------------------------
# Where to look
# --------------------------------------------------------------------------

# A deliberately generous outline of the Australian mainland, as (lat, lon)
# vertices running clockwise from Cape York. It exists only to keep the sweep
# from probing thousands of points of open ocean, so every edge is drawn a
# little offshore rather than on the coast, and MASK_BUFFER_CELLS below widens
# it by another ring - a few hundred wasted sea probes cost far less than one
# missed store. Interior desert is emphatically *inside* the polygon:
# Kalgoorlie, Alice Springs and Mount Isa are exactly the places an over-clever
# mask would drop silently.
MAINLAND = [
    (-10.6, 142.6), (-11.0, 141.9), (-12.5, 141.3), (-14.0, 141.3),
    (-15.5, 140.8), (-17.0, 139.9), (-17.8, 138.5), (-17.9, 137.0),
    (-16.5, 136.0), (-15.0, 136.0), (-14.2, 136.9), (-12.0, 137.0),
    (-11.5, 136.0), (-11.9, 134.5), (-11.0, 132.5), (-12.0, 130.8),
    (-13.0, 129.6), (-14.8, 129.0), (-15.3, 128.0), (-14.5, 126.5),
    (-14.2, 125.0), (-15.5, 123.5), (-16.5, 122.8), (-18.0, 121.5),
    (-19.8, 120.8), (-20.0, 118.5), (-20.4, 116.6), (-21.3, 114.9),
    (-21.9, 113.9), (-23.0, 113.3), (-24.5, 112.9), (-26.5, 112.8),
    (-28.5, 113.5), (-30.0, 114.6), (-31.8, 115.4), (-33.5, 114.8),
    (-34.6, 114.9), (-35.3, 116.5), (-35.3, 118.2), (-34.2, 119.5),
    (-34.2, 121.9), (-33.2, 124.0), (-32.5, 126.0), (-32.0, 128.0),
    (-31.6, 129.5), (-31.5, 131.5), (-32.0, 133.0), (-33.0, 134.5),
    (-34.9, 135.6), (-35.8, 136.9), (-36.2, 139.5), (-38.6, 140.9),
    (-38.9, 143.5), (-39.3, 146.0), (-38.9, 148.5), (-37.8, 150.2),
    (-36.0, 150.5), (-34.5, 151.4), (-32.5, 152.8), (-30.5, 153.4),
    (-28.2, 154.0), (-26.0, 153.6), (-24.5, 153.5), (-23.0, 152.0),
    (-21.5, 150.5), (-19.5, 148.0), (-18.5, 146.6), (-16.5, 146.2),
    (-14.5, 145.5), (-12.5, 143.8),
]

# Tasmania, drawn wide enough to take in King and Flinders Islands as well.
TASMANIA = [
    (-39.4, 143.6), (-40.5, 143.5), (-42.5, 144.4), (-43.8, 145.4),
    (-43.8, 147.2), (-43.0, 148.6), (-39.4, 148.6),
]

# Offshore places the polygons above deliberately do not reach. Chemist
# Warehouse has never had a store on any of them, but they are three requests -
# cheap enough that "we checked" beats "we assumed".
EXTERNAL_TERRITORIES = [
    (-10.45, 105.67),   # Christmas Island
    (-12.19, 96.83),    # Cocos (Keeling) Islands
    (-29.04, 167.95),   # Norfolk Island
]

# Rings of lattice cells added outside the polygons above. Insurance against
# the outline being drawn a little tight somewhere; one ring is SPACING_KM.
MASK_BUFFER_CELLS = 1

# Lattice anchor. Nothing depends on these values beyond both sweeps sharing
# them, which is what makes the targeted probe set a strict subset of the full
# one.
# LON_ORIGIN sits in the middle of the country on purpose. Column steps are
# computed per row, so two vertically adjacent points drift apart east-west by
# (col * the difference in step), and anchoring the columns at the west coast
# would put Sydney at column ~106 and shear the rows by 12 km. Anchoring at
# 133 deg E halves the worst column index, and so the worst shear.
LAT_ORIGIN = -44.0
LON_ORIGIN = 133.0
LON_WEST = 112.0
LAT_LIMIT = -9.0
LON_LIMIT = 154.5
ROW_STEP_DEG = SPACING_KM / KM_PER_DEG_LAT


def point_in_polygon(lat, lon, polygon):
    """Standard ray casting. At this scale, treating lat/lon as planar is fine
    for an inclusion test whose edges are already drawn out to sea."""
    inside = False
    count = len(polygon)
    for i in range(count):
        y1, x1 = polygon[i]
        y2, x2 = polygon[(i + 1) % count]
        if (y1 > lat) != (y2 > lat):
            if lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def on_land(lat, lon):
    return point_in_polygon(lat, lon, MAINLAND) or point_in_polygon(lat, lon, TASMANIA)


def _col_step_deg(lat):
    """Longitude step that is SPACING_KM on the ground at this latitude."""
    return SPACING_KM / (KM_PER_DEG_LAT * math.cos(math.radians(lat)))


def row_latitude(row):
    return LAT_ORIGIN + row * ROW_STEP_DEG


def cell_centre(cell):
    row, col = cell
    lat = row_latitude(row)
    return lat, LON_ORIGIN + col * _col_step_deg(lat)


def lattice_cell(lat, lon):
    """Map a coordinate onto the lattice as an integer (row, col).

    Column steps are computed per row, so rows do not share a physical column
    origin - which is exactly why the lattice is square rather than triangular
    (see the module docstring). Integer indices are still the right currency:
    they are what makes the targeted probe set a strict subset of the full one.
    """
    row = int(round((lat - LAT_ORIGIN) / ROW_STEP_DEG))
    col = int(round((lon - LON_ORIGIN) / _col_step_deg(row_latitude(row))))
    return row, col


def neighbours(cell):
    """The eight lattice cells surrounding this one."""
    row, col = cell
    return [(row + dr, col + dc)
            for dr in (-1, 0, 1) for dc in (-1, 0, 1)
            if (dr, dc) != (0, 0)]


def full_sweep_cells():
    """Every lattice cell on or near Australian land, plus the territories."""
    cells = set()
    row = 0
    while row_latitude(row) <= LAT_LIMIT:
        step = _col_step_deg(row_latitude(row))
        for col in range(int((LON_WEST - LON_ORIGIN) / step) - 1,
                         int((LON_LIMIT - LON_ORIGIN) / step) + 2):
            if on_land(*cell_centre((row, col))):
                cells.add((row, col))
        row += 1

    for _ in range(MASK_BUFFER_CELLS):
        cells |= {n for cell in cells for n in neighbours(cell)}

    for lat, lon in EXTERNAL_TERRITORIES:
        cells.add(lattice_cell(lat, lon))
    return cells


def targeted_sweep_cells(stores):
    """Cells holding a known store, plus one ring of neighbours around each.

    The ring is what lets a daily run find a store that opened since the last
    full sweep: anything within roughly 26 km of the existing network shows up.
    """
    cells = set()
    for store in stores:
        if store.get("latitude") is None or store.get("longitude") is None:
            continue
        cell = lattice_cell(store["latitude"], store["longitude"])
        cells.add(cell)
        cells.update(neighbours(cell))
    return cells


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

class FetchError(Exception):
    pass


def request_page(lat, lon, offset):
    query = urllib.parse.urlencode({
        "channel-type": "store",
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "search-type": "store-locator",
        "offset": offset,
        "limit": PAGE_SIZE,
    })
    req = urllib.request.Request(f"{API}?{query}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_point(lat, lon):
    """All stores within 26 km of one point, following offset pages."""
    found = []
    offset = 0
    for _ in range(MAX_PAGES):
        payload = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                payload = request_page(lat, lon, offset)
                break
            except urllib.error.HTTPError as exc:
                # A 4xx other than 429 means we are asking the wrong question;
                # retrying just asks it again. Surface it.
                if exc.code != 429 and 400 <= exc.code < 500:
                    raise FetchError(
                        f"{lat:.4f},{lon:.4f} offset={offset}: HTTP {exc.code} "
                        f"{exc.read()[:200]!r}"
                    ) from exc
                last = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last = repr(exc)
            if attempt == MAX_ATTEMPTS - 1:
                raise FetchError(f"{lat:.4f},{lon:.4f} offset={offset}: gave up after "
                                 f"{MAX_ATTEMPTS} attempts, last error {last}")
            # Exponential backoff with jitter.
            time.sleep(min(30.0, 2 ** attempt) * (0.5 + random.random()))

        page = payload.get("channels") or []
        found.extend(page)
        if len(page) < PAGE_SIZE:
            return found
        offset += PAGE_SIZE
    raise FetchError(f"{lat:.4f},{lon:.4f}: still paging after {MAX_PAGES} pages")


def sweep(cells):
    """Fetch every cell, returning {key: raw channel}. Any hard failure aborts."""
    points = sorted(cell_centre(cell) for cell in cells)
    total = len(points)
    stores = {}
    done = 0
    started = time.time()

    def work(point):
        return fetch_point(*point)

    with ThreadPoolExecutor(WORKERS) as pool:
        for result in pool.map(work, points):
            for entry in result:
                channel = entry.get("channel") or {}
                key = channel.get("key")
                if key:
                    stores[key] = channel
            done += 1
            if done % 250 == 0 or done == total:
                rate = done / max(time.time() - started, 1e-9)
                print(f"  {done}/{total} points, {len(stores)} stores, "
                      f"{rate:.1f} req/s", file=sys.stderr, flush=True)
    return stores


# --------------------------------------------------------------------------
# Normalising
# --------------------------------------------------------------------------

def clean_time(value):
    """'07:30:00.000' -> '07:30'; keep seconds only when they are meaningful."""
    if not value:
        return None
    match = re.match(r"^(\d{2}):(\d{2})(?::(\d{2}))?", str(value))
    if not match:
        return str(value)
    hour, minute, second = match.groups()
    return f"{hour}:{minute}:{second}" if second and second != "00" else f"{hour}:{minute}"


def normalise(channel):
    """Upstream is nearly clean, but not quite - three things force a reshape.

    The payload nests every store under {"channel": {...}}; openingHours comes
    back with per-day key order that varies between days and between runs
    (monday as {from,to}, tuesday as {to,from}); and capabilities arrive in
    arbitrary order. Left alone, the last two repaint the whole file on runs
    where nothing actually changed - exactly what PRINCIPLES.md section 2 warns
    about. So: fixed day order, fixed key order, sorted capabilities.
    """
    address = channel.get("address") or {}
    coords = channel.get("coordinates") or {}
    hours_in = channel.get("openingHours") or {}

    hours = {}
    for day in DAYS:
        spec = hours_in.get(day) or {}
        opens, closes = clean_time(spec.get("from")), clean_time(spec.get("to"))
        hours[day] = {"from": opens, "to": closes} if opens or closes else None

    key = channel.get("key") or ""
    match = re.search(r"-store-(\d+)$", key)

    return {
        "key": key,
        "store_id": int(match.group(1)) if match else None,
        "name": channel.get("name"),
        "type": channel.get("type"),
        "capabilities": sorted(channel.get("capabilities") or []),
        "address": {
            "building": address.get("building"),
            "street_number": address.get("streetNumber"),
            "street_name": address.get("streetName"),
            "city": address.get("city"),
            "region": address.get("region"),
            "state": address.get("state"),
            "postal_code": address.get("postalCode"),
            "country": address.get("country"),
        },
        "phone": address.get("phone"),
        "fax": address.get("fax"),
        "email": address.get("email"),
        "latitude": coords.get("latitude"),
        "longitude": coords.get("longitude"),
        "opening_hours": hours,
    }


def sort_key(store):
    """Numeric store id first - it is the stable identity, and it keeps a
    renamed or relocated store on the same line of the diff."""
    return (store["store_id"] is None, store["store_id"] or 0, store["key"])


# --------------------------------------------------------------------------

def load_existing():
    if not os.path.exists(OUTPUT):
        return None
    try:
        with open(OUTPUT, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else None
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: could not read existing {OUTPUT}: {exc}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--full", action="store_const", const="full", dest="sweep",
                       help="sweep the whole country (slow, weekly)")
    group.add_argument("--targeted", action="store_const", const="targeted", dest="sweep",
                       help="sweep around known stores only (fast, daily)")
    parser.set_defaults(sweep=os.environ.get("SWEEP", "full"))
    args = parser.parse_args()

    existing = load_existing()

    mode = args.sweep
    if mode not in ("full", "targeted"):
        sys.exit(f"error: unknown sweep mode {mode!r} (expected 'full' or 'targeted')")
    if mode == "targeted" and not existing:
        print(f"no usable {OUTPUT} to seed a targeted sweep from; running a full sweep",
              file=sys.stderr)
        mode = "full"

    cells = full_sweep_cells() if mode == "full" else targeted_sweep_cells(existing)
    print(f"{mode} sweep: {len(cells)} probe points at {SPACING_KM:g} km spacing "
          f"({RADIUS_KM:g} km radius)", file=sys.stderr)

    try:
        raw = sweep(cells)
    except FetchError as exc:
        # PRINCIPLES.md section 6: fail on the fetch. A partial sweep looks
        # exactly like a wave of closures, so never let one reach stores.json.
        sys.exit(f"error: fetch failed, refusing to write a partial result: {exc}")

    stores = sorted((normalise(channel) for channel in raw.values()), key=sort_key)

    if not stores:
        sys.exit("error: sweep returned zero stores - the endpoint has almost "
                 "certainly moved. Re-run the devtools pass over the store "
                 "locator before touching this script.")

    if existing:
        floor = int(len(existing) * MIN_RETAINED_FRACTION)
        if len(stores) < floor:
            sys.exit(f"error: found {len(stores)} stores, down from {len(existing)} "
                     f"(floor {floor}). That is a broken scraper, not a mass "
                     f"closure - not overwriting {OUTPUT}.")
        added = len(stores) - len(existing)
        print(f"{len(stores)} stores ({added:+d} against the committed file)", file=sys.stderr)
    else:
        print(f"{len(stores)} stores (no previous file to compare against)", file=sys.stderr)

    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(stores, handle, indent=2, ensure_ascii=False, sort_keys=False)
        handle.write("\n")


if __name__ == "__main__":
    main()
