import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import env_int, load_env_file


class EnvFile(unittest.TestCase):
    def load(self, text: str, **existing) -> dict:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, existing, clear=True):
            path = Path(tmp) / ".env"
            path.write_text(text, encoding="utf-8")
            load_env_file(path)
            return dict(os.environ)

    def test_basic_lines_comments_blanks_and_quotes(self):
        env = self.load('# a comment\n\nA=1\nB = two \nC="quoted value"\nD=\'single\'\nE=has=equals\nexport F=exported\nG=\n  # indented comment\nnot a setting\n')
        self.assertEqual({k: env[k] for k in "ABCDEFG"}, {"A": "1", "B": "two", "C": "quoted value", "D": "single", "E": "has=equals", "F": "exported", "G": ""})
        self.assertNotIn("not a setting", env)

    def test_real_environment_wins(self):
        self.assertEqual(self.load("TOKEN=from-file\n", TOKEN="from-shell")["TOKEN"], "from-shell")

    def test_windows_line_endings_and_a_byte_order_mark(self):
        env = self.load("﻿A=1\r\nB=2\r\n")
        self.assertEqual((env["A"], env["B"]), ("1", "2"))

    def test_a_missing_file_is_fine(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            load_env_file(Path("/nonexistent/.env"))

    def test_mismatched_quotes_are_left_alone(self):
        self.assertEqual(self.load("A=\"oops\n")["A"], '"oops')


class EnvInt(unittest.TestCase):
    def test_values(self):
        with mock.patch.dict(os.environ, {"A": "123456789012345678", "B": "", "C": "  42 "}, clear=True):
            self.assertEqual(env_int("A"), 123456789012345678)
            self.assertIsNone(env_int("B"))
            self.assertIsNone(env_int("MISSING"))
            self.assertEqual(env_int("C"), 42)

    def test_junk_is_a_clear_error_not_a_stack_trace_later(self):
        with mock.patch.dict(os.environ, {"GUILD_ID": "my-server"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GUILD_ID must be a number"):
                env_int("GUILD_ID")


if __name__ == "__main__":
    unittest.main()
