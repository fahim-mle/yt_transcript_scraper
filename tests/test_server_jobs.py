import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import server
from tests.test_pipeline_stages import _FakeDb, _raw_record, _write_dataset

VIDEO_ID = "dQw4w9WgXcQ"


class WebJobTests(unittest.TestCase):
    def setUp(self):
        self._reset()

    def tearDown(self):
        running = [j.label for j in server._jobs.values() if j.status == "running"]
        self._reset()
        self.assertEqual(running, [])

    @staticmethod
    def _reset():
        with server._jobs_lock:
            server._jobs.clear()
            server._active_jobs.clear()
        with server._tjm_lock:
            server._thread_job_map.clear()

    @staticmethod
    def _await_status(job_id, timeout=2):
        job = server._jobs[job_id]
        deadline = time.monotonic() + timeout
        while job.status == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        return job.status

    @staticmethod
    def _stream_text(job_id, last_event_id=None):
        response = server.stream_job(job_id, last_event_id=last_event_id)

        async def collect():
            chunks = []
            async for chunk in response.body_iterator:
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8")
                chunks.append(chunk)
            return "".join(chunks)

        return asyncio.run(collect())

    @staticmethod
    def _post_json(path, payload):
        request_body = json.dumps(payload).encode("utf-8")
        messages = []
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {
                    "type": "http.request",
                    "body": request_body,
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 123),
            "server": ("testserver", 80),
        }
        asyncio.run(server.app(scope, receive, send))
        status = next(
            message["status"] for message in messages
            if message["type"] == "http.response.start"
        )
        body = b"".join(
            message.get("body", b"") for message in messages
            if message["type"] == "http.response.body"
        )
        return status, json.loads(body)


    def test_launch_rejects_a_second_job_of_the_same_class(self):
        started = threading.Event()
        release = threading.Event()

        def block_until_released():
            started.set()
            release.wait()

        first_job_id = server._launch("first", block_until_released)
        try:
            self.assertIsNotNone(first_job_id)
            self.assertTrue(started.wait(timeout=2))
            self.assertIsNone(server._launch("second", lambda: None))
        finally:
            release.set()

        self.assertEqual(self._await_status(first_job_id), "done")

    def test_a_manual_add_is_admitted_while_the_pipeline_is_running(self):
        started = threading.Event()
        release = threading.Event()
        added = threading.Event()

        def block_until_released():
            started.set()
            release.wait()

        heavy_job_id = server._launch("run-all", block_until_released)
        try:
            self.assertTrue(started.wait(timeout=2))

            with patch.object(server.pipeline, "add_manual",
                              side_effect=lambda *a, **k: added.set()) as add_manual:
                result = server.api_manual(server.ManualBody(
                    url=VIDEO_ID,
                    transcript="Pasted while the pipeline grinds away.",
                    fetch_metadata=False,
                ))
                light_job_id = result["job_id"]
                self.assertEqual(self._await_status(light_job_id), "done")

            self.assertTrue(added.is_set())
            self.assertEqual(add_manual.call_count, 1)
            self.assertNotEqual(light_job_id, heavy_job_id)
            # The heavy job is untouched by the light one.
            self.assertEqual(server._jobs[heavy_job_id].status, "running")

            # ...but a second manual add still waits for the first.
            second = server._launch("manual", lambda: None, klass=server.LIGHT)
            self.assertIsNotNone(second)
            self.assertEqual(self._await_status(second), "done")
        finally:
            release.set()

        self.assertEqual(self._await_status(heavy_job_id), "done")

    def test_job_stream_exposes_success_and_error_terminal_statuses(self):
        def fail():
            raise RuntimeError("deliberate failure")

        for name, operation, expected_status in [
            ("success", lambda: None, "done"),
            ("error", fail, "error"),
        ]:
            with self.subTest(name=name):
                job_id = server._launch(name, operation)
                stream = self._stream_text(job_id)
                self.assertTrue(
                    stream.endswith(f"event: done\ndata: {expected_status}\n\n"),
                    stream,
                )

    def test_api_maps_a_concurrent_heavy_job_to_conflict(self):
        started = threading.Event()
        release = threading.Event()

        def block_until_released():
            started.set()
            release.wait()

        first_job_id = server._launch("run-all", block_until_released)
        try:
            self.assertTrue(started.wait(timeout=2))
            response = server.api_clean()
            self.assertEqual(response.status_code, 409)
            message = json.loads(response.body)["error"]
            self.assertIn("run-all", message)
            self.assertIn("manually", message)
        finally:
            release.set()

        self.assertEqual(self._await_status(first_job_id), "done")

    def test_a_reconnected_stream_replays_only_the_lines_it_missed(self):
        job = server._Job("reattach", "manual", server.LIGHT)
        server._jobs[job.id] = job
        job.log("first line")
        job.log("second line")
        job.finish("done")

        # A page reloaded after the job ended still gets the log and the verdict.
        fresh = self._stream_text(job.id)
        self.assertIn("id: 1\ndata: first line\n\n", fresh)
        self.assertIn("id: 2\ndata: second line\n\n", fresh)
        self.assertTrue(fresh.endswith("event: done\ndata: done\n\n"), fresh)

        # A dropped connection resumes from Last-Event-ID without duplicating.
        resumed = self._stream_text(job.id, last_event_id="1")
        self.assertNotIn("first line", resumed)
        self.assertIn("id: 2\ndata: second line\n\n", resumed)
        self.assertTrue(resumed.endswith("event: done\ndata: done\n\n"), resumed)

    def test_a_late_subscriber_gets_the_backlog_then_follows_live(self):
        job = server._Job("late", "run-all", server.HEAVY)
        server._jobs[job.id] = job
        job.log("written before anyone attached")

        stream_queue, backlog, status = job.subscribe(0)
        self.assertEqual([line for _, line in backlog], ["written before anyone attached"])
        self.assertEqual(status, "running")

        job.log("written after")
        self.assertEqual(stream_queue.get(timeout=1), (2, "written after"))
        job.finish("done")
        self.assertEqual(stream_queue.get(timeout=1), ("done", "done"))

    def test_status_reports_running_jobs_for_the_ui_to_reconcile(self):
        started = threading.Event()
        release = threading.Event()

        def block_until_released():
            started.set()
            release.wait()

        job_id = server._launch("run-all", block_until_released)
        try:
            self.assertTrue(started.wait(timeout=2))
            jobs = server.api_status()["jobs"]
            self.assertEqual(
                [(job["id"], job["label"], job["class"]) for job in jobs],
                [(job_id, "run-all", server.HEAVY)],
            )
        finally:
            release.set()

        self.assertEqual(self._await_status(job_id), "done")
        self.assertEqual(server.api_status()["jobs"], [])

    def test_enrichment_limit_rejects_negative_fractional_and_text_values(self):
        for value in [-1, 1.5, "many"]:
            with self.subTest(value=value):
                status, body = self._post_json("/api/enrich", {"limit": value})
                self.assertEqual(status, 422)
                self.assertEqual(body["detail"][0]["loc"], ["body", "limit"])

    def test_manual_background_failure_is_reported_as_an_error_sse_event(self):
        body = server.ManualBody(
            url=VIDEO_ID,
            transcript="Keep this pasted transcript when the job fails.",
            fetch_metadata=False,
        )
        with patch.object(
            server.pipeline,
            "add_manual",
            side_effect=RuntimeError("staging failed"),
        ):
            result = server.api_manual(body)
            stream = self._stream_text(result["job_id"])

        self.assertEqual(result["video_id"], VIDEO_ID)
        self.assertTrue(stream.endswith("event: done\ndata: error\n\n"), stream)

    def test_run_all_stops_before_ingest_when_enrichment_fails(self):
        with (
            patch.object(server, "_low_priority"),
            patch.object(server.pipeline, "cmd_clean", return_value=True) as clean,
            patch.object(server.pipeline, "cmd_rewrite", return_value=True),
            patch.object(server.pipeline, "cmd_enrich", return_value=False) as enrich,
            patch.object(server.pipeline, "cmd_ingest") as ingest,
        ):
            result = server.api_run_all(server.RunAllBody(url=""))
            stream = self._stream_text(result["job_id"])

        self.assertEqual(clean.call_count, 1)
        self.assertEqual(enrich.call_count, 1)
        ingest.assert_not_called()
        self.assertTrue(stream.endswith("event: done\ndata: error\n\n"), stream)

    def test_run_all_stops_at_the_first_stage_that_does_not_report_success(self):
        watch_url = f"https://www.youtube.com/watch?v={VIDEO_ID}"
        cases = [
            ("scrape fails", watch_url, "cmd_scrape", False,
             {"cmd_scrape": 1, "cmd_clean": 0, "cmd_rewrite": 0, "cmd_enrich": 0,
              "cmd_ingest": 0}),
            ("clean fails", "", "cmd_clean", False,
             {"cmd_scrape": 0, "cmd_clean": 1, "cmd_rewrite": 0, "cmd_enrich": 0,
              "cmd_ingest": 0}),
            ("clean returns no verdict", "", "cmd_clean", None,
             {"cmd_scrape": 0, "cmd_clean": 1, "cmd_rewrite": 0, "cmd_enrich": 0,
              "cmd_ingest": 0}),
            ("rewrite fails", "", "cmd_rewrite", False,
             {"cmd_scrape": 0, "cmd_clean": 1, "cmd_rewrite": 1, "cmd_enrich": 0,
              "cmd_ingest": 0}),
        ]

        for name, url, failing, outcome, expected_calls in cases:
            with self.subTest(name=name):
                with (
                    patch.object(server, "_low_priority"),
                    patch.object(server.config, "REWRITE_ENABLED", True),
                    patch.object(server.pipeline, "cmd_scrape", return_value=True) as scrape,
                    patch.object(server.pipeline, "cmd_clean", return_value=True) as clean,
                    patch.object(server.pipeline, "cmd_rewrite", return_value=True) as rewrite,
                    patch.object(server.pipeline, "cmd_enrich", return_value=True) as enrich,
                    patch.object(server.pipeline, "cmd_ingest", return_value=True) as ingest,
                ):
                    stages = {
                        "cmd_scrape": scrape, "cmd_clean": clean, "cmd_rewrite": rewrite,
                        "cmd_enrich": enrich, "cmd_ingest": ingest,
                    }
                    stages[failing].return_value = outcome
                    result = server.api_run_all(server.RunAllBody(url=url))
                    stream = self._stream_text(result["job_id"])

                    for stage, expected in expected_calls.items():
                        self.assertEqual(stages[stage].call_count, expected, stage)
                self.assertTrue(stream.endswith("event: done\ndata: error\n\n"), stream)

    def test_run_all_runs_the_stages_in_pipeline_order_when_each_succeeds(self):
        order = []

        def record(stage):
            def stage_fn(_ns):
                order.append(stage)
                return True
            return stage_fn

        with (
            patch.object(server, "_low_priority"),
            patch.object(server.config, "REWRITE_ENABLED", True),
            patch.object(server.config, "LLM_ENABLED", True),
            patch.object(server.pipeline, "cmd_scrape", side_effect=record("scrape")),
            patch.object(server.pipeline, "cmd_clean", side_effect=record("clean")),
            patch.object(server.pipeline, "cmd_rewrite", side_effect=record("rewrite")),
            patch.object(server.pipeline, "cmd_enrich", side_effect=record("enrich")),
            patch.object(server.pipeline, "cmd_ingest", side_effect=record("ingest")),
        ):
            result = server.api_run_all(
                server.RunAllBody(url=f"https://www.youtube.com/watch?v={VIDEO_ID}")
            )
            stream = self._stream_text(result["job_id"])

        # rewrite must precede enrich: enrich derives metadata from the article.
        self.assertEqual(order, ["scrape", "clean", "rewrite", "enrich", "ingest"])
        self.assertTrue(stream.endswith("event: done\ndata: done\n\n"), stream)

    def test_run_all_skips_the_rewrite_when_it_is_disabled(self):
        order = []

        def record(stage):
            def stage_fn(_ns):
                order.append(stage)
                return True
            return stage_fn

        with (
            patch.object(server, "_low_priority"),
            patch.object(server.config, "REWRITE_ENABLED", False),
            patch.object(server.config, "LLM_ENABLED", True),
            patch.object(server.pipeline, "cmd_clean", side_effect=record("clean")),
            patch.object(server.pipeline, "cmd_rewrite", side_effect=record("rewrite")),
            patch.object(server.pipeline, "cmd_enrich", side_effect=record("enrich")),
            patch.object(server.pipeline, "cmd_ingest", side_effect=record("ingest")),
        ):
            result = server.api_run_all(server.RunAllBody(url=""))
            stream = self._stream_text(result["job_id"])

        self.assertEqual(order, ["clean", "enrich", "ingest"])
        self.assertTrue(stream.endswith("event: done\ndata: done\n\n"), stream)

    def test_run_all_still_ingests_when_individual_videos_fail_enrichment(self):
        """
        The regression that made every run-all report failure: one video the
        model choked on turned the whole stage into a failure, so ingest never
        ran and the UI toasted an error over a healthy pipeline.
        """
        fake_db = _FakeDb(
            {VIDEO_ID: {"status": "cleaned", "enrichment_status": "pending"}},
            pending=[VIDEO_ID],
        )
        failed_result = server.pipeline.llm_processor.Result(
            None, 600_000, "ollama_unavailable_or_invalid_json",
        )
        with (
            tempfile.TemporaryDirectory() as raw_dir,
            tempfile.TemporaryDirectory() as blob_dir,
            tempfile.TemporaryDirectory() as clean_dir,
            patch.object(server, "_low_priority"),
            patch.object(server.config, "LOCAL_OUTPUT_DIR", raw_dir),
            patch.object(server.config, "BLOB_OUTPUT_DIR", blob_dir),
            patch.object(server.config, "CLEAN_OUTPUT_DIR", clean_dir),
            patch.object(server.config, "LLM_ENABLED", True),
            # This test is about enrichment failure alone; the rewrite stage is
            # exercised on its own elsewhere and would otherwise call a model.
            patch.object(server.config, "REWRITE_ENABLED", False),
            patch.object(server.pipeline, "_db_available", return_value=True),
            patch.object(server.pipeline, "db", fake_db),
            patch.object(server.pipeline.llm_processor, "is_available", return_value=True),
            patch.object(server.pipeline.llm_processor, "enrich", return_value=failed_result),
        ):
            _write_dataset(raw_dir, _raw_record())

            result = server.api_run_all(server.RunAllBody(url=""))
            stream = self._stream_text(result["job_id"])
            ingested = os.path.exists(
                os.path.join(clean_dir, "Test_Channel", "Test_Video.md")
            )

        self.assertTrue(stream.endswith("event: done\ndata: done\n\n"), stream)
        self.assertTrue(ingested, "clean document never reached /srv/dbdata")
        # ...and the video the model choked on is still queued for a retry.
        self.assertIn((VIDEO_ID, "failed"), fake_db.enrichment_status)

    def test_stage_endpoints_report_an_unsuccessful_stage_as_an_error_job(self):
        watch_url = f"https://www.youtube.com/watch?v={VIDEO_ID}"
        endpoints = [
            ("scrape", "cmd_scrape", lambda: server.api_scrape(server.ScrapeBody(url=watch_url))),
            ("clean", "cmd_clean", server.api_clean),
            ("enrich", "cmd_enrich", lambda: server.api_enrich(server.EnrichBody(limit=0))),
            ("rewrite", "cmd_rewrite", lambda: server.api_rewrite(server.RewriteBody(limit=0))),
            ("ingest", "cmd_ingest", server.api_ingest),
        ]

        for endpoint, attr, call in endpoints:
            for outcome, expected_status in [(False, "error"), (True, "done")]:
                with self.subTest(endpoint=endpoint, outcome=outcome):
                    with (
                        patch.object(server, "_low_priority"),
                        patch.object(server.pipeline, attr, return_value=outcome),
                    ):
                        result = call()
                        stream = self._stream_text(result["job_id"])

                    self.assertTrue(
                        stream.endswith(f"event: done\ndata: {expected_status}\n\n"),
                        stream,
                    )


if __name__ == "__main__":
    unittest.main()
