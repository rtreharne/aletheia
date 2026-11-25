"""Daily summaries and heatwave detection."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean

MIN_READINGS_PER_DAY = 18
HEATWAVE_THRESHOLD = 27.0
HEATWAVE_MIN_DAYS = 3


@dataclass
class DaySummary:
    day: date
    low: float
    high: float
    mean: float
    count: int


def daily_summaries(readings, min_readings=MIN_READINGS_PER_DAY):
    """Summarise each day that has at least min_readings temperatures, in date order."""
    by_day = defaultdict(list)
    for r in readings:
        if r.temp is not None:
            by_day[r.time.date()].append(r.temp)
    summaries = []
    for day in sorted(by_day):
        temps = by_day[day]
        if len(temps) < min_readings:
            continue
        summaries.append(DaySummary(day, min(temps), max(temps), round(mean(temps), 1), len(temps)))
    return summaries


def heatwaves(summaries, threshold=HEATWAVE_THRESHOLD, min_days=HEATWAVE_MIN_DAYS):
    """Return (first_day, last_day) for each run of at least min_days consecutive hot days."""
    waves, run = [], []
    for s in summaries:
        hot = s.high >= threshold
        follows = bool(run) and s.day - run[-1].day == timedelta(days=1)
        if hot and (follows or not run):
            run.append(s)
            continue
        if len(run) >= min_days:
            waves.append((run[0].day, run[-1].day))
        run = [s] if hot else []
    if len(run) >= min_days:
        waves.append((run[0].day, run[-1].day))
    return waves
