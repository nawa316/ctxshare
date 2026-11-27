import json
import tempfile
import unittest
from pathlib import Path

from ctxshare.ingest import (
    parse_html,
    parse_transcript,
    _parse_chatgpt_data,
    _parse_claude_snapshot,
    _parse_gemini_shared,
    capsules_from_json,
    load_file,
)


class TranscriptTests(unittest.TestCase):
    def test_roles_and_title(self):
        text = """# Riset kopi

You said:
Halo!

ChatGPT said:
Halo juga. Mau bahas apa?

Human: Harga kopi Jakarta.

Assistant: Sekitar 25-40 ribu.
"""
        capsule = parse_transcript(text)
        self.assertEqual(capsule.title, "Riset kopi")
        self.assertEqual(capsule.provider, "ChatGPT")
        self.assertEqual([m.role for m in capsule.messages], ["user", "assistant", "user", "assistant"])
        self.assertIn("Halo juga", capsule.messages[1].content)

    def test_provider_labels(self):
        text = "Gemini said:\nHai\n\nYou said:\nHalo"
        capsule = parse_transcript(text)
        self.assertEqual(capsule.provider, "Gemini")
        self.assertEqual(capsule.messages[0].role, "assistant")
        self.assertEqual(capsule.messages[1].role, "user")

    def test_no_labels_single_user_message(self):
        capsule = parse_transcript("Ini transkrip tanpa label.\nBaris kedua.")
        self.assertEqual(len(capsule.messages), 1)
        self.assertEqual(capsule.messages[0].role, "user")
        self.assertIn("Label peran tidak terdeteksi", capsule.note)

    def test_system_block_is_extracted(self):
        text = "System: Jawab formal.\n\nYou said:\nHai\n\nAssistant: Halo."
        capsule = parse_transcript(text)
        self.assertIn("Jawab formal.", capsule.system)
        self.assertEqual([m.role for m in capsule.messages], ["user", "assistant"])


class ChatGPTParserTests(unittest.TestCase):
    def test_linear_conversation(self):
        data = {
            "title": "Contoh",
            "create_time": 1700000000,
            "default_model_slug": "gpt-4o",
            "linear_conversation": [
                {"id": "root", "children": ["a"]},
                {
                    "id": "a",
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["Halo"]},
                        "create_time": 1700000001,
                    },
                },
                {
                    "id": "b",
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["Hai, ada yang bisa dibantu?"]},
                        "create_time": 1700000002,
                    },
                },
                {
                    "id": "hidden",
                    "message": {
                        "author": {"role": "system"},
                        "content": {"parts": ["instruksi"]},
                        "metadata": {"is_visually_hidden_from_conversation": True},
                    },
                },
            ],
        }
        capsule = _parse_chatgpt_data(data, source_url="https://chatgpt.com/share/abc")
        self.assertEqual(capsule.title, "Contoh")
        self.assertEqual(capsule.provider, "ChatGPT")
        self.assertEqual(len(capsule.messages), 2)
        self.assertEqual(capsule.messages[1].role, "assistant")

    def test_mapping_current_node(self):
        data = {
            "title": "Mapping",
            "mapping": {
                "root": {"id": "root", "parent": None, "children": ["a"]},
                "a": {
                    "id": "a",
                    "parent": "root",
                    "children": ["b"],
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["pertanyaan"]},
                    },
                },
                "b": {
                    "id": "b",
                    "parent": "a",
                    "children": [],
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["jawaban"]},
                    },
                },
            },
            "current_node": "b",
        }
        capsule = _parse_chatgpt_data(data)
        self.assertEqual([m.content for m in capsule.messages], ["pertanyaan", "jawaban"])


class ClaudeParserTests(unittest.TestCase):
    def test_snapshot(self):
        data = {
            "uuid": "snap-1",
            "snapshot_name": "Claude chat",
            "created_at": "2025-01-01T10:00:00Z",
            "chat_messages": [
                {
                    "index": 0,
                    "sender": "human",
                    "content": [{"type": "text", "text": "Halo Claude"}],
                    "created_at": "2025-01-01T10:00:01Z",
                },
                {
                    "index": 1,
                    "sender": "assistant",
                    "text": "",
                    "content": [{"type": "text", "text": "Halo! Ada yang bisa saya bantu?"}],
                },
            ],
        }
        capsule = _parse_claude_snapshot(data, source_url="https://claude.ai/share/x")
        self.assertEqual(capsule.title, "Claude chat")
        self.assertEqual(capsule.provider, "Claude")
        self.assertEqual([m.role for m in capsule.messages], ["user", "assistant"])


