import os
import tempfile
import unittest
from unittest.mock import patch

import main as pipeline


VIDEO_ID = "dQw4w9WgXcQ"


class ManualIngestionHelperTests(unittest.TestCase):
    def test_video_id_accepts_supported_youtube_source_shapes(self):
        sources = [
            VIDEO_ID,
            f"https://youtu.be/{VIDEO_ID}?t=12",
            f"https://www.youtube.com/watch?v={VIDEO_ID}&feature=share",
            f"youtube.com/shorts/{VIDEO_ID}",
            f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}",
            f"https://music.youtube.com/live/{VIDEO_ID}",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(pipeline._video_id_from(source), VIDEO_ID)

    def test_video_id_rejects_malformed_and_deceptive_sources(self):
        sources = [
            "",
            "too-short",
            f"https://example.com/watch?v={VIDEO_ID}",
            f"https://www.youtube.com.evil.example/watch?v={VIDEO_ID}",
            f"https://youtube.com/channel/{VIDEO_ID}",
            "https://youtu.be/not-valid!",
        ]

        for source in sources:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    pipeline._video_id_from(source)

    def test_invalid_published_dates_fail_before_creating_outputs(self):
        for published in ["02/29/2024", "2023-02-29"]:
            with self.subTest(published=published), tempfile.TemporaryDirectory() as output_dir:
                with self.assertRaises(ValueError), patch.object(
                    pipeline, "_db_available", return_value=False
                ):
                    pipeline.add_manual(
                        VIDEO_ID,
                        "A usable transcript.",
                        title="A title",
                        channel="A channel",
                        published=published,
                        output_dir=output_dir,
                        save_json=False,
                        fetch_metadata=False,
                    )

                self.assertEqual(os.listdir(output_dir), [])

    def test_manual_output_paths_cannot_escape_the_requested_directory(self):
        with tempfile.TemporaryDirectory() as output_dir, patch.object(
            pipeline, "_db_available", return_value=False
        ):
            record = pipeline.add_manual(
                VIDEO_ID,
                "A safely staged transcript.",
                title="../../outside/title",
                channel="../../outside/channel",
                published="2024-02-29",
                output_dir=output_dir,
                save_json=True,
                fetch_metadata=False,
            )

            output_root = os.path.realpath(output_dir)
            markdown_path = os.path.realpath(record["md_path"])
            self.assertEqual(os.path.commonpath([output_root, markdown_path]), output_root)
            self.assertTrue(os.path.isfile(markdown_path))
            self.assertTrue(os.path.isfile(os.path.join(output_dir, "dataset.jsonl")))
            self.assertTrue(os.path.isfile(os.path.join(output_dir, "index.csv")))

    def test_duplicate_video_id_is_rejected_without_appending_another_record(self):
        with tempfile.TemporaryDirectory() as output_dir, patch.object(
            pipeline, "_db_available", return_value=False
        ):
            pipeline.add_manual(
                VIDEO_ID,
                "The original transcript.",
                title="Original",
                channel="Channel",
                output_dir=output_dir,
                save_json=False,
                fetch_metadata=False,
            )
            dataset_path = os.path.join(output_dir, "dataset.jsonl")
            with open(dataset_path, encoding="utf-8") as dataset:
                before = dataset.read()

            with self.assertRaises(FileExistsError):
                pipeline.add_manual(
                    f"https://youtu.be/{VIDEO_ID}",
                    "A replacement transcript that must not be staged.",
                    title="Replacement",
                    channel="Channel",
                    output_dir=output_dir,
                    save_json=False,
                    fetch_metadata=False,
                )

            with open(dataset_path, encoding="utf-8") as dataset:
                self.assertEqual(dataset.read(), before)


if __name__ == "__main__":
    unittest.main()
