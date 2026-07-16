"""
Treadmill Virtual Routes
Loads GPX files from ~/treadmill_routes/ directory.
Each GPX becomes a route that the treadmill export can use.

Add routes by dropping .gpx files in ~/treadmill_routes/
File naming convention: distance_name.gpx
  Example: 10k_central_park.gpx, 21k_quebec_champlain.gpx, 42k_marathon_montreal.gpx

Routes loop if the run distance exceeds the route length.
"""

import json
import math
import os
import random
import xml.etree.ElementTree as ET

GPX_DIR = os.path.expanduser("~/treadmill_routes")

# Persisted list of recently-picked route ids, so consecutive 'random' runs
# don't repeat the same route (pure random.choice clusters and reads as "not
# random"). Overridable in tests. ROUTE_MEMORY = how many recent picks to
# exclude from the next draw — a shuffled "bag" the size of the catalogue.
HISTORY_PATH = os.path.expanduser("~/.treadmill_route_history.json")
ROUTE_MEMORY = 8

# Cache loaded routes, keyed to the GPX dir's mtime so a freshly dropped-in
# .gpx becomes visible without restarting the server.
_routes_cache = {}
_routes_cache_mtime = None


def _gpx_dir_mtime():
    try:
        return os.path.getmtime(GPX_DIR)
    except OSError:
        return None


# =============================================
# GPX PARSING
# =============================================

def parse_gpx(filepath):
    """Parse a GPX file and extract (lat, lon, ele) waypoints."""
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()

        # Handle GPX namespace
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        waypoints = []
        # Try trk/trkseg/trkpt first (track points)
        for trkpt in root.iter(f"{ns}trkpt"):
            lat = float(trkpt.attrib["lat"])
            lon = float(trkpt.attrib["lon"])
            ele_tag = trkpt.find(f"{ns}ele")
            ele = float(ele_tag.text) if ele_tag is not None else 0.0
            waypoints.append((lat, lon, ele))

        # Fall back to rte/rtept (route points)
        if not waypoints:
            for rtept in root.iter(f"{ns}rtept"):
                lat = float(rtept.attrib["lat"])
                lon = float(rtept.attrib["lon"])
                ele_tag = rtept.find(f"{ns}ele")
                ele = float(ele_tag.text) if ele_tag is not None else 0.0
                waypoints.append((lat, lon, ele))

        # Extract name. GPX uses <name>, not <n> — the earlier `{ns}n` selector
        # never matched anything so every route fell back to its filename.
        name_tag = root.find(f".//{ns}trk/{ns}name")
        if name_tag is None:
            name_tag = root.find(f".//{ns}metadata/{ns}name")
        name = name_tag.text if name_tag is not None else os.path.basename(filepath).replace(".gpx", "")

        return waypoints, name

    except Exception as e:
        print(f"[ROUTES] Error parsing {filepath}: {e}")
        return [], None


def route_id_from_filename(filename):
    """Convert filename to route id: '10k_central_park.gpx' -> 'central_park'"""
    name = os.path.basename(filename).replace(".gpx", "").lower()
    # Remove leading distance prefix like "10k_" or "21k_"
    parts = name.split("_", 1)
    if len(parts) > 1 and (parts[0].endswith("k") or parts[0].endswith("km")):
        return parts[1]
    return name


# =============================================
# DISTANCE UTILITIES
# =============================================

def haversine_m(lat1, lon1, lat2, lon2):
    """Distance in meters between two GPS points."""
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def compute_route_distances(waypoints):
    """Compute cumulative distances along waypoints. waypoints = [(lat,lon,...), ...]"""
    dists = [0.0]
    for i in range(1, len(waypoints)):
        d = haversine_m(waypoints[i-1][0], waypoints[i-1][1],
                        waypoints[i][0], waypoints[i][1])
        dists.append(dists[-1] + d)
    return dists


# =============================================
# ROUTE LOADING
# =============================================

