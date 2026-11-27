import unittest

from ctxshare.capsule import Capsule, CapsuleError, Message, target_window


def make_capsule() -> Capsule:
    return Capsule(
        title="Riset pasar",
        provider="ChatGPT",
        model="gpt-4o",
        source_url="https://chatgpt.com/share/abc",
        origin="chatgpt",
        system="Jawab singkat.",
        messages=[
            Message("user", "Halo, bantu riset kopi."),
            Message("assistant", "Siap. Mau fokus kota mana?"),
            Message("user", "Jakarta dan Bandung."),
        ],
    )


class CapsuleTests(unittest.TestCase):
    def test_roundtrip_and_checksum(self):
        capsule = make_capsule()
        data = capsule.to_dict()
        self.assertEqual(data["format"], "ctxshare.capsule/1")
        self.assertEqual(data["source"]["url"], capsule.source_url)
        restored = Capsule.from_dict(data)
        self.assertEqual(restored.title, capsule.title)
        self.assertEqual(restored.messages[-1].content, "Jakarta dan Bandung.")
        self.assertEqual(restored.source_url, capsule.source_url)

    def test_tampered_checksum_is_rejected(self):
        data = make_capsule().to_dict()
        data["title"] = "judul palsu"
        with self.assertRaises(CapsuleError):
            Capsule.from_dict(data)

    def test_code_roundtrip(self):
        capsule = make_capsule()
        code = capsule.encode_code()
        self.assertTrue(code.startswith("ctx1."))
        self.assertGreater(len(code), 50)
        restored = Capsule.decode_code(code)
        self.assertEqual(restored.title, capsule.title)
        self.assertEqual(len(restored.messages), 3)

    def test_truncation_keeps_system_and_newest(self):
        capsule = Capsule(
            title="panjang",
            system="sys",
            messages=[Message("user", "x" * 100), Message("assistant", "y" * 100)],
        )
        result = capsule.truncated(150)
        self.assertEqual(len(result.messages), 1)
        self.assertIn("dipotong", result.system)
        self.assertEqual(result.messages[-1].role, "assistant")

    def test_handoff_prompt_target(self):
        capsule = make_capsule()
        self.assertIn("Kamu adalah Claude", capsule.handoff_prompt("claude"))
        self.assertIn("=== TRANSKRIP ===", capsule.handoff_prompt("gemini"))
        self.assertIn("Jakarta dan Bandung", capsule.handoff_prompt("chatgpt"))
        self.assertEqual(target_window("gemini"), 1048576)

    def test_markdown_contains_source(self):
        markdown = make_capsule().to_markdown()
        self.assertIn("# Riset pasar", markdown)
        self.assertIn("https://chatgpt.com/share/abc", markdown)
        self.assertIn("## 1. User", markdown)

    def test_system_in_prompt(self):
        capsule = make_capsule()
        prompt = capsule.handoff_prompt("claude")
        self.assertIn("Jawab singkat.", prompt)
        self.assertIn("=== INSTRUKSI SISTEM ===", prompt)


if __name__ == "__main__":
    unittest.main()
