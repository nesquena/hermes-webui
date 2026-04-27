"""
Regression tests for #1164 — .env file corruption by WebUI.

The WebUI's onboarding flow had a duplicate _write_env_file() without
_ENV_LOCK protection. Concurrent writes (e.g. Telegram bot + WebUI) could
corrupt the shared .env file. Additionally, both _write_env_file copies
rewrote the entire file from a parsed dict, stripping comments and
reordering keys alphabetically.

Fix:
- onboarding.py now imports _write_env_file from providers.py (which holds
  _ENV_LOCK from api.streaming for the entire load→modify→write cycle).
- _write_env_file in providers.py now preserves comments, blank lines, and
  original key order instead of rebuilding from a sorted dict.

Sprint/commit: v0.50.227+
"""
import os
import tempfile
import textwrap
import unittest
from pathlib import Path


class TestEnvFileCommentPreservation(unittest.TestCase):
    """Verify _write_env_file preserves comments, blank lines, and key order."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.env_path = Path(self.tmpdir) / ".env"
        # Must import AFTER setting up, as the module has top-level code
        from api.providers import _write_env_file
        self._write_env_file = _write_env_file

    def tearDown(self):
        # Clean os.environ entries set during tests
        for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "NEW_KEY"):
            os.environ.pop(key, None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read(self) -> str:
        return self.env_path.read_text(encoding="utf-8")

    # ── Comment preservation ──────────────────────────────────────────

    def test_comments_preserved_on_update(self):
        """Comments in .env must survive a key value update."""
        self.env_path.write_text(textwrap.dedent("""\
            # Hermes API keys
            OPENROUTER_API_KEY=sk-or-old
            # Another comment
            OPENAI_API_KEY=sk-oai-old
        """).strip() + "\n", encoding="utf-8")

        self._write_env_file(self.env_path, {"OPENROUTER_API_KEY": "sk-or-new"})

        content = self._read()
        self.assertIn("# Hermes API keys", content,
                      "Leading comment must be preserved")
        self.assertIn("# Another comment", content,
                      "Inline comment must be preserved")
        self.assertIn("sk-or-new", content)

    def test_blank_lines_preserved(self):
        """Blank lines between key blocks must be preserved."""
        self.env_path.write_text(
            "KEY_A=val_a\n\nKEY_B=val_b\n", encoding="utf-8")

        self._write_env_file(self.env_path, {"KEY_A": "updated"})

        content = self._read()
        self.assertEqual(content.count("\n\n"), 1,
                         "Blank line between keys must be preserved")

    def test_key_order_preserved(self):
        """Original key order must not be sorted alphabetically."""
        self.env_path.write_text(
            "ZZZ_KEY=last\nAAA_KEY=first\nBBB_KEY=middle\n",
            encoding="utf-8")

        self._write_env_file(self.env_path, {"AAA_KEY": "updated"})

        content = self._read()
        zzz_pos = content.find("ZZZ_KEY")
        aaa_pos = content.find("AAA_KEY")
        bbb_pos = content.find("BBB_KEY")
        # Original order: ZZZ, AAA, BBB
        self.assertLess(zzz_pos, aaa_pos,
                        "ZZZ_KEY must still come before AAA_KEY (original order)")
        self.assertLess(aaa_pos, bbb_pos,
                        "AAA_KEY must still come before BBB_KEY (original order)")

    def test_new_key_appended_with_separator(self):
        """New keys are appended at the end with a blank-line separator."""
        self.env_path.write_text(
            "EXISTING_KEY=value\n", encoding="utf-8")

        self._write_env_file(self.env_path, {"NEW_KEY": "new_value"})

        content = self._read()
        self.assertIn("NEW_KEY=new_value", content)
        # New key should appear after the existing one
        self.assertGreater(content.find("NEW_KEY"), content.find("EXISTING_KEY"))

    def test_key_removal_preserves_others(self):
        """Removing a key leaves other keys and comments intact."""
        self.env_path.write_text(textwrap.dedent("""\
            # Comment A
            KEY_A=val_a
            # Comment B
            KEY_B=val_b
        """).strip() + "\n", encoding="utf-8")

        self._write_env_file(self.env_path, {"KEY_B": None})

        content = self._read()
        self.assertIn("KEY_A=val_a", content)
        self.assertIn("# Comment A", content)
        self.assertNotIn("KEY_B", content)
        # Comment B stays (it's just a comment, not tied to KEY_B structurally)
        self.assertIn("# Comment B", content)

    def test_empty_file_handled_gracefully(self):
        """Writing to a non-existent .env file works."""
        self.assertFalse(self.env_path.exists())
        self._write_env_file(self.env_path, {"NEW_KEY": "value"})
        self.assertTrue(self.env_path.exists())
        self.assertEqual(self._read().strip(), "NEW_KEY=value")


class TestOnboardingUsesProviderWriteEnv(unittest.TestCase):
    """Verify that onboarding.py delegates to providers._write_env_file
    (which holds _ENV_LOCK), eliminating the duplicate unprotected path."""

    def test_onboarding_imports_write_env_from_providers(self):
        """api.onboarding._write_env_file must be the same object as
        api.providers._write_env_file (shared implementation with lock)."""
        from api import onboarding, providers
        self.assertIs(
            onboarding._write_env_file,
            providers._write_env_file,
            "onboarding must use providers._write_env_file for thread safety (#1164)"
        )

    def test_providers_write_env_holds_env_lock(self):
        """providers._write_env_file must acquire _ENV_LOCK from api.streaming."""
        import inspect
        from api.providers import _write_env_file
        source = inspect.getsource(_write_env_file)
        self.assertIn("_ENV_LOCK", source,
                      "_write_env_file must use _ENV_LOCK for concurrency safety")
        self.assertIn("from api.streaming import _ENV_LOCK", source,
                      "_ENV_LOCK must be imported from api.streaming")

    def test_providers_write_env_uses_atomic_rename(self):
        """providers._write_env_file must write atomically via tempfile +
        os.replace so cross-process readers (Telegram, CLI) never observe
        a truncated half-written file (#1164 cross-process leg)."""
        import inspect
        from api.providers import _write_env_file
        source = inspect.getsource(_write_env_file)
        self.assertIn("tempfile", source,
                      "_write_env_file must stage writes through a tempfile")
        self.assertIn("os.replace(", source,
                      "_write_env_file must atomically rename via os.replace")
        # The original O_TRUNC pattern must NOT remain — it is the source of
        # the cross-process race the PR is closing.
        self.assertNotIn("O_TRUNC", source,
                         "_write_env_file must not truncate-in-place (#1164)")
