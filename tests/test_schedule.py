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


class GithubCronTests(unittest.TestCase):
    def _schedule(self, **values):
        schedule = {"frequency": "weekly", "anchor_date": "2026-11-13", "time": "01:00", "timezone": "America/Toronto"}
        schedule.update(values)
        return schedule

    def test_toronto_weekly_gets_est_and_edt_lines_in_utc(self):
        from schedule import github_cron_lines

        # Friday 01:00 Toronto = Friday 06:00 UTC in winter (EST), 05:00 UTC in summer (EDT).
        self.assertEqual(github_cron_lines(self._schedule()), ["0 5 * * 5", "0 6 * * 5"])

    def test_crossing_midnight_moves_the_weekday(self):
        from schedule import github_cron_lines

        # Tuesday 22:30 Toronto is Wednesday 02:30/03:30 UTC.
        lines = github_cron_lines(self._schedule(anchor_date="2026-11-10", time="22:30"))
        self.assertEqual(lines, ["30 2 * * 3", "30 3 * * 3"])

    def test_zone_without_daylight_saving_gets_one_line(self):
        from schedule import github_cron_lines

        self.assertEqual(github_cron_lines(self._schedule(timezone="UTC")), ["0 1 * * 5"])
        self.assertEqual(github_cron_lines(self._schedule(frequency="daily", timezone="UTC")), ["0 1 * * *"])

    def test_only_the_matching_offset_line_proceeds(self):
        from zoneinfo import ZoneInfo
        from schedule import github_cron_for

        zone = ZoneInfo("America/Toronto")
        winter = datetime(2026, 11, 20, 1, 0, tzinfo=zone)
        summer = datetime(2027, 7, 2, 1, 0, tzinfo=zone)
        self.assertEqual(github_cron_for(self._schedule(), winter), "0 6 * * 5")
        self.assertEqual(github_cron_for(self._schedule(), summer), "0 5 * * 5")

    def test_workflow_skips_the_other_offsets_trigger(self):
        import os
        from unittest.mock import patch
        from zoneinfo import ZoneInfo

        import dedicated_workflow

        schedule = self._schedule(
            enabled=True, pine_stop_time="00:45", pine_start_time="01:45",
            minimum_start_buffer_minutes=5, allowed_lateness_minutes=30,
        )
        config = {"schedule": schedule, "dedicated_settings": {"schedule": schedule}}
        now = datetime(2026, 11, 13, 1, 5, tzinfo=ZoneInfo("America/Toronto"))

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz)

        with patch.object(dedicated_workflow, "datetime", FakeDatetime):
            with patch.dict(os.environ, {"PZ_SCHEDULE_CRON": "0 5 * * 5"}):
                allowed, message = dedicated_workflow._scheduled_window_is_open(config)
            self.assertFalse(allowed)
            self.assertIn("other daylight-saving offset", message)
            with patch.dict(os.environ, {"PZ_SCHEDULE_CRON": "0 6 * * 5"}):
                allowed, _ = dedicated_workflow._scheduled_window_is_open(config)
            self.assertTrue(allowed)

    def test_workflow_text_has_utc_lines_and_no_timezone_key(self):
        from dedicated_web_ui import _workflow_text

        text = _workflow_text(self._schedule(enabled=True))
        self.assertIn("    - cron: '0 5 * * 5'\n    - cron: '0 6 * * 5'\n", text)
        self.assertNotIn("timezone:", text)
        self.assertIn("PZ_SCHEDULE_CRON: ${{ github.event.schedule }}", text)


if __name__ == "__main__":
    unittest.main()
