import json
import unittest
import urllib.error
from unittest.mock import patch

import config
from scraper import formatter, rewriter


def _para(word: str, n: int) -> str:
    return " ".join(f"{word}{i}" for i in range(n))


class ChunkingTests(unittest.TestCase):
    def test_paragraphs_are_never_split(self):
        blocks = rewriter._split_blocks("\n\n".join([_para("alpha", 50), _para("beta", 50)]))
        chunks = rewriter.chunk_blocks(blocks, target_words=10)
        for chunk in chunks:
            for para in chunk.paragraphs:
                self.assertIn(para.split()[0], {"alpha0", "beta0"},
                              "a paragraph was cut mid-way")

    def test_heading_starts_a_new_chunk(self):
        text = f"## One\n\n{_para('a', 5)}\n\n## Two\n\n{_para('b', 5)}"
        chunks = rewriter.chunk_blocks(rewriter._split_blocks(text), target_words=1000)
        self.assertEqual([c.heading for c in chunks], ["One", "Two"])

    def test_long_section_splits_but_keeps_its_heading(self):
        text = "## Long\n\n" + "\n\n".join(_para("w", 40) for _ in range(4))
        chunks = rewriter.chunk_blocks(rewriter._split_blocks(text), target_words=50)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.heading == "Long" for c in chunks))

    def test_prose_without_headings_still_chunks(self):
        text = "\n\n".join(_para("x", 30) for _ in range(5))
        chunks = rewriter.chunk_blocks(rewriter._split_blocks(text), target_words=45)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.heading is None for c in chunks))

    def test_empty_text_yields_no_chunks(self):
        self.assertEqual(rewriter.chunk_blocks(rewriter._split_blocks("   "), 100), [])

    def test_a_chunk_never_ends_mid_sentence(self):
        # The failure this guards against: "...I live in y city, I like to" |
        # "travel long distances." Chunks are built from whole paragraphs, so
        # every boundary lands on a sentence end.
        text = "\n\n".join(
            f"I am person{i}. I live in city{i}. I like to travel long distances."
            for i in range(8)
        )
        chunks = rewriter.chunk_blocks(rewriter._split_blocks(text), target_words=15)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.text.rstrip().endswith("."),
                            f"chunk ended mid-sentence: ...{chunk.text[-40:]!r}")

    def test_an_oversized_paragraph_is_kept_whole(self):
        # A single paragraph longer than the target is never sliced; the chunk
        # simply runs over budget.
        long_para = _para("word", 500)
        chunks = rewriter.chunk_blocks(rewriter._split_blocks(long_para), target_words=50)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].words, 500)


class CoverageTests(unittest.TestCase):
    def test_identical_text_is_full_coverage(self):
        src = "gradient descent tunes the activation function during backpropagation"
        ratio, retention = rewriter.coverage(src, src)
        self.assertAlmostEqual(ratio, 1.0)
        self.assertAlmostEqual(retention, 1.0)

    def test_summarising_is_detected_as_dropped_content(self):
        src = "gradient descent activation function backpropagation regularisation dropout"
        ratio, retention = rewriter.coverage(src, "gradient descent")
        self.assertLess(ratio, 0.5)
        self.assertLess(retention, 0.5)

    def test_reworded_prose_keeps_content_words(self):
        src = "so basically the gradient descent thing tunes the activation function"
        out = "Gradient descent tunes the activation function."
        _, retention = rewriter.coverage(src, out)
        self.assertAlmostEqual(retention, 1.0,
                               msg="stopword-only edits must not count as lost content")

    def test_failure_reasons(self):
        src = _para("concept", 100)
        self.assertIn("empty", rewriter._coverage_failure(src, "") or "")
        self.assertIn("too short", rewriter._coverage_failure(src, _para("concept", 10)) or "")
        self.assertIn("too long", rewriter._coverage_failure(src, _para("concept", 400)) or "")
        self.assertIsNone(rewriter._coverage_failure(src, src))

    def test_dropped_content_at_acceptable_length_still_fails(self):
        # Same word count, but the distinctive vocabulary was replaced — the
        # ratio check alone would wave this through.
        src = _para("concept", 100)
        self.assertIn("dropped content", rewriter._coverage_failure(src, _para("filler", 100)) or "")


class SystemPromptTests(unittest.TestCase):
    """
    Thinking models must be told to skip chain-of-thought. `think: false` alone
    is not honoured on every Ollama build, and on a whole-corpus rewrite the
    difference is between minutes and hours per video.
    """

    def test_qwen_models_get_the_no_think_switch(self):
        for model in ("qwen3:4b", "qwen3.5:9b", "Qwen3.5:9b"):
            with patch.object(config, "REWRITE_MODEL", model):
                self.assertTrue(rewriter._system_prompt().startswith("/no_think"), model)

    def test_other_models_do_not_get_a_stray_token(self):
        for model in ("gemma4:e4b", "llama3:8b"):
            with patch.object(config, "REWRITE_MODEL", model):
                self.assertNotIn("/no_think", rewriter._system_prompt(), model)

    def test_the_preserve_all_content_rule_survives_either_way(self):
        for model in ("qwen3:4b", "gemma4:e4b"):
            with patch.object(config, "REWRITE_MODEL", model):
                self.assertIn("PRESERVE ALL CONTENT", rewriter._system_prompt())


