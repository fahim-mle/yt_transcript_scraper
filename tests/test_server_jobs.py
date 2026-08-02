import asyncio
import json
import threading
import unittest
from unittest.mock import patch


import server


VIDEO_ID = "dQw4w9WgXcQ"


class WebJobTests(unittest.TestCase):
    def setUp(self):
        with server._active_lock:
            server._active_job = None
        server._job_queues.clear()
        with server._tqm_lock:
            server._thread_queue_map.clear()

    def tearDown(self):
        with server._active_lock:
            active = server._active_job
        self.assertFalse(active is not None and active["status"] == "running")
        server._job_queues.clear()
        with server._tqm_lock:
            server._thread_queue_map.clear()

    @staticmethod
    def _terminal_from(job_id):
        job_queue = server._job_queues[job_id]
        while True:
            message = job_queue.get(timeout=2)
            if isinstance(message, tuple) and message[0] == "done":
                return message

    @staticmethod
    def _stream_text(job_id):
        response = server.stream_job(job_id)

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


    def test_launch_rejects_a_second_job_while_the_first_is_running(self):
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

        self.assertEqual(self._terminal_from(first_job_id), ("done", "done"))

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

    def test_api_maps_a_concurrent_job_to_conflict(self):
        started = threading.Event()
        release = threading.Event()

        def block_until_released():
            started.set()
            release.wait()

        first_job_id = server._launch("first", block_until_released)
        try:
            self.assertTrue(started.wait(timeout=2))
            response = server.api_clean()
            self.assertEqual(response.status_code, 409)
            self.assertEqual(json.loads(response.body), {
                "error": "A job is already running",
            })
        finally:
            release.set()

        self.assertEqual(self._terminal_from(first_job_id), ("done", "done"))

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
             {"cmd_scrape": 1, "cmd_clean": 0, "cmd_enrich": 0, "cmd_ingest": 0}),
            ("clean fails", "", "cmd_clean", False,
             {"cmd_scrape": 0, "cmd_clean": 1, "cmd_enrich": 0, "cmd_ingest": 0}),
            ("clean returns no verdict", "", "cmd_clean", None,
             {"cmd_scrape": 0, "cmd_clean": 1, "cmd_enrich": 0, "cmd_ingest": 0}),
        ]

        for name, url, failing, outcome, expected_calls in cases:
            with self.subTest(name=name):
                with (
                    patch.object(server, "_low_priority"),
                    patch.object(server.pipeline, "cmd_scrape", return_value=True) as scrape,
                    patch.object(server.pipeline, "cmd_clean", return_value=True) as clean,
                    patch.object(server.pipeline, "cmd_enrich", return_value=True) as enrich,
                    patch.object(server.pipeline, "cmd_ingest", return_value=True) as ingest,
                ):
                    stages = {
                        "cmd_scrape": scrape, "cmd_clean": clean,
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
            patch.object(server.pipeline, "cmd_scrape", side_effect=record("scrape")),
            patch.object(server.pipeline, "cmd_clean", side_effect=record("clean")),
            patch.object(server.pipeline, "cmd_enrich", side_effect=record("enrich")),
            patch.object(server.pipeline, "cmd_ingest", side_effect=record("ingest")),
        ):
            result = server.api_run_all(
                server.RunAllBody(url=f"https://www.youtube.com/watch?v={VIDEO_ID}")
            )
            stream = self._stream_text(result["job_id"])

        self.assertEqual(order, ["scrape", "clean", "enrich", "ingest"])
        self.assertTrue(stream.endswith("event: done\ndata: done\n\n"), stream)

    def test_stage_endpoints_report_an_unsuccessful_stage_as_an_error_job(self):
        watch_url = f"https://www.youtube.com/watch?v={VIDEO_ID}"
        endpoints = [
            ("scrape", "cmd_scrape", lambda: server.api_scrape(server.ScrapeBody(url=watch_url))),
            ("clean", "cmd_clean", server.api_clean),
            ("enrich", "cmd_enrich", lambda: server.api_enrich(server.EnrichBody(limit=0))),
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
