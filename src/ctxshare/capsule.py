"""Portable context capsules for moving chats between AI providers.

A capsule is provider-neutral JSON: title, source, system prompt, messages and
a checksum.  It can travel as a file, a link, a secret gist, or a compressed
``ctx1.`` code that fits in a chat message.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import __version__
from .errors import CapsuleError
from .util import estimate_tokens, fmt_time

FORMAT = "ctxshare.capsule/1"
CODE_PREFIX = "ctx1."
ROLES = ("system", "user", "assistant")
MAX_MESSAGES = 5000
MAX_TOTAL_CHARS = 8_000_000
MAX_CODE_BYTES = 8 * 1024 * 1024

TARGETS: Dict[str, Dict[str, Any]] = {
    "chatgpt": {
        "label": "ChatGPT",
        "window": 128_000,
        "url": "https://chatgpt.com/",
        "hint": "Buka chatgpt.com → New chat → tempel prompt.",
    },
    "claude": {
        "label": "Claude",
        "window": 200_000,
        "url": "https://claude.ai/new",
        "hint": "Buka claude.ai/new → tempel prompt.",
    },
    "gemini": {
        "label": "Gemini",
        "window": 1_048_576,
        "url": "https://gemini.google.com/app",
        "hint": "Buka gemini.google.com → New chat → tempel prompt.",
    },
    "generic": {
        "label": "chatbot lain",
        "window": 0,
        "url": "",
        "hint": "Tempel prompt ke chat baru di chatbot tujuan.",
    },
}

SOURCE_LABELS = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "paste": "Teks tempel",
    "file": "File",
    "generic": "Chatbot",
}


@dataclass
class Message:
    role: str
    content: str
    ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.ts:
            data["ts"] = float(self.ts)
        return data

    @classmethod
    def from_dict(cls, data: Any, index: int = 0) -> "Message":
        if not isinstance(data, dict):
            raise CapsuleError(f"Pesan #{index + 1} bukan objek")
        role = str(data.get("role") or "").strip().lower()
        if role not in ROLES:
            raise CapsuleError(
                f"Pesan #{index + 1}: role '{role}' tidak valid (system/user/assistant)"
            )
        content = data.get("content")
        if not isinstance(content, str):
            raise CapsuleError(f"Pesan #{index + 1}: content harus string")
        content = content.replace("\x00", "").strip()
        if not content:
            raise CapsuleError(f"Pesan #{index + 1}: content kosong")
        ts = _number(data.get("ts"), 0.0)
        return cls(role=role, content=content, ts=ts)


def _number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


@dataclass
class Capsule:
    title: str
    messages: List[Message]
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    created_at: float = field(default_factory=time.time)
    provider: str = ""
    model: str = ""
    system: str = ""
    note: str = ""
    source_url: str = ""
    origin: str = ""
    app: str = field(default_factory=lambda: f"ctxshare/{__version__}")

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "format": FORMAT,
            "id": self.id,
            "title": self.title,
            "created_at": round(float(self.created_at), 3),
            "created_at_iso": datetime.fromtimestamp(self.created_at).isoformat(timespec="seconds"),
            "source": {
                "provider": self.provider,
                "model": self.model,
                "url": self.source_url,
                "origin": self.origin,
            },
            "system": self.system,
            "messages": [message.to_dict() for message in self.messages],
            "note": self.note,
            "app": self.app,
        }
        body["checksum"] = checksum_of(body)
        return body

    @classmethod
    def from_dict(cls, data: Any, verify_checksum: bool = True) -> "Capsule":
        if not isinstance(data, dict):
            raise CapsuleError("Capsule harus berupa objek JSON")
        fmt = str(data.get("format") or "")
        if not fmt.startswith("ctxshare.capsule/"):
            raise CapsuleError("Bukan capsule ctxshare (field 'format' tidak dikenal)")
        if fmt != FORMAT:
            raise CapsuleError(f"Versi capsule {fmt} belum didukung (butuh {FORMAT})")
        if verify_checksum and data.get("checksum"):
            expected = str(data["checksum"])
            actual = checksum_of({k: v for k, v in data.items() if k != "checksum"})
            if not secrets.compare_digest(expected, actual):
                raise CapsuleError("Checksum capsule tidak cocok (file rusak atau diubah)")
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise CapsuleError("Capsule tidak berisi pesan")
        if len(raw_messages) > MAX_MESSAGES:
            raise CapsuleError(f"Terlalu banyak pesan (maks {MAX_MESSAGES})")
        messages = [Message.from_dict(item, index) for index, item in enumerate(raw_messages)]
        total = sum(len(message.content) for message in messages)
        if total > MAX_TOTAL_CHARS:
            raise CapsuleError(f"Transkrip terlalu besar ({total} karakter, maks {MAX_TOTAL_CHARS})")
        source = data.get("source")
        if isinstance(source, dict):
            provider = str(source.get("provider") or "")
            model = str(source.get("model") or "")
            source_url = str(source.get("url") or "")
            origin = str(source.get("origin") or "")
        else:
            provider = str(data.get("provider") or "")
            model = str(data.get("model") or "")
            source_url = str(data.get("source_url") or "")
            origin = ""
        title = str(data.get("title") or "").strip() or "Percakapan tanpa judul"
        capsule_id = str(data.get("id") or "").strip() or secrets.token_hex(8)
        system = data.get("system")
        if system is None:
            system = ""
        if not isinstance(system, str):
            raise CapsuleError("Field 'system' harus string")
        note = data.get("note")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise CapsuleError("Field 'note' harus string")
        created = _timestamp(data.get("created_at") or data.get("created_at_iso"))
        return cls(
            id=capsule_id,
            title=title[:200],
            created_at=created,
            provider=provider[:80],
            model=model[:120],
            system=system.replace("\x00", "").strip(),
            messages=messages,
            note=note[:2000],
            source_url=source_url[:2000],
            origin=origin[:40],
            app=str(data.get("app") or "ctxshare"),
        )

    def to_json(self, pretty: bool = False) -> str:
        if pretty:
            return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str, verify_checksum: bool = True) -> "Capsule":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise CapsuleError(f"JSON capsule tidak valid: {exc}") from exc
        return cls.from_dict(data, verify_checksum=verify_checksum)

    # -- codes -------------------------------------------------------------

    def encode_code(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        compressed = zlib.compress(payload, 9)
        encoded = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
        return CODE_PREFIX + encoded

    @classmethod
    def decode_code(cls, code: str) -> "Capsule":
        text = code.strip()
        if not text.startswith(CODE_PREFIX):
            raise CapsuleError("Kode capsule harus diawali 'ctx1.'")
        encoded = text[len(CODE_PREFIX) :]
        if not encoded:
            raise CapsuleError("Kode capsule kosong")
        padding = "=" * (-len(encoded) % 4)
        try:
            compressed = base64.urlsafe_b64decode(encoded + padding)
        except (ValueError, TypeError) as exc:
            raise CapsuleError("Kode capsule bukan base64 yang valid") from exc
        decompressor = zlib.decompressobj()
        try:
            payload = decompressor.decompress(compressed, MAX_CODE_BYTES + 1)
        except zlib.error as exc:
            raise CapsuleError(f"Kode capsule rusak: {exc}") from exc
        if len(payload) > MAX_CODE_BYTES or decompressor.unconsumed_tail:
            raise CapsuleError("Kode capsule terlalu besar atau rusak")
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CapsuleError("Isi kode capsule bukan JSON yang valid") from exc
        return cls.from_dict(data)

    @staticmethod
    def is_code(text: str) -> bool:
        return text.strip().startswith(CODE_PREFIX)

    # -- analysis / transforms --------------------------------------------

    def tokens(self) -> int:
        return estimate_tokens(self.system) + sum(
            estimate_tokens(message.content) for message in self.messages
        )

    def message_count(self) -> int:
        return len(self.messages)

    def last_message(self) -> Optional[Message]:
        return self.messages[-1] if self.messages else None

    def truncated(self, max_chars: int) -> "Capsule":
        """Keep the newest messages so the transcript fits ``max_chars``."""
        if max_chars <= 0:
            return self
        total = len(self.system) + sum(len(message.content) for message in self.messages)
        if total <= max_chars:
            return self
        budget = max(0, max_chars - len(self.system))
        kept: List[Message] = []
        for message in reversed(self.messages):
            cost = len(message.content) + 32
            if kept and cost > budget:
                break
            kept.append(copy.copy(message))
            budget -= cost
        kept.reverse()
        omitted = len(self.messages) - len(kept)
        clone = copy.deepcopy(self)
        notice = (
            f"[{omitted} pesan sebelumnya dipotong oleh ctxshare agar muat di context window]"
        )
        clone.system = (self.system + "\n\n" + notice).strip()
        clone.messages = kept
        return clone

    # -- human-facing renderings -----------------------------------------

    def handoff_prompt(self, target: str = "generic") -> str:
        target_key = target if target in TARGETS else "generic"
        target_info = TARGETS[target_key]
        source = self.provider or "chatbot lain"
        if self.model:
            source += f" ({self.model})"
        target_label = target_info["label"]
        lines = [
            "[HANDOFF KONTEKS — ctxshare]",
            f"Kamu adalah {target_label}, melanjutkan percakapan yang dimulai di {source} "
            f"pada {fmt_time(self.created_at)}.",
            "Transkrip di bawah adalah konteks sebelumnya, bukan permintaan baru.",
            "Gunakan konteks ini secara alami; jangan mengulang atau merangkum seluruh transkrip",
            "kecuali diminta. Balas dalam bahasa yang sama dengan percakapan.",
            "",
        ]
        if self.system:
            lines.extend(["=== INSTRUKSI SISTEM ===", self.system, ""])
        lines.append("=== TRANSKRIP ===")
        for message in self.messages:
            label = {"system": "SYSTEM", "user": "USER", "assistant": "ASSISTANT"}.get(
                message.role, message.role.upper()
            )
            lines.append(f"{label}:")
            lines.append(message.content)
            lines.append("")
        lines.append("=== AKHIR KONTEKS ===")
        lines.append(
            "Jika transkrip berakhir dengan pertanyaan/permintaan user yang belum dijawab, "
            "jawab sekarang."
        )
        lines.append(
            'Jika tidak, balas hanya "Konteks diterima." lalu tunggu pesan saya berikutnya.'
        )
        if self.note:
            lines.extend(["", f"Catatan: {self.note}"])
        return "\n".join(lines).strip() + "\n"

    def to_markdown(self) -> str:
        source = self.provider or "-"
        if self.model:
            source += f" / {self.model}"
        lines = [
            f"# {self.title}",
            "",
            f"> ctxshare capsule · {source} · {fmt_time(self.created_at)} · "
            f"{len(self.messages)} pesan · ~{self.tokens()} token",
        ]
        if self.source_url:
            lines.append(f"> Sumber: {self.source_url}")
        lines.append("")
        if self.note:
            lines.extend([f"_{self.note}_", ""])
        if self.system:
            lines.extend(["## Instruksi sistem", "", "```", self.system, "```", ""])
        for index, message in enumerate(self.messages, 1):
            label = {"system": "System", "user": "User", "assistant": "Assistant"}.get(
                message.role, message.role.title()
            )
            lines.extend([f"## {index}. {label}", "", message.content, ""])
        lines.append("---")
        lines.append("Dibuat dengan [ctxshare](https://github.com/nawa316/ctxshare).")
        return "\n".join(lines)

    def summary(self) -> str:
        source = self.provider or "-"
        if self.model:
            source += f"/{self.model}"
        return f"{self.title} · {len(self.messages)} pesan · ~{self.tokens()} token · {source}"

    def filename_stem(self) -> str:
        from .util import safe_filename

        return safe_filename(self.title, f"ctxshare-{self.id}")


def canonical_bytes(data: Dict[str, Any]) -> bytes:
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def checksum_of(data: Dict[str, Any]) -> str:
    body = {key: value for key, value in data.items() if key != "checksum"}
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def capsule_from_messages(
    title: str,
    messages: List[Message],
    system: str = "",
    provider: str = "",
    model: str = "",
    note: str = "",
    source_url: str = "",
    origin: str = "",
    capsule_id: Optional[str] = None,
) -> Capsule:
    return Capsule(
        id=capsule_id or secrets.token_hex(8),
        title=title or "Percakapan tanpa judul",
        messages=messages,
        system=system,
        provider=provider,
        model=model,
        note=note,
        source_url=source_url,
        origin=origin,
    )


def target_label(target: str) -> str:
    return TARGETS.get(target, TARGETS["generic"])["label"]


def target_window(target: str) -> int:
    return int(TARGETS.get(target, TARGETS["generic"]).get("window") or 0)
