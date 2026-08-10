import importlib
import os
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EnvOverrideTests(unittest.TestCase):
    """
    Every setting is `os.getenv(NAME, default)`, so .env wins over the default
    and no caller needs to know a default exists.
    """

    def _reload_with(self, **env):
        with patch.dict(os.environ, env, clear=False):
            return importlib.reload(config)

    def tearDown(self):
        importlib.reload(config)  # restore the real environment for other tests

    def test_environment_overrides_the_default(self):
        reloaded = self._reload_with(OLLAMA_MODEL="probe:1b", REWRITE_MODEL="other:2b")
        self.assertEqual(reloaded.OLLAMA_MODEL, "probe:1b")
        self.assertEqual(reloaded.REWRITE_MODEL, "other:2b")

    def test_the_two_model_settings_are_independent(self):
        # Rewriting rewards a strong writer; enrichment only fills in JSON.
        # Setting one must never silently move the other.
        reloaded = self._reload_with(OLLAMA_MODEL="only-enrichment:1b")
        self.assertEqual(reloaded.OLLAMA_MODEL, "only-enrichment:1b")
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
            [sys.executable, "-c", "import config; print(config.OLLAMA_MODEL)"],
            cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "OLLAMA_MODEL": "from-environment:9b"},
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
