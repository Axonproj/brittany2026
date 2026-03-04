#!/usr/bin/env python3
"""Sailing route map: OSM basemap + GPX routes → SVG.

Usage:
    python3 sailing_map.py Navionics_archive_export.gpx [another.gpx ...]

Edit MAP_TITLE and ROUTE_NOTES below to customise the info box.
"""

import requests
import xml.etree.ElementTree as ET
import math
import sys
import json
import hashlib
import time
import os

# ---------------------------------------------------------------------------
# Customise here
# ---------------------------------------------------------------------------
MAP_TITLE = "North Brittany Passages"
ROUTE_NOTES = {
    # Partial route name (case-insensitive) → note shown in info box
    # e.g. "StQuay": "St Quay to Lézardrieux via Roches Douvres"
}

MARGIN_DEG  = 0.08   # padding around routes
SVG_W       = 1300   # SVG width in pixels; height auto-computed
LABEL_FONT  = "sans-serif"

ROUTE_COLORS = [
    "#e63946",   # red
    "#2a9d8f",   # teal
    "#f4a261",   # orange
    "#6a4c93",   # purple
    "#1982c4",   # blue
    "#e9c46a",   # amber
    "#a8dadc",   # light blue
    "#2d6a4f",   # forest green
]

NAMED_TOWNS = {"Lézardrieux", "Tréguier", "Saint-Quay-Portrieux"}
GPX_NS      = {"g": "http://www.topografix.com/GPX/1/1"}

# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def haversine_nm(lon1, lat1, lon2, lat2):
    R = 3440.065
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(df / 2)**2 + math.cos(f1) * math.cos(f2) * math.sin(dl / 2)**2
    return 2 * R * math.asin(math.sqrt(a))


def route_distance_nm(pts):
    return sum(haversine_nm(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1])
               for i in range(len(pts) - 1))


def route_bearing_deg(pt_a, pt_b):
    """Initial bearing from pt_a to pt_b in degrees [0,360)."""
    lat1, lat2 = math.radians(pt_a[1]), math.radians(pt_b[1])
    dlon = math.radians(pt_b[0] - pt_a[0])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ---------------------------------------------------------------------------
# GPX parsing
# ---------------------------------------------------------------------------

def parse_gpx(path):
    """Return list of (name, [(lon, lat), ...]) from routes and tracks."""
    tree = ET.parse(path)
    root = tree.getroot()
    routes = []

    for rte in root.findall("g:rte", GPX_NS):
        name = rte.findtext("g:name", "Route", GPX_NS)
        pts = [(float(p.get("lon")), float(p.get("lat")))
               for p in rte.findall("g:rtept", GPX_NS)]
        if pts:
            routes.append((name, pts))

    for trk in root.findall("g:trk", GPX_NS):
        name = trk.findtext("g:name", "Track", GPX_NS)
        pts = [(float(p.get("lon")), float(p.get("lat")))
               for p in trk.findall(".//g:trkpt", GPX_NS)]
        if pts:
            routes.append((name, pts))

    return routes


# ---------------------------------------------------------------------------
# Coordinate projection
# ---------------------------------------------------------------------------

class Projection:
    def __init__(self, south, west, north, east, svg_w, svg_h):
        self.south, self.west = south, west
        self.north, self.east = north, east
        self.svg_w, self.svg_h = svg_w, svg_h

    def xy(self, lon, lat):
        x = (lon - self.west)  / (self.east  - self.west)  * self.svg_w
        y = (self.north - lat) / (self.north - self.south) * self.svg_h
        return round(x, 1), round(y, 1)

    def path(self, pts):
        coords = [self.xy(lon, lat) for lon, lat in pts]
        return "M " + " L ".join(f"{x},{y}" for x, y in coords)


# ---------------------------------------------------------------------------
# Overpass query
# ---------------------------------------------------------------------------