def load_routes():
    """Scan GPX files but only load metadata (not full waypoints) to save memory."""
    global _routes_cache, _routes_cache_mtime

    os.makedirs(GPX_DIR, exist_ok=True)
    current_mtime = _gpx_dir_mtime()
    if _routes_cache and current_mtime == _routes_cache_mtime:
        return _routes_cache

    routes = {}

    # Built-in: stadium track (special handling)
    routes["piste_martinique"] = {
        "name": "Piste d'Athlétisme, Martinique",
        "distance_km": 0.4,
        "city": "Fort-de-France, Martinique",
        "base_altitude": 15,
        "waypoints": None,
        "source": "builtin",
        "is_loop": True,
    }

    # Scan GPX files - read only metadata, not full waypoints
    if os.path.isdir(GPX_DIR):
        for filename in sorted(os.listdir(GPX_DIR)):
            if not filename.lower().endswith(".gpx"):
                continue

            filepath = os.path.join(GPX_DIR, filename)
            route_id = route_id_from_filename(filename)

            try:
                tree = ET.parse(filepath)
                root = tree.getroot()
                ns = ""
                if root.tag.startswith("{"):
                    ns = root.tag.split("}")[0] + "}"

                trkpts = list(root.iter(f"{ns}trkpt"))
                if len(trkpts) < 2:
                    # Fall back to route points (<rtept>) — parse_gpx() reads
                    # them too, so a route-type GPX shouldn't be silently skipped.
                    trkpts = list(root.iter(f"{ns}rtept"))
                if len(trkpts) < 2:
                    continue

                # Get name. GPX tag is <name>, not <n>.
                name_tag = root.find(f".//{ns}trk/{ns}name")
                if name_tag is None:
                    name_tag = root.find(f".//{ns}metadata/{ns}name")
                name = name_tag.text if name_tag is not None else filename.replace(".gpx", "")

                # First/last for loop detection
                first_lat = float(trkpts[0].attrib["lat"])
                first_lon = float(trkpts[0].attrib["lon"])
                last_lat = float(trkpts[-1].attrib["lat"])
                last_lon = float(trkpts[-1].attrib["lon"])
                first_ele_tag = trkpts[0].find(f"{ns}ele")
                base_alt = float(first_ele_tag.text) if first_ele_tag is not None else 0
                gap = haversine_m(first_lat, first_lon, last_lat, last_lon)
                is_loop = gap < 200
                num_pts = len(trkpts)

                # Full distance over every point. Sampling ~20 chords used to
                # short-cut twisty routes badly, skewing the length filter in
                # pick_random_route(). The points are already in memory here.
                pts = [(float(p.attrib["lat"]), float(p.attrib["lon"])) for p in trkpts]
                total_m = sum(haversine_m(pts[i-1][0], pts[i-1][1],
                                          pts[i][0], pts[i][1])
                              for i in range(1, len(pts)))

                # Free XML memory
                del tree, root, trkpts

            except Exception as e:
                print(f"[ROUTES] Error scanning {filename}: {e}")
                continue

            if route_id in routes:
                route_id = route_id + "_" + str(len(routes))

            routes[route_id] = {
                "name": name or route_id,
                "distance_km": round(total_m / 1000.0, 2),
                "city": "",
                "base_altitude": base_alt,
                "waypoints": None,  # Lazy - loaded on demand
                "cumulative_dists": None,
                "source": filepath,
                "points": num_pts,
                "is_loop": is_loop,
            }

            loop_tag = "loop" if is_loop else "linear"
            print(f"[ROUTES] Found: {route_id} = {name} ({total_m/1000:.1f}km, {num_pts} pts, {loop_tag})")

    _routes_cache = routes
    _routes_cache_mtime = current_mtime
    return routes


def reload_routes():
    """Force reload of routes."""
    global _routes_cache, _routes_cache_mtime
    _routes_cache = {}
    _routes_cache_mtime = None
    return load_routes()


# =============================================
# ROUTE INTERPOLATION
# =============================================

def interpolate_route(waypoints, cumulative_dists, distance_m, is_loop=True):
    """
    Get (lat, lon) at a given distance along a route.
    Loop routes wrap; linear (point-to-point) routes stop at the end instead
    of teleporting back to the start (which drew a straight cross-country line
    and a pace spike on Strava).
    """
    route_len = cumulative_dists[-1]
    if route_len == 0:
        return (waypoints[0][0], waypoints[0][1])

    if is_loop:
        d = distance_m % route_len
    else:
        d = min(distance_m, route_len)

    # Binary search for segment
    lo, hi = 0, len(cumulative_dists) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if cumulative_dists[mid] <= d:
            lo = mid
        else:
            hi = mid

    seg_start = cumulative_dists[lo]
    seg_len = cumulative_dists[hi] - seg_start
    if seg_len == 0:
        return (waypoints[lo][0], waypoints[lo][1])

    frac = (d - seg_start) / seg_len
    lat = waypoints[lo][0] + frac * (waypoints[hi][0] - waypoints[lo][0])
    lon = waypoints[lo][1] + frac * (waypoints[hi][1] - waypoints[lo][1])
    return (lat, lon)


# =============================================
# PUBLIC API
# =============================================

