import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Model settings the developer's own .env may set. Stripped from the
# environment of clean-dir subprocesses so those assert the code's fallbacks
# rather than whichever provider this machine is pointed at today.
_MODEL_VARS = frozenset({"ENRICH_MODEL", "OLLAMA_MODEL", "REWRITE_MODEL"})


class EnvOverrideTests(unittest.TestCase):
    """
    Every setting is `os.getenv(NAME, default)`, so .env wins over the default
    and no caller needs to know a default exists.
    """

    def _reload_with(self, **env):
        """
        Reload config with these variables set.

        Only ever *sets* variables — never relies on unsetting one, because
        config calls load_dotenv() on import and would immediately restore it
        from the developer's real .env. Fallback behaviour is tested in
        _config_in_clean_dir instead.
        """
        with patch.dict(os.environ, env, clear=False):
            return importlib.reload(config)

    def _config_in_clean_dir(self, expr: str, **env) -> str:
        """
        Evaluate `expr` against a config imported where no .env exists.

        The developer's own .env legitimately sets ENRICH_MODEL and friends, and
        a default/fallback test that reads it is testing the machine rather than
        the code.
        """
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("config.py",):
                shutil.copy(os.path.join(ROOT, name), tmp)
            out = subprocess.run(
                [sys.executable, "-c", f"import config; print({expr})"],
                cwd=tmp, capture_output=True, text=True,
                env={k: v for k, v in {**os.environ, **env}.items()
                     if k not in _MODEL_VARS or k in env},
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout.strip()

    def tearDown(self):
        importlib.reload(config)  # restore the real environment for other tests

    def test_environment_overrides_the_default(self):
        reloaded = self._reload_with(ENRICH_MODEL="probe:1b", REWRITE_MODEL="other:2b")
        self.assertEqual(reloaded.ENRICH_MODEL, "probe:1b")
        self.assertEqual(reloaded.REWRITE_MODEL, "other:2b")

    def test_ollama_model_still_works_as_a_legacy_alias(self):
        # OLLAMA_MODEL was the enrichment setting before the pipeline could
        # target a non-Ollama provider. Existing .env files must keep working.
        self.assertEqual(
            self._config_in_clean_dir("config.ENRICH_MODEL", OLLAMA_MODEL="legacy:1b"),
            "legacy:1b",
        )

    def test_explicit_enrich_model_wins_over_the_alias(self):
        self.assertEqual(
            self._config_in_clean_dir("config.ENRICH_MODEL",
                                      ENRICH_MODEL="new:2b", OLLAMA_MODEL="legacy:1b"),
            "new:2b",
        )

    def test_the_two_model_settings_are_independent(self):
        # Rewriting rewards a strong writer; enrichment only fills in JSON.
        # Setting one must never silently move the other.
        reloaded = self._reload_with(ENRICH_MODEL="only-enrichment:1b")
        self.assertEqual(reloaded.ENRICH_MODEL, "only-enrichment:1b")
        self.assertNotEqual(reloaded.REWRITE_MODEL, "only-enrichment:1b")

    def test_numeric_and_boolean_settings_parse_from_strings(self):
        reloaded = self._reload_with(
            REWRITE_CHUNK_WORDS="123", REWRITE_MIN_RATIO="0.4", REWRITE_ENABLED="0",
        )
        self.assertEqual(reloaded.REWRITE_CHUNK_WORDS, 123)
        self.assertAlmostEqual(reloaded.REWRITE_MIN_RATIO, 0.4)
        self.assertIs(reloaded.REWRITE_ENABLED, False)


class ImportContractTests(unittest.TestCase):
    def test_importing_config_alone_applies_dotenv(self):
        """
        config used to rely on main.py/server.py calling load_dotenv() first, so
        any other caller silently got defaults while believing it had read .env.
        """
        out = subprocess.run(
            [sys.executable, "-c", "import config; print(config.ENRICH_MODEL)"],
            cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "ENRICH_MODEL": "from-environment:9b"},
        )
        self.assertEqual(out.stdout.strip(), "from-environment:9b", out.stderr)

    def test_no_entry_point_re_declares_a_model_default(self):
        """
        A model name written down anywhere but config.py is a second source of
        truth, and it drifts. setup.sh asks config for these instead.
        """
        pattern = re.compile(r"(qwen|gemma|llama|mistral)[\w.]*:\w+")
        for name in ("setup.sh", "start.sh", "enrich.sh"):
            path = os.path.join(ROOT, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                code = "\n".join(
                    line for line in f if not line.lstrip().startswith("#")
                )
            self.assertIsNone(pattern.search(code),
                              f"{name} hardcodes a model name; read it from config instead")


class SetupScriptContractTests(unittest.TestCase):
    """setup.sh probes config by attribute name; those names must exist."""

    PROBED = ("OLLAMA_HOST", "OLLAMA_MODEL", "REWRITE_MODEL", "REWRITE_ENABLED")

    def test_every_probed_attribute_exists(self):
        for attr in self.PROBED:
            self.assertTrue(hasattr(config, attr), f"setup.sh reads config.{attr}")

    def test_setup_script_probes_only_attributes_that_exist(self):
        with open(os.path.join(ROOT, "setup.sh"), encoding="utf-8") as f:
            script = f.read()
        for attr in re.findall(r"ask_config (\w+)", script):
            self.assertTrue(hasattr(config, attr),
                            f"setup.sh probes config.{attr}, which does not exist")

    def test_rewrite_enabled_renders_as_setup_expects(self):
        # setup.sh compares the printed value against the string "True".
        self.assertIn(str(config.REWRITE_ENABLED), {"True", "False"})


if __name__ == "__main__":
    unittest.main()
