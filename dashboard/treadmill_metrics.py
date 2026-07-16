"""Shared treadmill metric math.

Imported by BOTH the live server (`web_dashboard/treadmill_server.py`) and the
offline TCX exporter (`treadmill_export.py`) so the elevation shown on the
run-summary screen and the elevation written into the uploaded Strava activity
are computed the same way. They used to have separate copies with different
glitch guards (50 m vs 100 m), which made the two numbers disagree.
"""

import math

# Largest plausible horizontal advance between two consecutive samples (metres).
# A bigger jump means a distance-counter reset/glitch, not real running, so it
# must not be folded into elevation gain.
ELEV_MAX_STEP_M = 50.0


def incline_pct_to_sin(pct: float) -> float:
    """Vertical fraction of along-slope distance for a treadmill % grade.

    Incline is a PERCENT grade, not degrees; sin(radians(pct)) over-counts
    elevation by ~1.7x at 12%. For grade g = pct/100 the vertical fraction is
    g / sqrt(1 + g^2).
    """
    g = pct / 100.0
    return g / math.sqrt(1.0 + g * g)


def elevation_delta(delta_dist_m: float, incline_pct: float) -> float:
    """Signed vertical metres for a horizontal advance at a given grade.

    Returns 0 when the advance is non-positive or implausibly large (glitch
    guard), so a counter reset can't dump a multi-km jump into elevation.
    """
    if not (0.0 < delta_dist_m < ELEV_MAX_STEP_M):
        return 0.0
    return delta_dist_m * incline_pct_to_sin(incline_pct)
