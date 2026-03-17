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
from datetime import datetime, timedelta

LOG_DIR = os.path.expanduser("~/treadmill_logs")
EXPORT_DIR = os.path.expanduser("~/treadmill_exports")
BASE_ALTITUDE = 15.0  # Altitude reelle ~15m a Fort-de-France

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
            inclin_deg = float(row.get("inclin_corr", row.get("inclin_raw", 0)))
            distance_m = float(row.get("distance_m", 0))
            speed_corr = float(row.get("speed_corr_kmh", 0))
            elapsed_s = float(row.get("elapsed_s", 0))
            cadence = int(row.get("cadence", 0))
            heart_rate = int(row.get("heart_rate", 0))
        except (ValueError, TypeError):
            continue

        delta_dist = distance_m - prev_distance
        prev_distance = distance_m

        if delta_dist > 0 and abs(inclin_deg) > 0.1:
            delta_alt = delta_dist * math.sin(math.radians(inclin_deg))
            altitude += delta_alt
            if delta_alt > 0:
                total_gain += delta_alt
            else:
                total_loss += abs(delta_alt)

        profile.append({
            "elapsed_s": elapsed_s,
            "altitude": altitude,
            "distance_m": distance_m,
            "speed_kmh": speed_corr,
            "cadence": cadence,
            "heart_rate": heart_rate,
            "inclin_deg": inclin_deg,
            "total_gain": total_gain,
            "total_loss": total_loss,
        })

    return profile


def generate_tcx(profile, start_time, sport="Running"):
    if not profile:
        return ""

    total_time = profile[-1]["elapsed_s"]
    total_dist = profile[-1]["distance_m"]
    total_calories = int(total_time / 60 * 8)

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

        # Position GPS sur la piste d'athletisme
        x_m, y_m = generate_track_point(point["distance_m"])
        lat, lng = track_xy_to_latlon(x_m, y_m)

        lines.append('          <Trackpoint>')
        lines.append(f'            <Time>{time_str}</Time>')
        lines.append('            <Position>')
        lines.append(f'              <LatitudeDegrees>{lat:.7f}</LatitudeDegrees>')
        lines.append(f'              <LongitudeDegrees>{lng:.7f}</LongitudeDegrees>')
        lines.append('            </Position>')
        lines.append(f'            <AltitudeMeters>{point["altitude"]:.2f}</AltitudeMeters>')
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


def export_activity(csv_path, output_dir=None):
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
    try:
        ts_str = basename.replace("run_", "").replace(".csv", "")
        start_time = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
    except ValueError:
        start_time = datetime.now()

    tcx_content = generate_tcx(profile, start_time)

    tcx_filename = basename.replace(".csv", ".tcx")
    tcx_path = os.path.join(output_dir, tcx_filename)
    with open(tcx_path, "w") as f:
        f.write(tcx_content)

    last = profile[-1]
    total_time_min = last["elapsed_s"] / 60.0
    total_dist_km = last["distance_m"] / 1000.0
    avg_pace = total_time_min / total_dist_km if total_dist_km > 0.01 else 0
    pace_min = int(avg_pace)
    pace_sec = int((avg_pace - pace_min) * 60)

    summary = {
        "duration_min": total_time_min,
        "distance_km": total_dist_km,
        "avg_pace": f"{pace_min}:{pace_sec:02d} min/km",
        "elevation_gain": last["total_gain"],
        "elevation_loss": last["total_loss"],
        "points": len(profile),
        "tcx_path": tcx_path,
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
    if len(sys.argv) > 1:
        csv_path = sys.argv[1]
    else:
        activities = list_activities()
        if not activities:
            print("Aucune activite trouvee dans", LOG_DIR)
            sys.exit(1)
        csv_path = activities[0]["path"]
        print(f"Export: {os.path.basename(csv_path)}")

    tcx_path, result = export_activity(csv_path)
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
    print(f"  Piste:    Stade Pierre-Aliker ({get_track_perimeter():.0f}m/tour)")
    print(f"\nUpload: https://www.strava.com/upload/select")
    print(f"NE PAS taguer 'Treadmill' pour garder l'elevation!")
