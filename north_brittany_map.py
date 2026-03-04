#!/usr/bin/env python3
"""Generate SVG wireframe map of North Brittany from OpenStreetMap data."""

import requests
import sys

# Bounding box: south, west, north, east
# Morlaix (west) to Granville (east), northern Brittany coast
SOUTH, WEST, NORTH, EAST = 48.4, -3.95, 48.9, -1.5

# Aspect ratio: lon_span * cos(48.65°) / lat_span ≈ 2.45 * 0.661 / 0.5 ≈ 3.24
SVG_W = 1100
SVG_H = 340

# Towns to highlight specifically (may be "village" class in OSM)
NAMED_TOWNS = {"Lézardrieux", "Tréguier", "Saint-Quay-Portrieux"}


def to_xy(lon, lat):
    x = (lon - WEST) / (EAST - WEST) * SVG_W
    y = (NORTH - lat) / (NORTH - SOUTH) * SVG_H
    return round(x, 1), round(y, 1)


def query_overpass(query):
    mirrors = [
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass-api.de/api/interpreter",
    ]
    for url in mirrors:
        try:
            print(f"Querying {url} ...", flush=True)
            r = requests.post(url, data={"data": query}, timeout=120)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  failed: {e}", flush=True)
    raise RuntimeError("All Overpass mirrors failed")


def way_to_path(refs, nodes):
    pts = [nodes[r] for r in refs if r in nodes]
    if len(pts) < 2:
        return None
    coords = [to_xy(lon, lat) for lon, lat in pts]
    return "M " + " L ".join(f"{x},{y}" for x, y in coords)


def escape_xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def main():
    bbox = f"{SOUTH},{WEST},{NORTH},{EAST}"
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
    data = query_overpass(query)

    nodes = {}
    for el in data["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg"',
        f'  viewBox="0 0 {SVG_W} {SVG_H}"',
        f'  width="{SVG_W}" height="{SVG_H}">',
        f'',
        f'  <rect width="{SVG_W}" height="{SVG_H}" fill="#dde8f0"/>',
    ]

    # Styles per feature type
    styles = {
        "coastline":    'stroke="#1a4a7a" stroke-width="1.8" fill="none"',
        "water":        'stroke="#4a90c4" stroke-width="0.8" fill="#b8d4e8" fill-opacity="0.5"',
        "waterway":     'stroke="#4a90c4" stroke-width="1.2" fill="none"',
        "motorway":     'stroke="#cc3300" stroke-width="2" fill="none"',
        "trunk":        'stroke="#cc3300" stroke-width="1.5" fill="none"',
        "primary":      'stroke="#884400" stroke-width="1" fill="none"',
        "secondary":    'stroke="#665522" stroke-width="0.6" fill="none"',
        "admin":        'stroke="#555555" stroke-width="0.7" fill="none" stroke-dasharray="5,4"',
    }

    way_count = 0
    for el in data["elements"]:
        if el["type"] != "way":
            continue
        tags = el.get("tags", {})
        refs = el.get("nodes", [])
        path_d = way_to_path(refs, nodes)
        if not path_d:
            continue

        natural  = tags.get("natural", "")
        highway  = tags.get("highway", "")
        waterway = tags.get("waterway", "")
        boundary = tags.get("boundary", "")

        if natural == "coastline":
            style = styles["coastline"]
        elif natural == "water":
            style = styles["water"]
        elif waterway in ("river", "canal"):
            style = styles["waterway"]
        elif highway == "motorway":
            style = styles["motorway"]
        elif highway == "trunk":
            style = styles["trunk"]
        elif highway == "primary":
            style = styles["primary"]
        elif highway == "secondary":
            style = styles["secondary"]
        elif boundary == "administrative":
            style = styles["admin"]
        else:
            continue

        out.append(f'  <path d="{path_d}" {style}/>')
        way_count += 1

    # Place labels + dots
    # Include city/town from OSM, plus any node matching our named towns list
    def is_place_node(el):
        if el["type"] != "node":
            return False
        tags = el.get("tags", {})
        if tags.get("place") in ("city", "town"):
            return True
        name = tags.get("name", "")
        return any(nt.lower() in name.lower() for nt in NAMED_TOWNS)

    seen_names = set()
    places = []
    for el in data["elements"]:
        if is_place_node(el):
            name = el.get("tags", {}).get("name", "")
            if name not in seen_names:
                seen_names.add(name)
                places.append((el["lon"], el["lat"], el.get("tags", {})))

    for lon, lat, tags in places:
        x, y = to_xy(lon, lat)
        name = escape_xml(tags.get("name", ""))
        place = tags.get("place", "")
        is_named = any(nt.lower() in tags.get("name", "").lower() for nt in NAMED_TOWNS)
        is_city = place == "city"
        is_special = is_named and place not in ("city", "town")
        r = 5 if is_city else 4 if is_special else 3
        fs = 13 if is_city else 11 if is_special else 10
        fw = "bold" if is_city else "bold" if is_special else "normal"
        dot_color = "#8800cc" if is_special else "#cc3300"
        out.append(
            f'  <circle cx="{x}" cy="{y}" r="{r}" fill="{dot_color}" stroke="white" stroke-width="1"/>'
        )
        if name:
            out.append(
                f'  <text x="{x + r + 2}" y="{y + 4}" '
                f'font-family="sans-serif" font-size="{fs}" font-weight="{fw}" '
                f'fill="#1a1a1a" stroke="white" stroke-width="2.5" paint-order="stroke">'
                f'{name}</text>'
            )

    # Legend
    legend = [
        ("Coastline",      "#1a4a7a", "1.8"),
        ("Motorway/Trunk", "#cc3300", "2"),
        ("Primary road",   "#884400", "1"),
        ("River/Canal",    "#4a90c4", "1.2"),
        ("Admin boundary", "#555555", "0.7"),
    ]
    lx, ly = 20, SVG_H - 20 - len(legend) * 18
    out.append(f'  <rect x="{lx-6}" y="{ly-14}" width="170" height="{len(legend)*18+8}" '
               f'fill="white" fill-opacity="0.8" rx="4"/>')
    for i, (label, color, sw) in enumerate(legend):
        y_leg = ly + i * 18
        out.append(f'  <line x1="{lx}" y1="{y_leg}" x2="{lx+30}" y2="{y_leg}" '
                   f'stroke="{color}" stroke-width="{sw}"/>')
        out.append(f'  <text x="{lx+36}" y="{y_leg+4}" font-family="sans-serif" '
                   f'font-size="11" fill="#222">{label}</text>')

    out.append("</svg>")

    svg_text = "\n".join(out)
    out_file = "north_brittany.svg"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(svg_text)

    print(f"Saved {out_file}  ({way_count} ways, {len(places)} places)")


if __name__ == "__main__":
    main()
