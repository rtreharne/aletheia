import unittest
from datetime import date, datetime, timedelta

from weather.readings import Reading
from weather.summary import DaySummary, daily_summaries, heatwaves


def day(d, high):
    return DaySummary(date(2025, 7, d), high - 10, high, high - 5, 24)


class DailySummaryTests(unittest.TestCase):
    def test_summary_values(self):
        start = datetime(2025, 7, 14)
        readings = [Reading(start + timedelta(hours=h), float(h), 50.0) for h in range(24)]
        (summary,) = daily_summaries(readings)
        self.assertEqual((summary.low, summary.high, summary.mean, summary.count), (0.0, 23.0, 11.5, 24))

    def test_day_with_too_few_readings_skipped(self):
        start = datetime(2025, 7, 14)
        readings = [Reading(start + timedelta(hours=h), 20.0, 50.0) for h in range(10)]
        self.assertEqual(daily_summaries(readings), [])


class HeatwaveTests(unittest.TestCase):
    def test_three_hot_days(self):
        days = [day(14, 24), day(15, 28), day(16, 29), day(17, 30), day(18, 25)]
        self.assertEqual(heatwaves(days), [(date(2025, 7, 15), date(2025, 7, 17))])

    def test_two_hot_days_not_enough(self):
        days = [day(14, 28), day(15, 29), day(16, 20)]
        self.assertEqual(heatwaves(days), [])

    def test_missing_day_breaks_run(self):
        days = [day(14, 28), day(15, 29), day(17, 30)]
        self.assertEqual(heatwaves(days), [])

    def test_run_at_end(self):
        days = [day(14, 20), day(15, 28), day(16, 28), day(17, 28)]
        self.assertEqual(heatwaves(days), [(date(2025, 7, 15), date(2025, 7, 17))])


if __name__ == "__main__":
    unittest.main()
