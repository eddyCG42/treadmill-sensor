#!/usr/bin/env python3
"""
Treadmill Activity Exporter v3 - CSV to TCX for Strava
- Proper 400m stadium track shape (2 straights + 2 semicircles)
- Stade Pierre-Aliker, Fort-de-France, Martinique
- "with barometer" creator so Strava respects altitude data
- Elevation calculated from incline angle x distance
"""

import csv
import math
import os
import glob
import re
from datetime import datetime, timedelta, timezone

try:
    from treadmill_routes import get_route_point, get_route_info, pick_random_route, list_routes, unload_route_waypoints
    HAS_ROUTES = True
except ImportError:
    HAS_ROUTES = False

LOG_DIR = os.path.expanduser("~/treadmill_logs")
EXPORT_DIR = os.path.expanduser("~/treadmill_exports")
BASE_ALTITUDE = 15.0  # Altitude par defaut

# Rough running energy cost (~1 kcal per kg per km). Body mass is unknown to
# the sensor, so assume a value; Strava recomputes calories from its own model
# anyway. Still beats the old flat 8 kcal/min, which ignored pace entirely.
ASSUMED_BODY_MASS_KG = 70.0


# Shared with the live server so the summary-screen D+ and the TCX D+ match.
from treadmill_metrics import incline_pct_to_sin, elevation_delta


def _body_mass_kg():
    """Athlete mass from Pi config (falls back to the assumed default)."""
    try:
        import json
        with open(os.path.expanduser("~/treadmill_config.json"), encoding="utf-8") as f:
            v = float(json.load(f).get("body_mass_kg", ASSUMED_BODY_MASS_KG))
        return v if 30.0 <= v <= 200.0 else ASSUMED_BODY_MASS_KG
    except Exception:
        return ASSUMED_BODY_MASS_KG


# =====================================================
# STRAVA ACTIVITY NAMING
# The route is picked at random from the GPX library, but that choice used
# to be discarded: every upload landed on Strava as "Treadmill Run <ts>".
# These helpers turn the chosen route into a digestible title like
#   "Central Park · Matin · Treadmill"
# so the feed reads clearly and honestly flags the run as a treadmill/virtual
# route (not a real outdoor run at those GPS coordinates).
# =====================================================

# Curated pretty names for the shipped GPX routes, keyed by route_id
# (see treadmill_routes.route_id_from_filename). Anything not listed falls
# back to generic cleaning of the GPX name, so new .gpx drop-ins still work.
_ROUTE_DISPLAY_NAMES = {
    "central_park": "Central Park",
    "stanleypark_vancouver": "Stanley Park, Vancouver",
    "seawall_vancouver": "Seawall, Vancouver",
    "mont_royal": "Mont Royal",
    "montreal_old_port": "Vieux-Port, Montréal",
    "canal_lachine": "Canal de Lachine",
    "paris_seine": "Seine, Paris",
    "toronto_long": "Toronto Waterfront",
    "saint_laurent_side": "Bords du Saint-Laurent",
    "ile_aux_coudres": "Île aux Coudres",
    "park_lafontaine": "Parc La Fontaine",
    "2_5k_park_lafontaine": "Parc La Fontaine",
    "plaine_abraham": "Plaines d'Abraham",
    "tokyo_palace": "Tokyo, Palais Impérial",
    "champ_martinique": "Champ de Mars, Martinique",
    "osaka_castle": "Château d'Osaka",
    "piste_martinique": "Piste Martinique",
}

# French particles kept lowercase when title-casing a derived name.
_SMALL_WORDS = {"de", "du", "des", "la", "le", "les", "aux", "au", "et", "sur", "d"}


def _time_of_day(dt):
    """French time-of-day label matching Strava's Morning/Evening Run convention."""
    h = dt.hour
    if 5 <= h < 11:
        return "Matin"
    if 11 <= h < 14:
        return "Midi"
    if 14 <= h < 18:
        return "Après-midi"
    if 18 <= h < 22:
        return "Soir"
    return "Nuit"