def _ensure_waypoints_loaded(route):
    """Lazy-load full waypoints for a single route from its GPX file."""
    if route.get("waypoints") is not None:
        return True

    gpx_path = route.get("source")
    if not gpx_path or gpx_path == "builtin":
        return False

    waypoints, _ = parse_gpx(gpx_path)
    if not waypoints or len(waypoints) < 2:
        return False

    route["waypoints"] = waypoints
    route["cumulative_dists"] = compute_route_distances(waypoints)
    route["points"] = len(waypoints)
    print(f"[ROUTES] Loaded waypoints: {route['name']} ({len(waypoints)} pts)")
    return True


def get_route_point(route_id, distance_m):
    """Get GPS coordinates for a distance along a named route."""
    routes = load_routes()
    route = routes.get(route_id)
    if not route:
        return None

    # Lazy-load waypoints if needed
    if not _ensure_waypoints_loaded(route):
        return None

    return interpolate_route(route["waypoints"], route["cumulative_dists"],
                             distance_m, route.get("is_loop", True))


def get_route_info(route_id):
    """Get route metadata."""
    routes = load_routes()
    return routes.get(route_id)


def unload_route_waypoints(route_id):
    """Free waypoints memory after export is done."""
    routes = load_routes()
    route = routes.get(route_id)
    if route and route.get("source") != "builtin":
        route["waypoints"] = None
        route["cumulative_dists"] = None
        import gc
        gc.collect()


def _load_route_history():
    """Recently-picked route ids, oldest first. Never raises."""
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


def _save_route_history(history):
    """Persist the (trimmed) history. Never raises — history I/O must not
    break an export/upload if the disk is full or the file is read-only."""
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history[-40:], f)
    except Exception:
        pass


def pick_random_route(distance_km):
    """
    Pick a route at random for a 'random' run, avoiding recent repeats.

    Any route works geometrically: loops repeat naturally, linear routes just
    stop where you are. Two things make the pick feel good:

    - Distance rule: skip routes shorter than a third of the run distance
      (10 laps of a 2.5km park for a 25km run is ugly).
    - Anti-repeat: pure random.choice() clusters — it happily returns the
      same one or two routes several runs in a row, which reads as "not
      random" to a human. We keep a small persisted history and exclude the
      most-recently-used routes from the draw, so the picker cycles through
      the catalogue (a shuffled "bag") before any route can come back.

    (Note: the previous filter required `waypoints is not None`, but routes
    are lazy-loaded, so waypoints were always None at pick time — that made
    RANDOM silently collapse onto a single route. Waypoints get loaded later
    by _ensure_waypoints_loaded(), so we don't check them here.)
    """
    routes = load_routes()
    candidates = [(k, v) for k, v in routes.items()
                  if v.get("source") and v["source"] != "builtin"]

    if not candidates:
        return "piste_martinique"

    # Skip routes that are way too short (would need 3+ laps).
    min_km = distance_km / 3.0
    valid = [(k, v) for k, v in candidates if v["distance_km"] >= min_km]
    if not valid:  # very long run — everything is "too short", allow all
        valid = candidates

    valid_ids = [k for k, _ in valid]

    # Exclude the most-recent picks, but never so many that nothing is left:
    # cap the exclusion window at len(valid)-1 so at least one route remains.
    history = _load_route_history()
    window = min(ROUTE_MEMORY, len(valid_ids) - 1)
    recent = set(history[-window:]) if window > 0 else set()
    pool = [rid for rid in valid_ids if rid not in recent]
    if not pool:  # safety net — shouldn't trigger given the cap above
        pool = valid_ids

    chosen = random.choice(pool)
    history.append(chosen)
    _save_route_history(history)
    return chosen


def list_routes():
    """List all available routes."""
    routes = load_routes()
    result = []
    for route_id, info in routes.items():
        result.append({
            "id": route_id,
            "name": info["name"],
            "distance_km": info["distance_km"],
            "city": info.get("city", ""),
            "points": info.get("points", 0),
            "source": info.get("source", ""),
            "is_loop": info.get("is_loop", False),
        })
    result.sort(key=lambda x: x["distance_km"])
    return result


# =============================================
# CLI
# =============================================
if __name__ == "__main__":
    print(f"=== Virtual Routes ({GPX_DIR}) ===\n")
    routes = reload_routes()
    for r in list_routes():
        pts = f"{r['points']} pts" if r['points'] else "special"
        loop = "loop" if r.get('is_loop') else "linear" if r['points'] else ""
        print(f"  {r['distance_km']:6.1f} km | {r['name']:40s} | {pts:10s} | {loop}")
    print(f"\n  Total: {len(routes)} routes")
    print(f"  Add routes: drop .gpx files in {GPX_DIR}")

    # Test random picker for various distances
    print(f"\n  Random picks:")
    for d in [5, 8, 10, 12, 15, 21, 25]:
        picks = [pick_random_route(d) for _ in range(5)]
        unique = list(set(picks))
        print(f"    {d:3d}km -> {', '.join(unique)}")