def query_overpass(south, west, north, east):
    bbox = f"{south:.4f},{west:.4f},{north:.4f},{east:.4f}"
    query = f"""
[out:json][timeout:120];
(
  way["natural"="coastline"]({bbox});
  way["waterway"~"^(river|canal)$"]({bbox});
  way["highway"~"^(motorway|trunk|primary)$"]({bbox});
  way["boundary"="administrative"]["admin_level"="6"]({bbox});
  node["place"~"^(city|town)$"]({bbox});
  node["place"]["name"~"Lezardrieux|Treguier|Saint-Quay",i]({bbox});
);
out body;
>;
out skel qt;
"""

    # Local cache keyed on query content
    cache_key = hashlib.md5(query.encode()).hexdigest()[:12]
    cache_file = f".osm_cache_{cache_key}.json"
    if os.path.exists(cache_file):
        print(f"Using cached OSM data ({cache_file})", flush=True)
        with open(cache_file) as f:
            return json.load(f)

    mirrors = [
        "https://overpass-api.de/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
        "https://z.overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]
    for url in mirrors:
        for attempt in range(2):
            try:
                print(f"Querying {url} (attempt {attempt+1}) ...", flush=True)
                r = requests.post(url, data={"data": query}, timeout=120)
                r.raise_for_status()
                data = r.json()   # will raise if HTML returned
                with open(cache_file, "w") as f:
                    json.dump(data, f)
                print(f"  cached to {cache_file}", flush=True)
                return data
            except Exception as e:
                print(f"  failed: {e}", flush=True)
                if attempt == 0:
                    time.sleep(3)
    raise RuntimeError("All Overpass mirrors failed")


# ---------------------------------------------------------------------------
# XML escaping
# ---------------------------------------------------------------------------

def xe(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# SVG building
# ---------------------------------------------------------------------------

def basemap_elements(data, proj):
    """Return list of SVG strings for OSM basemap."""
    nodes = {}
    for el in data["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])

    styles = {
        "coastline": 'stroke="#1a4a7a" stroke-width="1.8" fill="none"',
        "waterway":  'stroke="#4a90c4" stroke-width="1.2" fill="none"',
        "motorway":  'stroke="#cc3300" stroke-width="2"   fill="none"',
        "trunk":     'stroke="#cc3300" stroke-width="1.5" fill="none"',
        "primary":   'stroke="#884400" stroke-width="1"   fill="none"',
        "admin":     'stroke="#888888" stroke-width="0.7" fill="none" stroke-dasharray="5,4"',
    }

    out = []
    for el in data["elements"]:
        if el["type"] != "way":
            continue
        tags = el.get("tags", {})
        refs = el.get("nodes", [])
        pts = [(nodes[r][0], nodes[r][1]) for r in refs if r in nodes]
        if len(pts) < 2:
            continue
        d = proj.path(pts)
        hw = tags.get("highway", "")
        nat = tags.get("natural", "")
        ww = tags.get("waterway", "")
        bd = tags.get("boundary", "")

        if nat == "coastline":          style = styles["coastline"]
        elif ww in ("river", "canal"):  style = styles["waterway"]
        elif hw == "motorway":          style = styles["motorway"]
        elif hw == "trunk":             style = styles["trunk"]
        elif hw == "primary":           style = styles["primary"]
        elif bd == "administrative":    style = styles["admin"]
        else:                           continue

        out.append(f'  <path d="{d}" {style}/>')

    return out


def place_elements(data, proj):
    """Return SVG strings for city/town dots and labels."""
    NAMED_LC = {n.lower() for n in NAMED_TOWNS}

    def is_place(el):
        if el["type"] != "node":
            return False
        t = el.get("tags", {})
        if t.get("place") in ("city", "town"):
            return True
        return any(n in t.get("name", "").lower() for n in NAMED_LC)

    seen, places = set(), []
    for el in data["elements"]:
        if is_place(el):
            name = el.get("tags", {}).get("name", "")
            if name not in seen:
                seen.add(name)
                places.append(el)

    out = []
    for el in places:
        tags = el.get("tags", {})
        x, y = proj.xy(el["lon"], el["lat"])
        name = xe(tags.get("name", ""))
        place = tags.get("place", "")
        special = any(n in tags.get("name", "").lower() for n in NAMED_LC)
        is_city = place == "city"
        r  = 5   if is_city else 4  if special else 3
        fs = 13  if is_city else 11 if special else 10
        fw = "bold" if (is_city or special) else "normal"
        fc = "#8800cc" if (special and place not in ("city", "town")) else "#cc3300"
        out.append(
            f'  <circle cx="{x}" cy="{y}" r="{r}" '
            f'fill="{fc}" stroke="white" stroke-width="1"/>'
        )
        if name:
            out.append(
                f'  <text x="{x+r+2}" y="{y+4}" font-family="{LABEL_FONT}" '
                f'font-size="{fs}" font-weight="{fw}" fill="#111" '
                f'stroke="white" stroke-width="2.5" paint-order="stroke">'
                f'{xe(name)}</text>'
            )
    return out


def route_elements(routes, proj):
    """Return SVG strings for coloured route lines + annotations."""
    out = []
    LEADER = 50   # leader line length in px
    END_R  = 5    # radius of start/end dots

    for idx, (name, pts) in enumerate(routes):
        color = ROUTE_COLORS[idx % len(ROUTE_COLORS)]
        d = proj.path(pts)

        # Route line
        out.append(
            f'  <path d="{d}" stroke="{color}" stroke-width="2.5" '
            f'fill="none" stroke-linejoin="round" stroke-linecap="round" '
            f'opacity="0.85"/>'
        )

        # Start / end dots
        for lon, lat in (pts[0], pts[-1]):
            x, y = proj.xy(lon, lat)
            out.append(
                f'  <circle cx="{x}" cy="{y}" r="{END_R}" '
                f'fill="{color}" stroke="white" stroke-width="1.5"/>'
            )

        # Leader line + label at start
        sx, sy = proj.xy(pts[0][0], pts[0][1])
        # Direction: perpendicular-ish to initial bearing, pushed "up-left"
        if len(pts) > 1:
            bear = route_bearing_deg(pts[0], pts[1])
        else:
            bear = 0.0
        # Offset 90° counter-clockwise from bearing, normalised to LEADER px
        angle_rad = math.radians(bear - 90)
        dx = math.sin(angle_rad) * LEADER
        dy = -math.cos(angle_rad) * LEADER   # SVG Y inverted

        # Keep label on screen
        lx = max(10, min(SVG_W - 160, sx + dx))
        ly = max(20, sy + dy)

        label = xe(name)
        note = next((v for k, v in ROUTE_NOTES.items()
                     if k.lower() in name.lower()), "")

        out.append(
            f'  <line x1="{sx}" y1="{sy}" x2="{lx}" y2="{ly}" '
            f'stroke="{color}" stroke-width="1" stroke-dasharray="4,3" opacity="0.8"/>'
        )
        out.append(
            f'  <text x="{lx}" y="{ly - 3}" font-family="{LABEL_FONT}" '
            f'font-size="12" font-weight="bold" fill="{color}" '
            f'stroke="white" stroke-width="2.5" paint-order="stroke">'
            f'{label}</text>'
        )
        if note:
            out.append(
                f'  <text x="{lx}" y="{ly + 11}" font-family="{LABEL_FONT}" '
                f'font-size="10" fill="#333" '
                f'stroke="white" stroke-width="2" paint-order="stroke">'
                f'{xe(note)}</text>'
            )

    return out


def info_box(routes, svg_w, svg_h):
    """Return SVG strings for the info/legend box."""
    pad   = 12
    lh    = 20
    sw_w  = 28   # colour swatch width

    lines = [MAP_TITLE]
    entries = []
    for idx, (name, pts) in enumerate(routes):
        color = ROUTE_COLORS[idx % len(ROUTE_COLORS)]
        dist  = route_distance_nm(pts)
        note  = next((v for k, v in ROUTE_NOTES.items()
                      if k.lower() in name.lower()), "")
        entries.append((color, name, dist, note))

    box_w  = 260
    n_rows = 1 + len(entries) + (1 if MAP_TITLE else 0)
    box_h  = pad + lh + len(entries) * (lh + (lh if True else 0)) + pad
    # simpler: just compute from entries
    box_h = pad + lh + len(entries) * lh * (2 if any(e[3] for e in entries) else 1) + pad

    bx = svg_w - box_w - 16
    by = svg_h - box_h - 16

    out = [
        f'  <rect x="{bx-pad}" y="{by-pad}" width="{box_w+2*pad}" '
        f'height="{box_h+2*pad}" fill="white" fill-opacity="0.88" '
        f'rx="6" stroke="#aaa" stroke-width="0.8"/>',
        f'  <text x="{bx}" y="{by+4}" font-family="{LABEL_FONT}" '
        f'font-size="14" font-weight="bold" fill="#111">{xe(MAP_TITLE)}</text>',
    ]

    y = by + lh + 6
    for color, name, dist, note in entries:
        # Colour swatch
        out.append(
            f'  <rect x="{bx}" y="{y-10}" width="{sw_w}" height="6" '
            f'rx="2" fill="{color}"/>'
        )
        out.append(
            f'  <text x="{bx+sw_w+6}" y="{y}" font-family="{LABEL_FONT}" '
            f'font-size="12" font-weight="bold" fill="{color}">'
            f'{xe(name)} ({dist:.1f} nm)</text>'
        )
        y += lh
        if note:
            out.append(
                f'  <text x="{bx+sw_w+6}" y="{y}" font-family="{LABEL_FONT}" '
                f'font-size="10" fill="#444">{xe(note)}</text>'
            )
            y += lh - 4

    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    gpx_files = sys.argv[1:] or ["Navionics_archive_export.gpx"]

    # Parse all GPX files
    all_routes = []
    for path in gpx_files:
        all_routes.extend(parse_gpx(path))

    if not all_routes:
        print("No routes or tracks found in GPX files.")
        sys.exit(1)

    print(f"Found {len(all_routes)} route(s):")
    for name, pts in all_routes:
        print(f"  {name!r:40s} {len(pts)} pts  {route_distance_nm(pts):.1f} nm")

    # Fixed bbox: north Brittany coast, Morlaix to Granville
    south, west, north, east = 47.8, -3.95, 48.9, -1.5

    # Compute SVG height from aspect ratio
    mid_lat = (north + south) / 2
    lon_span = east - west
    lat_span = north - south
    aspect = (lon_span * math.cos(math.radians(mid_lat))) / lat_span
    svg_h = round(SVG_W / aspect)

    proj = Projection(south, west, north, east, SVG_W, svg_h)

    # Fetch OSM basemap
    data = query_overpass(south, west, north, east)

    # Build SVG
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {SVG_W} {svg_h}" '
        f'width="{SVG_W}" height="{svg_h}">',
        f'  <rect width="{SVG_W}" height="{svg_h}" fill="#dde8f0"/>',
    ]

    out += basemap_elements(data, proj)
    out += place_elements(data, proj)
    out += route_elements(all_routes, proj)
    out += info_box(all_routes, SVG_W, svg_h)

    out.append("</svg>")

    out_file = "sailing_routes.svg"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write("\n".join(out))

    print(f"\nSaved {out_file}")


if __name__ == "__main__":
    main()