def _clean_route_name(raw):
    """Best-effort prettify a raw GPX/filename route name.
    '16k_Mont_Royal' -> 'Mont Royal',  '5k_tokyo_palace' -> 'Tokyo Palace'.
    """
    if not raw:
        return "Course"
    name = raw.strip()
    # Strip a leading distance token: 10k_, 21km_, 2_5k_, 8k- ...
    name = re.sub(r"^\s*\d+(?:[._]\d+)?\s*k(?:m)?[\s_\-]+", "", name, flags=re.IGNORECASE)
    # Split camelCase runs like 'StanleyPark' / 'MyCentralParkLoop'
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    name = name.replace("_", " ").replace("-", " ")
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return "Course"

    words = []
    for i, w in enumerate(name.split(" ")):
        lw = w.lower()
        if i > 0 and lw in _SMALL_WORDS:
            words.append(lw)
        elif w.isupper() and len(w) > 1:
            words.append(w)  # keep acronyms as-is
        else:
            words.append(w[:1].upper() + w[1:].lower())
    return " ".join(words)


def route_display_name(route_id, raw_name=None):
    """Human-friendly name for a route: curated map first, else cleaned raw name."""
    if route_id in _ROUTE_DISPLAY_NAMES:
        return _ROUTE_DISPLAY_NAMES[route_id]
    # Strip the disambiguation suffix load_routes() adds to duplicate ids
    # ('central_park_9' -> 'central_park') before the second lookup.
    base_id = re.sub(r"_\d+$", "", route_id or "")
    if base_id in _ROUTE_DISPLAY_NAMES:
        return _ROUTE_DISPLAY_NAMES[base_id]
    return _clean_route_name(raw_name or route_id)


def build_activity_name(route_id, raw_name, start_time):
    """Compose the Strava activity title, e.g. 'Central Park · Soir · Treadmill'."""
    place = route_display_name(route_id, raw_name)
    return f"{place} · {_time_of_day(start_time)} · Treadmill"

# =====================================================
# PISTE D'ATHLETISME - Stade Pierre-Aliker
# Fort-de-France, Martinique (14.60334°N, 61.04621°W)
# Piste 400m standard IAAF, 8 couloirs
# =====================================================

# Dimensions standard piste 400m (lane 4, ~38.2m radius)
STRAIGHT_LENGTH = 84.39  # metres
CURVE_RADIUS = 38.2      # metres (lane 4-5 pour un trace realiste)
# Perimetre: 2 * 84.39 + 2 * pi * 38.2 = 168.78 + 239.96 = 408.74m (lane 4)

# Centre du stade et orientation
CENTER_LAT = 14.60334
CENTER_LNG = -61.04621
# Rotation de la piste par rapport au nord (degres, sens horaire)
# D'apres la vue satellite, la piste est orientee ~345 degres (NNW-SSE)
TRACK_ROTATION_DEG = -15.0

# Conversion metres -> degres a cette latitude
M_PER_DEG_LAT = 110574.0  # metres par degre de latitude
M_PER_DEG_LNG = 110574.0 * math.cos(math.radians(CENTER_LAT))  # ~107000


def generate_track_point(distance_m):
    """
    Genere un point (x_m, y_m) relatif au centre de la piste.
    Forme: 2 droites + 2 demi-cercles (sens anti-horaire vu du dessus).
    Y pointe vers le nord, X vers l'est.
    """
    half_straight = STRAIGHT_LENGTH / 2.0
    semi_arc = math.pi * CURVE_RADIUS
    total_perimeter = 2 * STRAIGHT_LENGTH + 2 * semi_arc
    
    d = distance_m % total_perimeter
    
    # Section 1: Droite est (sud vers nord)
    if d < STRAIGHT_LENGTH:
        frac = d / STRAIGHT_LENGTH
        x = CURVE_RADIUS
        y = -half_straight + frac * STRAIGHT_LENGTH
        return x, y
    d -= STRAIGHT_LENGTH
    
    # Section 2: Demi-cercle nord (centre en (0, +half_straight))
    # De angle=0 (est) a angle=pi (ouest), sens anti-horaire
    if d < semi_arc:
        angle = d / CURVE_RADIUS  # 0 -> pi
        x = CURVE_RADIUS * math.cos(angle)
        y = half_straight + CURVE_RADIUS * math.sin(angle)
        return x, y
    d -= semi_arc
    
    # Section 3: Droite ouest (nord vers sud)
    if d < STRAIGHT_LENGTH:
        frac = d / STRAIGHT_LENGTH
        x = -CURVE_RADIUS
        y = half_straight - frac * STRAIGHT_LENGTH
        return x, y
    d -= STRAIGHT_LENGTH
    
    # Section 4: Demi-cercle sud (centre en (0, -half_straight))
    # De angle=pi (ouest) a angle=2*pi (est), sens anti-horaire
    angle = math.pi + d / CURVE_RADIUS  # pi -> 2*pi
    x = CURVE_RADIUS * math.cos(angle)
    y = -half_straight + CURVE_RADIUS * math.sin(angle)
    return x, y


