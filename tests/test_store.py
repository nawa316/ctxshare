import tempfile
import time
import unittest
from pathlib import Path

from ctxshare.capsule import Capsule, Message
from ctxshare.errors import CtxshareError
from ctxshare.store import Store


def capsule(title="Judul", url="https://chatgpt.com/share/abc") -> Capsule:
    return Capsule(
        title=title,
        provider="ChatGPT",
        model="gpt-4o",
        source_url=url,
        origin="chatgpt",
        messages=[Message("user", "halo"), Message("assistant", "hai")],
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_save_list_get_delete(self):
        saved = capsule()
        capsule_id = self.store.save_capsule(saved)
        rows = self.store.list_capsules()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], capsule_id)
        self.assertEqual(rows[0]["message_count"], 2)
        loaded = self.store.get_capsule(capsule_id[:6])
        self.assertEqual(loaded.title, "Judul")
        self.store.delete_capsule(capsule_id)
        self.assertEqual(self.store.list_capsules(), [])

    def test_same_source_url_updates(self):
        first = self.store.save_capsule(capsule("Lama"))
        second = self.store.save_capsule(capsule("Baru"))
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.list_capsules()), 1)
        self.assertEqual(self.store.get_capsule(first).title, "Baru")

    def test_search(self):
        self.store.save_capsule(capsule("Kopi Jakarta", "https://chatgpt.com/share/1"))
        self.store.save_capsule(capsule("Riset teh", "https://chatgpt.com/share/2"))
        self.assertEqual(len(self.store.list_capsules("kopi")), 1)
        self.assertEqual(len(self.store.list_capsules("ChatGPT")), 2)

    def test_share_lifecycle(self):
        capsule_id = self.store.save_capsule(capsule())
        share = self.store.create_share(capsule_id, ttl_hours=1)
        record = self.store.get_share(share["token"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["capsule"].title, "Judul")
        self.assertEqual(self.store.stats()["shares"], 1)
        self.store.delete_capsule(capsule_id)
        self.assertIsNone(self.store.get_share(share["token"]))

    def test_expired_share_is_removed(self):
        capsule_id = self.store.save_capsule(capsule())
        share = self.store.create_share(capsule_id, ttl_hours=1)
        with self.store._write() as conn:
            conn.execute(
                "UPDATE shares SET expires_at = ? WHERE token = ?",
                (time.time() - 5, share["token"]),
            )
        self.assertIsNone(self.store.get_share(share["token"]))
        self.assertEqual(self.store.stats()["shares"], 0)

    def test_resolve_ambiguous_prefix(self):
        first = capsule("A", "https://chatgpt.com/share/a")
        first.id = "abcd111111111111"
        second = capsule("B", "https://chatgpt.com/share/b")
        second.id = "abcd222222222222"
        self.store.save_capsule(first)
        self.store.save_capsule(second)
        with self.assertRaises(CtxshareError):
            self.store.resolve_id("abcd")


if __name__ == "__main__":
    unittest.main()
