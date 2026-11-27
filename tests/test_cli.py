import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ctxshare.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = str(Path(self.temp.name) / "data")
        self.config = str(Path(self.temp.name) / "config" / "config.json")
        self.transcript = Path(self.temp.name) / "chat.txt"
        self.transcript.write_text(
            "# Judul CLI\n\nYou said:\nHalo\n\nClaude said:\nHai!",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, stdin=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_stdin = None
        if stdin is not None:
            old_stdin = __import__("sys").stdin
            __import__("sys").stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    ["--data-dir", self.data, "--config", self.config, *args]
                )
        finally:
            if old_stdin is not None:
                __import__("sys").stdin = old_stdin
        return code, stdout.getvalue(), stderr.getvalue()

    def test_import_list_share_delete(self):
        code, out, err = self.run_cli("import", str(self.transcript), "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        capsule_id = payload[0]["id"]
        self.assertEqual(payload[0]["title"], "Judul CLI")

        code, out, err = self.run_cli("list", "--json")
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], capsule_id)

        code, out, err = self.run_cli(
            "share", capsule_id, "--print", "prompt", "--target", "gemini"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("Kamu adalah Gemini", out)

        code, out, err = self.run_cli("delete", capsule_id, "--yes")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("list", "--json")
        self.assertEqual(json.loads(out), [])

    def test_import_stdin_and_code_roundtrip(self):
        code, out, err = self.run_cli("import", "-", "--json", stdin="You said:\nHalo\n\nAssistant said:\nHai")
        self.assertEqual(code, 0, err)
        capsule_id = json.loads(out)[0]["id"]
        code, out, err = self.run_cli("show", capsule_id, "--prompt", "--target", "claude")
        self.assertEqual(code, 0, err)
        self.assertIn("Kamu adalah Claude", out)

        code, out, err = self.run_cli("share", capsule_id, "--print", "code")
        self.assertEqual(code, 0, err)
        ctx_code = out.strip()
        self.assertTrue(ctx_code.startswith("ctx1."))
        code, out, err = self.run_cli("import", ctx_code, "--json")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)[0]["saved"])

    def test_missing_source_is_error(self):
        code, out, err = self.run_cli("import", "bukan-link")
        self.assertEqual(code, 1)
        self.assertIn("Format tidak dikenal", err)

    @mock.patch("ctxshare.tui.run_tui", return_value=0)
    @mock.patch("ctxshare.cli.is_tty", return_value=True)
    def test_no_command_opens_tui(self, _is_tty, run_tui):
        code, _out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        run_tui.assert_called_once()
        _args, kwargs = run_tui.call_args
        self.assertIn("auto_serve", kwargs)
        self.assertIn("target", kwargs)


if __name__ == "__main__":
    unittest.main()