def track_xy_to_latlon(x_m, y_m):
    """
    Convertit des coordonnees (x, y) en metres relatives au centre de la piste
    en coordonnees GPS (lat, lon), avec rotation.
    """
    # Appliquer la rotation
    rot_rad = math.radians(TRACK_ROTATION_DEG)
    rx = x_m * math.cos(rot_rad) - y_m * math.sin(rot_rad)
    ry = x_m * math.sin(rot_rad) + y_m * math.cos(rot_rad)
    
    # Convertir en degres
    lat = CENTER_LAT + ry / M_PER_DEG_LAT
    lng = CENTER_LNG + rx / M_PER_DEG_LNG
    return lat, lng


def get_track_perimeter():
    return 2 * STRAIGHT_LENGTH + 2 * math.pi * CURVE_RADIUS


def load_csv_activity(filepath):
    rows = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def compute_elevation_profile(rows):
    altitude = BASE_ALTITUDE
    total_gain = 0.0
    total_loss = 0.0
    prev_distance = 0.0
    profile = []

    for row in rows:
        try:
            inclin_pct = float(row.get("inclin_corr", row.get("inclin_raw", 0)))
            distance_m = float(row.get("distance_m", 0))
            # pace_factor is applied to displayed speed AND distance; apply it
            # here too so the exported route length, TCX <DistanceMeters> and
            # <Speed> all agree with what the dashboard showed live.
            pace_factor = float(row.get("pace_factor", 1.0) or 1.0)
            distance_m *= pace_factor
            speed_corr = float(row.get("speed_corr_kmh", 0))
            elapsed_s = float(row.get("elapsed_s", 0))
            cadence = int(row.get("cadence", 0))
            heart_rate = int(row.get("heart_rate", 0))
        except (ValueError, TypeError):
            continue

        delta_dist = distance_m - prev_distance
        prev_distance = distance_m

        # Shared guard + math with the live server (glitch-guarded).
        delta_alt = elevation_delta(delta_dist, inclin_pct)
        altitude += delta_alt
        if delta_alt > 0:
            total_gain += delta_alt
        elif delta_alt < 0:
            total_loss += -delta_alt

        profile.append({
            "elapsed_s": elapsed_s,
            "altitude": altitude,
            "distance_m": distance_m,
            "speed_kmh": speed_corr,
            "cadence": cadence,
            "heart_rate": heart_rate,
            "inclin_pct": inclin_pct,
            "total_gain": total_gain,
            "total_loss": total_loss,
        })

    return profile


