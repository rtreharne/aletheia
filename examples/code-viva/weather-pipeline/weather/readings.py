"""Load hourly readings from a CSV file."""

import csv
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

TIME_FORMAT = "%Y-%m-%d %H:%M"
MISSING = {"", "NA", "-"}


@dataclass
class Reading:
    time: datetime
    temp: Optional[float]  # degrees C, None if missing
    humidity: Optional[float]  # percent, None if missing


def _number(text):
    text = text.strip()
    if text in MISSING:
        return None
    return float(text)


def load_readings(path):
    """Read the CSV, skip rows without a usable timestamp, and return readings in time order."""
    readings = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                time = datetime.strptime(row["time"], TIME_FORMAT)
            except (KeyError, ValueError):
                continue
            readings.append(Reading(time, _number(row.get("temp", "")), _number(row.get("humidity", ""))))
    readings.sort(key=lambda r: r.time)
    return readings
