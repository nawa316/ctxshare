import inspect
import unittest
from pathlib import Path

from ctxshare import tui


class TuiStaticTests(unittest.TestCase):
    def test_dialog_classes_exist(self):
        for name in (
            "Dialog",
            "ListDialog",
            "PromptDialog",
            "ConfirmDialog",
            "TextDialog",
            "JobDialog",
            "PreviewDialog",
            "ShareDialog",
        ):
            self.assertTrue(hasattr(tui, name), f"{name} hilang dari modul TUI")

    def test_share_dialog_is_reachable_from_viewer(self):
        source = inspect.getsource(tui.TuiApp.share_capsule)
        self.assertIn("ShareDialog", source)

    def test_chrome_is_ascii(self):
        source = Path(inspect.getfile(tui)).read_text(encoding="utf-8")
        non_ascii = sorted({char for char in source if ord(char) > 127})
        self.assertEqual(non_ascii, [], f"karakter non-ASCII di TUI: {non_ascii}")

    def test_fit_text(self):
        self.assertEqual(tui.fit_text("hello", 3), "hel")
        self.assertEqual(tui.fit_text("a\tb", 3), "a b")
        self.assertEqual(tui.fit_text("", 10), "")

    def test_wrap_text(self):
        lines = tui.wrap_text("satu dua tiga empat", 9)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) <= 9 for line in lines))
        self.assertEqual(tui.wrap_text("", 10), [])


if __name__ == "__main__":
    unittest.main()
