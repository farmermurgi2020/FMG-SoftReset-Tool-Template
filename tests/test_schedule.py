import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import ConfigError
from schedule import fits_provider_window, next_run, occurrence_on_or_after


class ScheduleTests(unittest.TestCase):
    def test_daily_run_uses_anchor_and_timezone(self):
        config = {
            "frequency": "daily",
            "anchor_date": "2026-11-09",
            "time": "03:00",
            "timezone": "America/New_York",
        }
        result = next_run(config, datetime(2026, 11, 9, 2, 59, tzinfo=ZoneInfo("America/New_York")))
        self.assertEqual(result.isoformat(), "2026-11-09T03:00:00-05:00")

    def test_next_run_keeps_occurrence_within_lateness_grace(self):
        config = {
            "frequency": "daily",
            "anchor_date": "2026-11-09",
            "time": "03:00",
            "timezone": "UTC",
            "allowed_lateness_minutes": 10,
        }
        result = next_run(config, datetime(2026, 11, 9, 3, 5, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(result.date(), date(2026, 11, 9))

    def test_weekly_run_keeps_anchor_weekday(self):
        config = {"frequency": "weekly", "anchor_date": "2026-11-09"}
        self.assertEqual(occurrence_on_or_after(config, date(2026, 11, 10)), date(2026, 11, 16))

    def test_monthly_run_clamps_short_month(self):
        config = {"frequency": "monthly", "anchor_date": "2026-01-31"}
        self.assertEqual(occurrence_on_or_after(config, date(2026, 2, 1)), date(2026, 2, 28))

    def test_maintenance_window_includes_buffer(self):
        config = {
            "time": "03:00",
            "pine_stop_time": "02:30",
            "pine_start_time": "06:00",
            "minimum_start_buffer_minutes": 30,
        }
        self.assertTrue(fits_provider_window(config))
        config["time"] = "02:45"
        self.assertFalse(fits_provider_window(config))

    def test_overnight_maintenance_window(self):
        config = {
            "time": "01:00",
            "pine_stop_time": "23:00",
            "pine_start_time": "04:00",
            "minimum_start_buffer_minutes": 30,
        }
        self.assertTrue(fits_provider_window(config))
        config["time"] = "22:00"
        self.assertFalse(fits_provider_window(config))

    def test_unknown_frequency_is_rejected(self):
        with self.assertRaises(ConfigError):
            occurrence_on_or_after({"frequency": "hourly", "anchor_date": "2026-01-01"},
                                   date(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
