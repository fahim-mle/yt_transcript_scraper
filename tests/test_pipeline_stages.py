import argparse
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import main as pipeline
from scraper import llm_processor


VIDEO_ID = "dQw4w9WgXcQ"
OTHER_VIDEO_ID = "9bZkp7q19f0"
WATCH_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


class _FakeDb:
    """In-memory stand-in for database.db that records writes instead of issuing them."""

    def __init__(self, rows=None, read_error=None, write_error=None, pending=()):
        self.rows = {video_id: dict(row) for video_id, row in (rows or {}).items()}
        self.read_error = read_error
        self.write_error = write_error
        self.pending = list(pending)
        self.updates = []
        self.enrichment_status = []
        self.sections = []
        self.runs = []

    def get_video(self, video_id):
        if self.read_error is not None:
            raise self.read_error
        return self.rows.get(video_id)

    def upsert_video(self, row):
        self.rows[row["video_id"]] = dict(row)

    def update_video(self, video_id, updates):
        if self.write_error is not None:
            raise self.write_error
        self.updates.append((video_id, dict(updates)))
        self.rows.setdefault(video_id, {}).update(updates)

    def set_enrichment_status(self, video_id, status):
        self.enrichment_status.append((video_id, status))
        self.rows.setdefault(video_id, {})["enrichment_status"] = status

    def list_pending_enrichment(self, limit=None):
        return self.pending[:limit] if limit else list(self.pending)

    def record_processing_run(self, video_id, model, status, error, duration_ms):
        self.runs.append((video_id, model, status))

    def upsert_sections(self, video_id, sections):
        self.sections.append((video_id, sections))


def _document(video_id, body):
    return (
        "---\n"
        'title: "Test Video"\n'
        f'url: "https://www.youtube.com/watch?v={video_id}"\n'
        "---\n\n"
        f"{body}\n"
    )


BLOB_DOCUMENT = _document(VIDEO_ID, "Blob body awaiting ingest.")
REVIEWED_COPY = _document(VIDEO_ID, "Reviewed destination copy that must survive ingest.")
FOREIGN_DOCUMENT = _document(OTHER_VIDEO_ID, "A different video entirely.")
UNIDENTIFIED_DOCUMENT = "---\ntitle: \"No url here\"\n---\n\nNothing to key on.\n"


def _raw_record():
    """A record whose cleaned prose clears config.MIN_WORD_COUNT."""
    return {
        "video_id": VIDEO_ID,
        "url": WATCH_URL,
        "title": "Test Video",
        "channel": "Test Channel",
        "published": "2024-01-01",
        "description": "",
        "transcript_segments": [
            {
                "text": "alpha beta gamma delta epsilon zeta eta theta iota kappa",
                "start": i * 4.0,
                "duration": 4.0,
            }
            for i in range(30)
        ],
    }


