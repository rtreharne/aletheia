"""Summarise a CSV of hourly weather readings.

Usage: python main.py data/sample.csv [--threshold 27]
"""

import argparse

from weather.clean import fill_gaps, remove_implausible, remove_spikes
from weather.readings import load_readings
from weather.summary import HEATWAVE_THRESHOLD, daily_summaries, heatwaves


def main(argv=None):
    parser = argparse.ArgumentParser(description="Summarise hourly weather readings.")
    parser.add_argument("csv", help="CSV file with time, temp and humidity columns")
    parser.add_argument("--threshold", type=float, default=HEATWAVE_THRESHOLD, help="heatwave threshold in degrees C")
    args = parser.parse_args(argv)

    readings = load_readings(args.csv)
    # Implausible values first, so a 999 cannot make its neighbours look like spikes.
    implausible = remove_implausible(readings)
    spikes = remove_spikes(readings)
    filled = fill_gaps(readings)
    days = daily_summaries(readings)

    print(f"{len(readings)} readings: {implausible} implausible, {spikes} spikes, {filled} filled")
    print(f"{len(days)} days with enough readings")
    for d in days:
        print(f"{d.day}  low {d.low:5.1f}  high {d.high:5.1f}  mean {d.mean:5.1f}  ({d.count} readings)")
    waves = heatwaves(days, args.threshold)
    for first, last in waves:
        print(f"Heatwave: {first} to {last}")
    if not waves:
        print("No heatwaves")


if __name__ == "__main__":
    main()
