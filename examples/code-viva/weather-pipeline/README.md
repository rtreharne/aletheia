# Weather station pipeline

Reads hourly readings from a CSV, cleans them, and prints a summary for each day and any heatwaves.

```
python main.py data/sample.csv
python main.py data/sample.csv --threshold 25
python -m unittest
```

No packages beyond the standard library are needed.

## Steps

1. `load_readings` reads the CSV and sorts it by time. Rows with a bad timestamp are skipped.
2. `remove_implausible` blanks temperatures outside −40 to 50 °C (the sensor writes 999 when it fails).
3. `remove_spikes` blanks a reading that jumps more than 8 °C from both neighbours.
4. `fill_gaps` interpolates gaps of up to 3 hours.
5. `daily_summaries` skips days with fewer than 18 readings, so a day with a long outage does not give a misleading low or high.
6. `heatwaves` looks for 3 or more consecutive days with a high of at least 27 °C.
