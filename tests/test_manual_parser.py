import unittest

from scraper.manual import parse_transcript


class ManualTranscriptParserTests(unittest.TestCase):
    def test_inline_and_panel_timestamps_preserve_text_and_timing(self):
        cases = [
            (
                "inline",
                "00:01 First line\n00:04 Second line",
                [
                    {"text": "First line", "start": 1.0, "duration": 3.0},
                    {"text": "Second line", "start": 4.0, "duration": 3.0},
                ],
            ),
            (
                "panel",
                "(00:01)\nFirst panel line\n[00:04]\nSecond panel line",
                [
                    {"text": "First panel line", "start": 1.0, "duration": 3.0},
                    {"text": "Second panel line", "start": 4.0, "duration": 3.0},
                ],
            ),
        ]

        for name, source, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(parse_transcript(source), expected)

    def test_srt_and_vtt_cues_keep_numeric_caption_text(self):
        cases = [
            (
                "srt",
                "1\n00:00:01,000 --> 00:00:02,500\n123\n\n2\n"
                "00:00:03,000 --> 00:00:04,250\nSecond cue\n",
                [
                    {"text": "123", "start": 1.0, "duration": 1.5},
                    {"text": "Second cue", "start": 3.0, "duration": 1.25},
                ],
            ),
            (
                "vtt",
                "WEBVTT\n\n00:01.000 --> 00:02.500\n123\n\n"
                "00:03.000 --> 00:04.250\nSecond cue\n",
                [
                    {"text": "123", "start": 1.0, "duration": 1.5},
                    {"text": "Second cue", "start": 3.0, "duration": 1.25},
                ],
            ),
        ]

        for name, source, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(parse_transcript(source), expected)

    def test_prose_preserves_paragraph_order_and_a_timing_gap(self):
        segments = parse_transcript("One two. Three four.\n\nFive six.")

        self.assertEqual([segment["text"] for segment in segments], [
            "One two. Three four.",
            "Five six.",
        ])
        first_end = segments[0]["start"] + segments[0]["duration"]
        self.assertGreater(segments[1]["start"], first_end)

    def test_empty_and_header_only_input_is_rejected(self):
        for name, source in [
            ("empty", ""),
            ("whitespace", " \n\t"),
            ("vtt header only", "WEBVTT\n\n"),
        ]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    parse_transcript(source)


if __name__ == "__main__":
    unittest.main()