def generate_tcx(profile, start_time, sport="Running", route_id=None):
    if not profile:
        return ""

    # Determine route. Waypoints are lazy-loaded by treadmill_routes — at
    # this point route_info["waypoints"] is None even for valid routes.
    # The previous check `if route_info.get("waypoints")` was therefore
    # ALWAYS false, silently falling back to the Martinique synthetic
    # track even in RANDOM mode (visible in TCX files as the 14.602° lat
    # of Stade Pierre-Aliker). Trigger the lazy load by fetching point 0;
    # if it returns None, the route really is unusable and we fall back.
    use_virtual_route = False
    route_info = None
    if HAS_ROUTES and route_id and route_id != "piste_martinique":
        if get_route_point(route_id, 0) is not None:
            route_info = get_route_info(route_id)
            use_virtual_route = True

    total_time = profile[-1]["elapsed_s"]
    total_dist = profile[-1]["distance_m"]
    # Running energy cost ~1.036 kcal/kg/km — scales with distance instead of
    # the old flat 8 kcal/min that ignored pace entirely.
    total_calories = max(1, int((total_dist / 1000.0) * _body_mass_kg() * 1.036))

    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<TrainingCenterDatabase')
    lines.append('  xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"')
    lines.append('  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
    lines.append('  xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2"')
    lines.append('  xsi:schemaLocation="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2')
    lines.append('  http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd">')
    lines.append('  <Activities>')
    lines.append(f'    <Activity Sport="{sport}">')
    lines.append(f'      <Id>{start_time.strftime("%Y-%m-%dT%H:%M:%SZ")}</Id>')

    lap_time = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f'      <Lap StartTime="{lap_time}">')
    lines.append(f'        <TotalTimeSeconds>{total_time:.1f}</TotalTimeSeconds>')
    lines.append(f'        <DistanceMeters>{total_dist:.2f}</DistanceMeters>')
    lines.append(f'        <Calories>{total_calories}</Calories>')

    # Heart rate summary
    hr_values = [p["heart_rate"] for p in profile if p.get("heart_rate", 0) > 0]
    if hr_values:
        avg_hr = int(sum(hr_values) / len(hr_values))
        max_hr = max(hr_values)
        lines.append('        <AverageHeartRateBpm>')
        lines.append(f'          <Value>{avg_hr}</Value>')
        lines.append('        </AverageHeartRateBpm>')
        lines.append('        <MaximumHeartRateBpm>')
        lines.append(f'          <Value>{max_hr}</Value>')
        lines.append('        </MaximumHeartRateBpm>')

    lines.append('        <Intensity>Active</Intensity>')
    lines.append('        <TriggerMethod>Manual</TriggerMethod>')
    lines.append('        <Track>')

    for point in profile:
        t = start_time + timedelta(seconds=point["elapsed_s"])
        time_str = t.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Position GPS
        if use_virtual_route:
            gps = get_route_point(route_id, point["distance_m"])
            lat, lng = gps[0], gps[1]
            # Use route base altitude + treadmill incline delta
            if route_info:
                base_alt = route_info.get("base_altitude", BASE_ALTITUDE)
                # Keep treadmill-measured altitude changes relative to route base
                alt_delta = point["altitude"] - BASE_ALTITUDE
                altitude = base_alt + alt_delta
            else:
                altitude = point["altitude"]
        else:
            x_m, y_m = generate_track_point(point["distance_m"])
            lat, lng = track_xy_to_latlon(x_m, y_m)
            altitude = point["altitude"]

        lines.append('          <Trackpoint>')
        lines.append(f'            <Time>{time_str}</Time>')
        lines.append('            <Position>')
        lines.append(f'              <LatitudeDegrees>{lat:.7f}</LatitudeDegrees>')
        lines.append(f'              <LongitudeDegrees>{lng:.7f}</LongitudeDegrees>')
        lines.append('            </Position>')
        lines.append(f'            <AltitudeMeters>{altitude:.2f}</AltitudeMeters>')
        lines.append(f'            <DistanceMeters>{point["distance_m"]:.2f}</DistanceMeters>')

        if point.get("heart_rate", 0) > 0:
            lines.append('            <HeartRateBpm>')
            lines.append(f'              <Value>{point["heart_rate"]}</Value>')
            lines.append('            </HeartRateBpm>')

        if point["cadence"] > 0:
            lines.append('            <Extensions>')
            lines.append('              <ns3:TPX>')
            speed_ms = point["speed_kmh"] / 3.6
            lines.append(f'                <ns3:Speed>{speed_ms:.4f}</ns3:Speed>')
            run_cad = max(0, point["cadence"] // 2)
            lines.append(f'                <ns3:RunCadence>{run_cad}</ns3:RunCadence>')
            lines.append('              </ns3:TPX>')
            lines.append('            </Extensions>')

        lines.append('          </Trackpoint>')

    lines.append('        </Track>')
    lines.append('      </Lap>')

    # Creator: "with barometer" force Strava a respecter nos altitudes
    lines.append('      <Creator xsi:type="Device_t">')
    lines.append('        <Name>Treadmill Sensor with barometer</Name>')
    lines.append('        <UnitId>0</UnitId>')
    lines.append('        <ProductID>0</ProductID>')
    lines.append('        <Version>')
    lines.append('          <VersionMajor>1</VersionMajor>')
    lines.append('          <VersionMinor>0</VersionMinor>')
    lines.append('        </Version>')
    lines.append('      </Creator>')

    lines.append('    </Activity>')
    lines.append('  </Activities>')
    lines.append('</TrainingCenterDatabase>')

    return "\n".join(lines)


def export_activity(csv_path, output_dir=None, route_id=None):
    """
    Export CSV to TCX with virtual GPS route.
    route_id: 'piste_martinique' for track, 'random' for auto-pick,
              or a specific route id from treadmill_routes.py.
              None defaults to 'random'.
    """
    if output_dir is None:
        output_dir = EXPORT_DIR
    os.makedirs(output_dir, exist_ok=True)

    rows = load_csv_activity(csv_path)
    if not rows:
        return None, "Fichier CSV vide"

    profile = compute_elevation_profile(rows)
    if not profile:
        return None, "Aucune donnee valide"

    basename = os.path.basename(csv_path)
    # start_time_local drives the activity title (time-of-day must be LOCAL).
    try:
        ts_str = basename.replace("run_", "").replace(".csv", "")
        start_time_local = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
    except ValueError:
        start_time_local = datetime.now()

    # Route selection
    if route_id is None:
        route_id = "piste_martinique"

    total_dist_km = profile[-1]["distance_m"] / 1000.0

    if route_id == "random" and HAS_ROUTES:
        route_id = pick_random_route(total_dist_km)

    # The TCX <Time> fields are stamped with a trailing "Z" (UTC), so the
    # timestamps must actually be UTC. The filename is local time; interpret
    # it as local and convert, otherwise Strava shows the run 4-5h off.
    start_time_utc = start_time_local.astimezone(timezone.utc)
    tcx_content = generate_tcx(profile, start_time_utc, route_id=route_id)

    tcx_filename = basename.replace(".csv", ".tcx")
    tcx_path = os.path.join(output_dir, tcx_filename)
    with open(tcx_path, "w") as f:
        f.write(tcx_content)

    # Free route waypoints memory
    if HAS_ROUTES and route_id != "piste_martinique":
        try:
            unload_route_waypoints(route_id)
        except:
            pass

    last = profile[-1]
    total_time_min = last["elapsed_s"] / 60.0
    avg_pace = total_time_min / total_dist_km if total_dist_km > 0.01 else 0
    pace_min = int(avg_pace)
    pace_sec = int((avg_pace - pace_min) * 60)

    raw_route_name = None
    if HAS_ROUTES and route_id != "piste_martinique":
        ri = get_route_info(route_id)
        if ri:
            raw_route_name = ri["name"]

    route_name = route_display_name(route_id, raw_route_name)
    strava_name = build_activity_name(route_id, raw_route_name, start_time_local)

    summary = {
        "duration_min": total_time_min,
        "distance_km": total_dist_km,
        "avg_pace": f"{pace_min}:{pace_sec:02d} min/km",
        "elevation_gain": last["total_gain"],
        "elevation_loss": last["total_loss"],
        "points": len(profile),
        "tcx_path": tcx_path,
        "route": route_name,
        "strava_name": strava_name,
    }

    return tcx_path, summary


def list_activities():
    files = sorted(glob.glob(os.path.join(LOG_DIR, "run_*.csv")),
                   key=os.path.getmtime, reverse=True)
    activities = []
    for f in files:
        size = os.path.getsize(f)
        basename = os.path.basename(f)
        tcx_path = os.path.join(EXPORT_DIR, basename.replace(".csv", ".tcx"))
        exported = os.path.exists(tcx_path)
        activities.append({
            "path": f, "name": basename, "size_kb": size / 1024,
            "exported": exported, "tcx_path": tcx_path if exported else None,
        })
    return activities


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "routes":
        if HAS_ROUTES:
            print("=== Routes Virtuelles ===\n")
            for r in list_routes():
                print(f"  {r['id']:20s}  {r['distance_km']:6.1f} km  {r['name']}")
        else:
            print("Module treadmill_routes.py non trouvé")
        sys.exit(0)

    csv_path = None
    route_id = "piste_martinique"

    for arg in sys.argv[1:]:
        if arg.startswith("--route="):
            route_id = arg.split("=", 1)[1]
        elif not csv_path:
            csv_path = arg

    if not csv_path:
        activities = list_activities()
        if not activities:
            print("Aucune activite trouvee dans", LOG_DIR)
            sys.exit(1)
        csv_path = activities[0]["path"]
        print(f"Export: {os.path.basename(csv_path)}")

    tcx_path, result = export_activity(csv_path, route_id=route_id)
    if tcx_path is None:
        print(f"Erreur: {result}")
        sys.exit(1)

    print(f"\nExport reussi!")
    print(f"  Fichier:  {tcx_path}")
    print(f"  Duree:    {result['duration_min']:.1f} min")
    print(f"  Distance: {result['distance_km']:.2f} km")
    print(f"  Pace:     {result['avg_pace']}")
    print(f"  D+:       {result['elevation_gain']:.1f} m")
    print(f"  D-:       {result['elevation_loss']:.1f} m")
    print(f"  Route:    {result['route']}")
    print(f"\nUpload: https://www.strava.com/upload/select")
