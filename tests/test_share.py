import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ctxshare.capsule import Capsule, Message
from ctxshare.errors import CapsuleError, CtxshareError
from ctxshare.share import create_gist, resolve_input
from ctxshare.store import Store


def sample(title="Gist test") -> Capsule:
    return Capsule(
        title=title,
        provider="ChatGPT",
        messages=[Message("user", "halo"), Message("assistant", "hai")],
    )


class ResolveInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_code(self):
        code = sample().encode_code()
        capsules = resolve_input(code, self.store)
        self.assertEqual(capsules[0].title, "Gist test")

    def test_file(self):
        path = Path(self.temp.name) / "capsule.json"
        path.write_text(sample().to_json(pretty=True), encoding="utf-8")
        capsules = resolve_input(str(path), self.store)
        self.assertEqual(capsules[0].title, "Gist test")

    def test_local_share_token(self):
        capsule_id = self.store.save_capsule(sample())
        share = self.store.create_share(capsule_id)
        capsules = resolve_input(share["token"], self.store)
        self.assertEqual(capsules[0].title, "Gist test")

    def test_local_capsule_id(self):
        capsule_id = self.store.save_capsule(sample())
        capsules = resolve_input(capsule_id, self.store)
        self.assertEqual(capsules[0].title, "Gist test")

    def test_multiline_transcript(self):
        capsules = resolve_input("You said:\nHalo\n\nAssistant said:\nHai", self.store)
        self.assertEqual(capsules[0].provider, "Tempelan")

    def test_unknown_text(self):
        with self.assertRaises(CapsuleError):
            resolve_input("bukan link", self.store)


class GistTests(unittest.TestCase):
    @mock.patch("ctxshare.share.subprocess.run")
    @mock.patch("ctxshare.share.shutil.which", return_value="/usr/bin/gh")
    def test_create_gist(self, _which, run):
        seen = {}

        def fake_run(command, cwd=None, capture_output=None, text=None, timeout=None):
            seen["files"] = sorted(os.listdir(cwd))
            seen["capsule"] = json.loads(Path(cwd, "capsule.json").read_text(encoding="utf-8"))
            seen["readme"] = Path(cwd, "README.md").read_text(encoding="utf-8")
            return SimpleNamespace(
                returncode=0,
                stdout="https://gist.github.com/user/abc123\n",
                stderr="",
            )

        run.side_effect = fake_run
        url = create_gist(sample())
        self.assertEqual(url, "https://gist.github.com/user/abc123")
        self.assertEqual(seen["files"], ["README.md", "capsule.json"])
        self.assertEqual(seen["capsule"]["title"], "Gist test")
        self.assertIn("# Gist test", seen["readme"])

    @mock.patch("ctxshare.share.subprocess.run")
    @mock.patch("ctxshare.share.shutil.which", return_value="/usr/bin/gh")
    def test_gist_scope_hint(self, _which, run):
        run.return_value = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr='This API operation needs the "gist" scope.',
        )
        with self.assertRaises(CtxshareError) as context:
            create_gist(sample())
        self.assertIn("gh auth refresh", str(context.exception))


if __name__ == "__main__":
    unittest.main()
