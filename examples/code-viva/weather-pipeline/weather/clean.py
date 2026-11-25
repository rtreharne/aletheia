"""Remove bad temperatures and fill short gaps.

Every function here changes the readings in place.
"""

TEMP_RANGE = (-40.0, 50.0)
MAX_JUMP = 8.0  # degrees C in one hour
MAX_GAP_HOURS = 3


def remove_implausible(readings, low=TEMP_RANGE[0], high=TEMP_RANGE[1]):
    """Set temperatures outside [low, high] to None, keeping the reading itself."""
    removed = 0
    for r in readings:
        if r.temp is not None and not low <= r.temp <= high:
            r.temp = None
            removed += 1
    return removed


def remove_spikes(readings, max_jump=MAX_JUMP):
    """Set a temperature to None if it jumps more than max_jump away from both neighbours."""
    removed = 0
    for i in range(1, len(readings) - 1):
        prev, cur, nxt = readings[i - 1].temp, readings[i].temp, readings[i + 1].temp
        if None in (prev, cur, nxt):
            continue
        if abs(cur - prev) > max_jump and abs(cur - nxt) > max_jump:
            readings[i].temp = None
            removed += 1
    return removed


def fill_gaps(readings, max_gap=MAX_GAP_HOURS):
    """Linearly interpolate runs of up to max_gap missing temperatures.

    Runs at the very start or end, and longer runs, are left missing.
    Returns the number of temperatures filled.
    """
    filled = 0
    i = 0
    while i < len(readings):
        if readings[i].temp is not None:
            i += 1
            continue
        start = i
        while i < len(readings) and readings[i].temp is None:
            i += 1
        # readings[start:i] are missing
        if start == 0 or i == len(readings) or i - start > max_gap:
            continue
        before, after = readings[start - 1].temp, readings[i].temp
        steps = i - start + 1
        for k in range(start, i):
            fraction = (k - start + 1) / steps
            readings[k].temp = round(before + (after - before) * fraction, 1)
            filled += 1
    return filled