class GeminiParserTests(unittest.TestCase):
    def test_ast_text_lists_code_table(self):
        shared = {
            "externalUrlId": "abc",
            "landingPageMetadata": {
                "title": "Gemini test",
                "conversationLabelInfo": {"modeTitle": "4 Pro"},
            },
            "content": {
                "content": "conversation",
                "conversation": {
                    "turns": [
                        {
                            "request": {"query": {"text": "Halo"}},
                            "response": {
                                "selectedParsedResponse": {
                                    "__typename": "assistant_boq_bard_application_ast_InlineSection",
                                    "constrainedChildren": [
                                        {
                                            "__typename": "assistant_boq_bard_application_ast_Text",
                                            "text": "Berikut jawabannya.",
                                        },
                                        {
                                            "__typename": "assistant_boq_bard_application_ast_InlineCitations",
                                            "inlineCitationContent": {
                                                "responseSpanContentMetadata": {"title": "JANGAN DIPAKAI"}
                                            },
                                        },
                                        {
                                            "__typename": "assistant_boq_bard_application_ast_UnorderedList",
                                            "flatEntries": [
                                                {"kind": "UNORDERED_LIST", "depth": 0},
                                                {
                                                    "kind": "LEAF",
                                                    "depth": 2,
                                                    "leaf": {
                                                        "__typename": "assistant_boq_bard_application_ast_Text",
                                                        "text": "Poin satu",
                                                    },
                                                },
                                            ],
                                        },
                                        {
                                            "__typename": "assistant_boq_bard_application_ast_Code",
                                            "code": "print('hi')",
                                            "info": "python",
                                        },
                                    ],
                                }
                            },
                        }
                    ]
                },
            },
        }
        capsule = _parse_gemini_shared(shared, source_url="https://gemini.google.com/share/abc")
        self.assertEqual(capsule.title, "Gemini test")
        self.assertIn("Gemini 4 Pro", capsule.model)
        assistant = capsule.messages[1].content
        self.assertIn("Berikut jawabannya.", assistant)
        self.assertIn("- Poin satu", assistant)
        self.assertIn("```python", assistant)
        self.assertNotIn("JANGAN DIPAKAI", assistant)


class HTMLTests(unittest.TestCase):
    def test_chatgpt_dom(self):
        html = """<html><head><title>ChatGPT - Test</title></head><body>
        <div data-message-author-role="user">Halo <b>dunia</b></div>
        <div data-message-author-role="assistant">Hai! <p>Ada yang bisa dibantu?</p></div>
        </body></html>"""
        capsule = parse_html(html)
        self.assertEqual(capsule.title, "Test")
        self.assertEqual(capsule.provider, "ChatGPT")
        self.assertEqual(len(capsule.messages), 2)
        self.assertIn("Halo dunia", capsule.messages[0].content)

    def test_claude_dom(self):
        html = """<html><body>
        <div data-testid="user-message">Halo Claude</div>
        <div class="font-claude-message"><p>Siap membantu.</p></div>
        </body></html>"""
        capsule = parse_html(html)
        self.assertEqual(capsule.provider, "Claude")
        self.assertEqual(len(capsule.messages), 2)

    def test_gemini_dom(self):
        html = """<html><body>
        <user-query><div>Halo Gemini</div></user-query>
        <model-response><div>Halo juga!</div></model-response>
        </body></html>"""
        capsule = parse_html(html)
        self.assertEqual(capsule.provider, "Gemini")
        self.assertEqual(len(capsule.messages), 2)
        self.assertIn("Halo juga", capsule.messages[1].content)

    def test_title_is_not_a_message(self):
        html = "<html><head><title>Hanya judul</title></head><body><p>bukan chat</p></body></html>"
        capsule = parse_html(html)
        self.assertEqual(capsule.messages[0].role, "user")
        self.assertNotIn("Hanya judul", capsule.messages[0].content)


class ExportTests(unittest.TestCase):
    def test_chatgpt_export_list(self):
        data = [
            {
                "title": "Satu",
                "mapping": {
                    "a": {
                        "id": "a",
                        "parent": None,
                        "children": [],
                        "message": {
                            "author": {"role": "user"},
                            "content": {"parts": ["halo"]},
                        },
                    }
                },
                "current_node": "a",
            }
        ]
        capsules = capsules_from_json(data, source_name="conversations.json")
        self.assertEqual(len(capsules), 1)
        self.assertEqual(capsules[0].title, "Satu")

    def test_claude_export_list(self):
        data = [
            {
                "uuid": "u1",
                "name": "Claude export",
                "chat_messages": [
                    {"index": 0, "sender": "human", "text": "hai"},
                    {"index": 1, "sender": "assistant", "text": "halo"},
                ],
            }
        ]
        capsules = capsules_from_json(data, source_name="conversations.json")
        self.assertEqual(len(capsules), 1)
        self.assertEqual(capsules[0].title, "Claude export")

    def test_load_file_transcript(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chat.txt"
            path.write_text("You said:\nHalo\n\nClaude said:\nHai", encoding="utf-8")
            capsules = load_file(path)
            self.assertEqual(len(capsules), 1)
            self.assertEqual(capsules[0].provider, "Claude")


if __name__ == "__main__":
    unittest.main()
