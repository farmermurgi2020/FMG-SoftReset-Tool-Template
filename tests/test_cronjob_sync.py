import json
import unittest

from config import ConfigError
from cronjob_sync import cronjob_schedule, describe_schedule, pause_cronjob, sync_cronjob


SCHEDULE = {"frequency": "weekly", "anchor_date": "2026-11-13", "time": "01:45", "timezone": "America/Toronto"}
URL = "https://api.github.com/repos/owner/repo/actions/workflows/pz-dedicated-reset.yml/dispatches"


class FakeApi:
    def __init__(self, jobs=()):
        self.jobs = list(jobs)
        self.calls = []

    def __call__(self, api_key, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET":
            return {"jobs": self.jobs}
        if method == "PUT":
            return {"jobId": 42}
        return {}


class CronjobScheduleTests(unittest.TestCase):
    def test_weekly_uses_local_time_and_anchor_weekday(self):
        result = cronjob_schedule(SCHEDULE)
        self.assertEqual(result["timezone"], "America/Toronto")
        self.assertEqual((result["hours"], result["minutes"], result["wdays"], result["mdays"]), ([1], [45], [5], [-1]))
        self.assertEqual(describe_schedule(SCHEDULE), "every Friday at 01:45 America/Toronto")

    def test_daily_and_monthly(self):
        self.assertEqual(cronjob_schedule({**SCHEDULE, "frequency": "daily"})["wdays"], [-1])
        self.assertEqual(cronjob_schedule({**SCHEDULE, "frequency": "monthly"})["mdays"], [13])
        # The 31st doesn't exist in every month: fire 28-31, the worker picks the real reset date.
        self.assertEqual(cronjob_schedule({**SCHEDULE, "frequency": "monthly", "anchor_date": "2026-01-31"})["mdays"],
                         [28, 29, 30, 31])


class SyncCronjobTests(unittest.TestCase):
    def test_creates_job_that_dispatches_a_scheduled_run(self):
        api = FakeApi()
        summary = sync_cronjob("key", "gh-token", "owner", "repo", "main", SCHEDULE, True, request=api)
        method, path, payload = api.calls[-1]
        self.assertEqual((method, path), ("PUT", "/jobs"))
        job = payload["job"]
        self.assertEqual(job["url"], URL)
        self.assertEqual(job["requestMethod"], 1)
        self.assertTrue(job["enabled"])
        self.assertEqual(job["extendedData"]["headers"]["Authorization"], "Bearer gh-token")
        self.assertEqual(json.loads(job["extendedData"]["body"]),
                         {"ref": "main", "inputs": {"action": "scheduled", "confirm_server_name": ""}})
        self.assertIn("created", summary)
        self.assertNotIn("gh-token", summary)

    def test_updates_existing_job_and_keeps_token_when_blank(self):
        api = FakeApi([{"jobId": 7, "url": URL}, {"jobId": 8, "url": "https://example.invalid"}])
        summary = sync_cronjob("key", "", "owner", "repo", "main", SCHEDULE, True, request=api)
        method, path, payload = api.calls[-1]
        self.assertEqual((method, path), ("PATCH", "/jobs/7"))
        self.assertNotIn("extendedData", payload["job"])
        self.assertIn("token kept", summary)

    def test_new_job_needs_the_github_token(self):
        with self.assertRaises(ConfigError):
            sync_cronjob("key", "", "owner", "repo", "main", SCHEDULE, True, request=FakeApi())

    def test_disabled_schedule_pauses_and_never_creates(self):
        api = FakeApi()
        sync_cronjob("key", "gh-token", "owner", "repo", "main", SCHEDULE, False, request=api)
        self.assertEqual([call[0] for call in api.calls], ["GET"])
        api = FakeApi([{"jobId": 7, "url": URL}])
        sync_cronjob("key", "", "owner", "repo", "main", SCHEDULE, False, request=api)
        self.assertFalse(api.calls[-1][2]["job"]["enabled"])

    def test_pause_for_github_schedule(self):
        api = FakeApi([{"jobId": 7, "url": URL}])
        pause_cronjob("key", "owner", "repo", request=api)
        self.assertEqual(api.calls[-1], ("PATCH", "/jobs/7", {"job": {"enabled": False}}))


class WorkflowTriggerTests(unittest.TestCase):
    def test_cronjob_workflow_has_no_github_schedule_but_offers_scheduled(self):
        from dedicated_web_ui import _workflow_text

        schedule = {**SCHEDULE, "enabled": True}
        text = _workflow_text(schedule, github_schedule=False)
        self.assertNotIn("schedule:\n", text)
        self.assertNotIn("UTC", text)  # no UTC cron lines or comment: cron-job.org uses local time
        self.assertNotIn("PZ_SCHEDULE_CRON", text)
        self.assertIn("          - scheduled\n", text)
        self.assertIn("scheduled run (cron-job.org)", text)
        self.assertIn("    - cron: '45 6 * * 5'", _workflow_text(schedule))

    def test_dispatched_scheduled_run_uses_window_checks(self):
        import os
        from datetime import datetime
        from unittest.mock import patch
        from zoneinfo import ZoneInfo

        import dedicated_workflow

        schedule = {**SCHEDULE, "enabled": True, "pine_stop_time": "01:15", "pine_start_time": "02:30",
                    "minimum_start_buffer_minutes": 5, "allowed_lateness_minutes": 30}
        config = {"schedule": schedule, "dedicated_settings": {"schedule": schedule}}

        def run_at(hour, minute):
            now = datetime(2026, 11, 20, hour, minute, tzinfo=ZoneInfo("America/Toronto"))

            class FakeDatetime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return now.astimezone(tz)

            with patch.object(dedicated_workflow, "datetime", FakeDatetime), \
                    patch.dict(os.environ, {"PZ_SCHEDULE_CRON": ""}):
                return dedicated_workflow._scheduled_window_is_open(config)[0]

        self.assertTrue(run_at(1, 46))   # cron-job.org fires on time
        self.assertFalse(run_at(9, 0))   # a stray trigger outside the window does nothing


if __name__ == "__main__":
    unittest.main()