def _write_dataset(raw_dir, record):
    with open(os.path.join(raw_dir, "dataset.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _write_blob(blob_dir, content):
    channel_dir = os.path.join(blob_dir, "Test_Channel")
    os.makedirs(channel_dir, exist_ok=True)
    path = os.path.join(channel_dir, "Test_Video.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class CleanStageOutcomeTests(unittest.TestCase):
    @staticmethod
    def _args(raw_dir, blob_dir):
        return argparse.Namespace(raw_dir=raw_dir, blob_dir=blob_dir)

    def test_clean_without_a_dataset_reports_failure_and_writes_no_blob(self):
        with tempfile.TemporaryDirectory() as raw_dir, tempfile.TemporaryDirectory() as blob_dir:
            result = pipeline.cmd_clean(self._args(raw_dir, blob_dir))

            self.assertIs(result, False)
            self.assertEqual(os.listdir(blob_dir), [])

    def test_clean_reports_success_and_writes_the_cleaned_document(self):
        with (
            tempfile.TemporaryDirectory() as raw_dir,
            tempfile.TemporaryDirectory() as blob_dir,
            patch.object(pipeline, "_db_available", return_value=False),
        ):
            _write_dataset(raw_dir, _raw_record())

            result = pipeline.cmd_clean(self._args(raw_dir, blob_dir))

            self.assertIs(result, True)
            with open(os.path.join(blob_dir, "Test_Channel", "Test_Video.md"), encoding="utf-8") as f:
                written = f.read()
            self.assertIn(WATCH_URL, written)
            self.assertIn("alpha beta gamma delta", written)

    def test_clean_reports_failure_when_the_database_sync_fails(self):
        fake_db = _FakeDb(read_error=RuntimeError("connection refused"))
        with (
            tempfile.TemporaryDirectory() as raw_dir,
            tempfile.TemporaryDirectory() as blob_dir,
            patch.object(pipeline, "_db_available", return_value=True),
            patch.object(pipeline, "db", fake_db),
        ):
            _write_dataset(raw_dir, _raw_record())

            result = pipeline.cmd_clean(self._args(raw_dir, blob_dir))

            self.assertIs(result, False)
            # The blob is still written — only the database half of the stage failed.
            self.assertTrue(
                os.path.isfile(os.path.join(blob_dir, "Test_Channel", "Test_Video.md"))
            )

    def test_rebuilding_a_missing_blob_requeues_enrichment_that_was_done(self):
        fake_db = _FakeDb({VIDEO_ID: {"status": "ingested", "enrichment_status": "done"}})
        with (
            tempfile.TemporaryDirectory() as raw_dir,
            tempfile.TemporaryDirectory() as blob_dir,
            patch.object(pipeline, "_db_available", return_value=True),
            patch.object(pipeline, "db", fake_db),
        ):
            _write_dataset(raw_dir, _raw_record())

            result = pipeline.cmd_clean(self._args(raw_dir, blob_dir))

            blob_path = os.path.join(blob_dir, "Test_Channel", "Test_Video.md")
            self.assertIs(result, True)
            with open(blob_path, encoding="utf-8") as f:
                rebuilt = f.read()
            # The rebuilt document is plain clean Markdown again, so the video has
            # to go back through the enrichment worker instead of staying 'done'.
            self.assertNotIn("## Summary", rebuilt)
            self.assertEqual(fake_db.enrichment_status, [(VIDEO_ID, "pending")])
            self.assertEqual(fake_db.updates, [])

    def test_existing_blob_keeps_its_content_row_status_and_queue_state(self):
        cases = [
            ("enriched and done", "done", []),
            ("waiting in the queue", "pending", []),
            ("failed and retryable", "failed", []),
            ("never queued", None, [(VIDEO_ID, "pending")]),
        ]

        for name, enrichment_status, expected_status_writes in cases:
            with self.subTest(name=name):
                row = {"status": "ingested"}
                if enrichment_status is not None:
                    row["enrichment_status"] = enrichment_status
                fake_db = _FakeDb({VIDEO_ID: row})
                existing = _document(VIDEO_ID, "## Summary\n\nAlready enriched by the worker.")

                with (
                    tempfile.TemporaryDirectory() as raw_dir,
                    tempfile.TemporaryDirectory() as blob_dir,
                    patch.object(pipeline, "_db_available", return_value=True),
                    patch.object(pipeline, "db", fake_db),
                ):
                    _write_dataset(raw_dir, _raw_record())
                    blob_path = _write_blob(blob_dir, existing)

                    result = pipeline.cmd_clean(self._args(raw_dir, blob_dir))

                    with open(blob_path, encoding="utf-8") as f:
                        self.assertEqual(f.read(), existing)

                self.assertIs(result, True)
                self.assertEqual(fake_db.enrichment_status, expected_status_writes)
                # An already ingested row is never downgraded back to 'cleaned'.
                self.assertEqual(fake_db.updates, [])

    def test_blob_belonging_to_another_video_is_rejected_and_left_untouched(self):
        cases = [
            ("another video's document", FOREIGN_DOCUMENT),
            ("document without a video id", UNIDENTIFIED_DOCUMENT),
        ]

        for name, existing in cases:
            with self.subTest(name=name):
                fake_db = _FakeDb({VIDEO_ID: {"status": "cleaned", "enrichment_status": "done"}})
                with (
                    tempfile.TemporaryDirectory() as raw_dir,
                    tempfile.TemporaryDirectory() as blob_dir,
                    patch.object(pipeline, "_db_available", return_value=True),
                    patch.object(pipeline, "db", fake_db),
                ):
                    _write_dataset(raw_dir, _raw_record())
                    blob_path = _write_blob(blob_dir, existing)

                    result = pipeline.cmd_clean(self._args(raw_dir, blob_dir))

                    with open(blob_path, encoding="utf-8") as f:
                        self.assertEqual(f.read(), existing)

                self.assertIs(result, False)
                self.assertEqual(fake_db.updates, [])
                self.assertEqual(fake_db.enrichment_status, [])


class IngestStageOutcomeTests(unittest.TestCase):
    @staticmethod
    def _args(blob_dir, clean_dir):
        return argparse.Namespace(blob_dir=blob_dir, clean_dir=clean_dir)

    @staticmethod
    def _stage_destination(clean_dir, content):
        dst = os.path.join(clean_dir, "Test_Channel", "Test_Video.md")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
        return dst

    def test_ingest_without_a_blob_directory_reports_failure(self):
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as clean_dir:
            missing_blob_dir = os.path.join(parent, "never-created")

            result = pipeline.cmd_ingest(self._args(missing_blob_dir, clean_dir))

            self.assertIs(result, False)
            self.assertEqual(os.listdir(clean_dir), [])

    def test_new_blob_document_is_copied_and_marked_ingested(self):
        fake_db = _FakeDb({VIDEO_ID: {"status": "cleaned", "clean_path": None}})
        with (
            tempfile.TemporaryDirectory() as blob_dir,
            tempfile.TemporaryDirectory() as clean_dir,
            patch.object(pipeline, "_db_available", return_value=True),
            patch.object(pipeline, "db", fake_db),
        ):
            _write_blob(blob_dir, BLOB_DOCUMENT)
            dst = os.path.join(clean_dir, "Test_Channel", "Test_Video.md")

            result = pipeline.cmd_ingest(self._args(blob_dir, clean_dir))

            self.assertIs(result, True)
            with open(dst, encoding="utf-8") as f:
                self.assertEqual(f.read(), BLOB_DOCUMENT)
            self.assertEqual(
                fake_db.updates,
                [(VIDEO_ID, {"status": "ingested", "clean_path": dst})],
            )

    def test_blob_without_a_video_id_is_never_copied(self):
        fake_db = _FakeDb({VIDEO_ID: {"status": "cleaned"}})
        with (
            tempfile.TemporaryDirectory() as blob_dir,
            tempfile.TemporaryDirectory() as clean_dir,
            patch.object(pipeline, "_db_available", return_value=True),
            patch.object(pipeline, "db", fake_db),
        ):
            _write_blob(blob_dir, UNIDENTIFIED_DOCUMENT)

            result = pipeline.cmd_ingest(self._args(blob_dir, clean_dir))

            self.assertIs(result, False)
            self.assertEqual(os.listdir(clean_dir), [])
            self.assertEqual(fake_db.updates, [])

    def test_existing_destination_is_repaired_in_the_database_without_recopying(self):
        cases = [
            ("stale status", {"status": "cleaned"}, True, [{"status": "ingested"}]),
            ("clean path points elsewhere",
             {"status": "ingested", "clean_path": "/old/path.md"}, True, [{"clean_path": "<dst>"}]),
            ("already ingested at this path", {"status": "ingested"}, True, []),
            ("embedded rows keep their downstream status", {"status": "embedded"}, True, []),
            ("unusable status", {"status": "archived"}, False, []),
            ("no database row", None, False, []),
        ]

        for name, row, expected_result, expected_updates in cases:
            with self.subTest(name=name):
                with (
                    tempfile.TemporaryDirectory() as blob_dir,
                    tempfile.TemporaryDirectory() as clean_dir,
                ):
                    _write_blob(blob_dir, BLOB_DOCUMENT)
                    dst = self._stage_destination(clean_dir, REVIEWED_COPY)

                    rows = None if row is None else {VIDEO_ID: {"clean_path": dst, **row}}
                    fake_db = _FakeDb(rows)

                    with (
                        patch.object(pipeline, "_db_available", return_value=True),
                        patch.object(pipeline, "db", fake_db),
                    ):
                        result = pipeline.cmd_ingest(self._args(blob_dir, clean_dir))

                    self.assertIs(result, expected_result)
                    with open(dst, encoding="utf-8") as f:
                        self.assertEqual(f.read(), REVIEWED_COPY)
                    self.assertEqual(
                        [updates for _, updates in fake_db.updates],
                        [{k: dst if v == "<dst>" else v for k, v in expected.items()}
                         for expected in expected_updates],
                    )

    def test_destination_holding_a_different_video_is_rejected(self):
        cases = [
            ("another video's document", FOREIGN_DOCUMENT),
            ("document without a video id", UNIDENTIFIED_DOCUMENT),
        ]

        for name, destination in cases:
            with self.subTest(name=name):
                fake_db = _FakeDb({VIDEO_ID: {"status": "cleaned", "clean_path": None}})
                with (
                    tempfile.TemporaryDirectory() as blob_dir,
                    tempfile.TemporaryDirectory() as clean_dir,
                    patch.object(pipeline, "_db_available", return_value=True),
                    patch.object(pipeline, "db", fake_db),
                ):
                    _write_blob(blob_dir, BLOB_DOCUMENT)
                    dst = self._stage_destination(clean_dir, destination)

                    result = pipeline.cmd_ingest(self._args(blob_dir, clean_dir))

                    with open(dst, encoding="utf-8") as f:
                        self.assertEqual(f.read(), destination)

                self.assertIs(result, False)
                self.assertEqual(fake_db.updates, [])


def _enrichment_result():
    enrichment = llm_processor.Enrichment(
        summary="A concise overview of the test video.",
        key_concepts=["alpha", "beta", "gamma"],
        domains=["testing"],
        difficulty="beginner",
        content_kind="tutorial",
        sections=[llm_processor.Section(heading="Part one", summary="What it covers.")],
    )
    return llm_processor.Result(enrichment, duration_ms=12, error=None)


class EnrichStageOutcomeTests(unittest.TestCase):
    @staticmethod
    def _args(raw_dir, blob_dir):
        return argparse.Namespace(
            raw_dir=raw_dir, blob_dir=blob_dir, limit=0, loop=False, poll=60,
        )

    def test_successful_enrichment_writes_the_knowledge_doc_and_marks_it_done(self):
        fake_db = _FakeDb(
            {VIDEO_ID: {"status": "cleaned", "enrichment_status": "pending"}},
            pending=[VIDEO_ID],
        )
        with (
            tempfile.TemporaryDirectory() as raw_dir,
            tempfile.TemporaryDirectory() as blob_dir,
            patch.object(pipeline, "_db_available", return_value=True),
            patch.object(pipeline, "db", fake_db),
            patch.object(pipeline.config, "LLM_ENABLED", True),
            patch.object(pipeline.llm_processor, "is_available", return_value=True),
            patch.object(pipeline.llm_processor, "enrich", return_value=_enrichment_result()),
        ):
            _write_dataset(raw_dir, _raw_record())

            result = pipeline.cmd_enrich(self._args(raw_dir, blob_dir))

            with open(os.path.join(blob_dir, "Test_Channel", "Test_Video.md"), encoding="utf-8") as f:
                document = f.read()

        self.assertIs(result, True)
        self.assertIn("A concise overview of the test video.", document)
        self.assertEqual(fake_db.enrichment_status, [(VIDEO_ID, "done")])

    def test_persistence_failures_keep_the_video_queued_and_fail_the_stage(self):
        for name in ["database write fails", "knowledge document write fails"]:
            with self.subTest(name=name):
                database_fails = name.startswith("database")
                fake_db = _FakeDb(
                    {VIDEO_ID: {"status": "cleaned", "enrichment_status": "pending"}},
                    write_error=RuntimeError("write refused") if database_fails else None,
                    pending=[VIDEO_ID],
                )
                with (
                    tempfile.TemporaryDirectory() as raw_dir,
                    tempfile.TemporaryDirectory() as blob_dir,
                    patch.object(pipeline, "_db_available", return_value=True),
                    patch.object(pipeline, "db", fake_db),
                    patch.object(pipeline.config, "LLM_ENABLED", True),
                    patch.object(pipeline.llm_processor, "is_available", return_value=True),
                    patch.object(pipeline.llm_processor, "enrich", return_value=_enrichment_result()),
                ):
                    _write_dataset(raw_dir, _raw_record())
                    if not database_fails:
                        # A file where the channel directory belongs: the knowledge
                        # document cannot be written.
                        with open(os.path.join(blob_dir, "Test_Channel"), "w", encoding="utf-8") as f:
                            f.write("not a directory")

                    result = pipeline.cmd_enrich(self._args(raw_dir, blob_dir))

                self.assertIs(result, False)
                # Left in the retry queue rather than reported as enriched.
                self.assertEqual(fake_db.enrichment_status, [(VIDEO_ID, "failed")])


class CliExitCodeTests(unittest.TestCase):
    def test_command_result_decides_the_process_exit_code(self):
        cases = [
            ("clean failure", ["main.py", "clean"], "cmd_clean", False, 1),
            ("clean success", ["main.py", "clean"], "cmd_clean", True, None),
            (
                "manual failure",
                ["main.py", "manual", VIDEO_ID, "--transcript", "text"],
                "cmd_manual",
                False,
                1,
            ),
            ("command without a result", ["main.py", "setup-db"], "cmd_setup_db", None, None),
        ]

        for name, argv, attr, outcome, expected_code in cases:
            with self.subTest(name=name):
                with (
                    patch.object(sys, "argv", argv),
                    patch.object(pipeline, attr, return_value=outcome) as command,
                ):
                    if expected_code is None:
                        pipeline.main()
                    else:
                        with self.assertRaises(SystemExit) as raised:
                            pipeline.main()
                        self.assertEqual(raised.exception.code, expected_code)

                    self.assertEqual(command.call_count, 1)


if __name__ == "__main__":
    unittest.main()
