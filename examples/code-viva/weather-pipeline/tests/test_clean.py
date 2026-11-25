import unittest
from datetime import datetime, timedelta

from weather.clean import fill_gaps, remove_implausible, remove_spikes
from weather.readings import Reading

START = datetime(2025, 7, 14)


def make(temps):
    return [Reading(START + timedelta(hours=h), t, 60.0) for h, t in enumerate(temps)]


def temps(readings):
    return [r.temp for r in readings]


class RemoveImplausibleTests(unittest.TestCase):
    def test_out_of_range_becomes_missing(self):
        readings = make([12.0, 999.0, -60.0, 13.0])
        self.assertEqual(remove_implausible(readings), 2)
        self.assertEqual(temps(readings), [12.0, None, None, 13.0])

    def test_limits_are_inclusive(self):
        readings = make([-40.0, 50.0])
        self.assertEqual(remove_implausible(readings), 0)


class RemoveSpikesTests(unittest.TestCase):
    def test_isolated_spike_removed(self):
        readings = make([15.0, 16.0, 30.0, 16.5, 17.0])
        self.assertEqual(remove_spikes(readings), 1)
        self.assertEqual(temps(readings), [15.0, 16.0, None, 16.5, 17.0])

    def test_steady_rise_kept(self):
        readings = make([15.0, 20.0, 25.0, 30.0])
        self.assertEqual(remove_spikes(readings), 0)


class FillGapsTests(unittest.TestCase):
    def test_short_gap_interpolated(self):
        readings = make([10.0, None, None, 16.0])
        self.assertEqual(fill_gaps(readings), 2)
        self.assertEqual(temps(readings), [10.0, 12.0, 14.0, 16.0])

    def test_long_gap_left_missing(self):
        readings = make([10.0, None, None, None, None, 20.0])
        self.assertEqual(fill_gaps(readings), 0)

    def test_gap_at_start_left_missing(self):
        readings = make([None, 11.0, 12.0])
        self.assertEqual(fill_gaps(readings), 0)
        self.assertIsNone(readings[0].temp)


if __name__ == "__main__":
    unittest.main()