class RewriteChunkTests(unittest.TestCase):
    def _chunk(self, words=100):
        return rewriter.Chunk(None, [_para("topic", words)])

    def test_falls_back_to_verbatim_when_model_unreachable(self):
        chunk = self._chunk()
        with patch.object(rewriter, "_call", return_value=None):
            text, fallback = rewriter.rewrite_chunk(chunk, "T", "")
        self.assertTrue(fallback)
        self.assertEqual(text, chunk.text, "content must survive an unreachable model")

    def test_falls_back_when_every_attempt_fails_coverage(self):
        chunk = self._chunk()
        with patch.object(rewriter, "_call", return_value="too short"):
            text, fallback = rewriter.rewrite_chunk(chunk, "T", "")
        self.assertTrue(fallback)
        self.assertEqual(text, chunk.text)

    def test_retries_before_giving_up(self):
        chunk = self._chunk()
        with patch.object(rewriter, "_call", return_value="nope") as call:
            rewriter.rewrite_chunk(chunk, "T", "")
        self.assertEqual(call.call_count, config.REWRITE_MAX_ATTEMPTS)

    def test_accepts_a_faithful_rewrite(self):
        chunk = self._chunk()
        with patch.object(rewriter, "_call", return_value=chunk.text):
            text, fallback = rewriter.rewrite_chunk(chunk, "T", "")
        self.assertFalse(fallback)
        self.assertEqual(text, chunk.text)

    def test_second_attempt_is_cooler(self):
        temps = []
        chunk = self._chunk()

        def record(prompt, temperature, source_words):
            temps.append(temperature)
            return "short"

        with patch.object(rewriter, "_call", side_effect=record):
            rewriter.rewrite_chunk(chunk, "T", "")
        self.assertLess(temps[1], temps[0])

    def test_output_ceiling_bounds_a_runaway_model(self):
        # Must leave room for a legitimate rewrite at the top of the accepted
        # band, but still cut off a model that never stops.
        ceiling = rewriter._output_ceiling(900)
        legitimate = 900 * config.REWRITE_MAX_RATIO * 1.35  # words → tokens
        self.assertGreater(ceiling, legitimate)
        self.assertLess(ceiling, 900 * 4)

    def test_ceiling_is_passed_to_ollama(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured.update(json.loads(req.data.decode()))
            raise urllib.error.URLError("stop here")

        with patch.object(rewriter.urllib.request, "urlopen", fake_urlopen):
            rewriter._call("p", 0.3, 900)
        self.assertEqual(captured["options"]["num_predict"], rewriter._output_ceiling(900))


class RewriteDocumentTests(unittest.TestCase):
    """rewrite() reads the chunk size from config, so these force it small."""

    def setUp(self):
        patcher = patch.object(config, "REWRITE_CHUNK_WORDS", 50)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_multi_chunk_section_emits_its_heading_once(self):
        text = "## Long\n\n" + "\n\n".join(_para("w", 40) for _ in range(4))
        with patch.object(rewriter, "_call", return_value=None):
            result = rewriter.rewrite(text, "T")
        self.assertGreater(result.chunks, 1, "test needs a genuinely multi-chunk section")
        self.assertEqual(result.article.count("## Long"), 1)
        self.assertEqual(result.fallbacks, result.chunks)

    def test_no_prose_is_reported_not_crashed(self):
        result = rewriter.rewrite("   ", "T")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_prose_to_rewrite")

    def test_every_chunk_reaches_the_article(self):
        text = "\n\n".join(_para(f"seg{i}", 30) for i in range(5))
        with patch.object(rewriter, "_call", return_value=None):
            result = rewriter.rewrite(text, "T")
        self.assertGreater(result.chunks, 1, "test needs more than one chunk to be meaningful")
        for i in range(5):
            self.assertIn(f"seg{i}0", result.article, "a fallback chunk went missing")

    def test_context_tail_is_carried_between_chunks(self):
        text = "\n\n".join(_para(f"seg{i}", 30) for i in range(3))
        seen = []

        def record(prompt, temperature, source_words):
            seen.append(prompt)
            return None

        with patch.object(rewriter, "_call", side_effect=record):
            rewriter.rewrite(text, "T")
        # First chunk has no preceding article; later ones must quote its tail.
        self.assertNotIn("The article so far", seen[0])
        self.assertIn("The article so far", seen[-1])

    def test_fence_wrapped_output_is_unwrapped(self):
        self.assertEqual(rewriter._strip_fences("```markdown\nhello\n```"), "hello")
        self.assertEqual(rewriter._strip_fences("plain"), "plain")


class ArticleDocTests(unittest.TestCase):
    META = {"title": "T", "channel": "C", "published": "2026-01-01",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "description": ""}

    def test_body_round_trips(self):
        doc = formatter.to_article_doc(self.META, "## Section\n\nBody prose.")
        self.assertEqual(formatter.extract_article_body(doc), "## Section\n\nBody prose.")

    def test_enrichment_is_layered_above_the_article(self):
        doc = formatter.to_article_doc(
            self.META, "Body prose.",
            {"summary": "S", "key_concepts": ["a", "b"], "domains": ["d"],
             "difficulty": "beginner", "content_kind": "talk", "sections": []},
        )
        self.assertLess(doc.index("## Summary"), doc.index(formatter.ARTICLE_MARKER))
        self.assertIn('key_concepts: ["a", "b"]', doc)
        self.assertEqual(formatter.extract_article_body(doc), "Body prose.")

    def test_article_doc_carries_the_url_identity_field(self):
        # main._extract_video_id depends on this for overwrite protection.
        import main
        doc = formatter.to_article_doc(self.META, "Body.")
        self.assertEqual(main._extract_video_id(doc), "dQw4w9WgXcQ")

    def test_transcript_companion_also_carries_identity(self):
        import main
        doc = formatter.to_transcript_doc(self.META, "Verbatim prose.")
        self.assertEqual(main._extract_video_id(doc), "dQw4w9WgXcQ")

    def test_extract_returns_none_for_a_plain_clean_doc(self):
        doc = formatter.to_clean_markdown(self.META, "Just transcript.")
        self.assertIsNone(formatter.extract_article_body(doc))


class CreatedAtTests(unittest.TestCase):
    """
    `created_at` records when a video entered the knowledge base, which is what
    goes stale — `published` is a fact about the video and never changes.
    """

    META = {"title": "T", "channel": "C", "published": "2026-01-01",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "description": ""}
    SEGMENTS = [{"text": "hello there", "start": 0.0, "duration": 1.0}]

    def test_new_records_are_stamped(self):
        rec = formatter.to_jsonl_record(self.META, self.SEGMENTS, "/tmp/x.md")
        self.assertTrue(rec["created_at"])
        # date -Iseconds shape: 2026-08-09T13:57:27+10:00
        self.assertRegex(rec["created_at"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_an_existing_stamp_is_never_overwritten(self):
        meta = {**self.META, "created_at": "2020-01-01T00:00:00+00:00"}
        rec = formatter.to_jsonl_record(meta, self.SEGMENTS, "/tmp/x.md")
        self.assertEqual(rec["created_at"], "2020-01-01T00:00:00+00:00")

    def test_it_reaches_every_document_type(self):
        meta = {**self.META, "created_at": "2026-08-09T13:00:00+10:00"}
        enrichment = {"summary": "S", "key_concepts": [], "domains": [],
                      "difficulty": "beginner", "content_kind": "talk", "sections": []}
        for doc in (formatter.to_clean_markdown(meta, "body"),
                    formatter.to_transcript_doc(meta, "body"),
                    formatter.to_article_doc(meta, "body"),
                    formatter.to_article_doc(meta, "body", enrichment),
                    formatter.to_knowledge_doc(meta, "body", enrichment)):
            self.assertIn('created_at: "2026-08-09T13:00:00+10:00"', doc)

    def test_it_is_distinct_from_published(self):
        meta = {**self.META, "created_at": "2026-08-09T13:00:00+10:00"}
        doc = formatter.to_clean_markdown(meta, "body")
        self.assertIn('published: "2026-01-01"', doc)
        self.assertIn('created_at: "2026-08-09T13:00:00+10:00"', doc)

    def test_records_staged_before_the_field_existed_omit_it(self):
        doc = formatter.to_clean_markdown(self.META, "body")
        self.assertNotIn("created_at", doc,
                         "a missing stamp must be omitted, never invented")

    def test_rendering_twice_does_not_change_the_stamp(self):
        # clean/rewrite/enrich each rewrite the document; the stamp must not drift.
        meta = {**self.META, "created_at": "2026-08-09T13:00:00+10:00"}
        self.assertEqual(formatter.to_clean_markdown(meta, "body"),
                         formatter.to_clean_markdown(meta, "body"))

    def test_it_survives_the_record_to_metadata_hop(self):
        import main
        rec = formatter.to_jsonl_record(self.META, self.SEGMENTS, "/tmp/x.md")
        self.assertEqual(main._meta_from_record(rec)["created_at"], rec["created_at"])


class CompanionPathTests(unittest.TestCase):
    def test_transcript_path_is_derived_from_the_article_path(self):
        import main
        self.assertEqual(main._transcript_path("/blob/Ch/Title.md"),
                         "/blob/Ch/Title.transcript.md")

    def test_companion_is_recognised(self):
        import main
        self.assertTrue(main._is_transcript_companion("/blob/Ch/T.transcript.md"))
        self.assertFalse(main._is_transcript_companion("/blob/Ch/T.md"))


if __name__ == "__main__":
    unittest.main()
