"""Import context from ChatGPT, Claude, Gemini and plain transcripts.

ChatGPT and Claude expose JSON endpoints used by their public share pages.
Gemini uses the same (unofficial) GraphQL query as its web share viewer.
Every network path has a clear paste/export fallback.
"""

from __future__ import annotations

import hashlib
import html as html_module
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .capsule import Capsule, Message, capsule_from_messages
from .errors import CapsuleError, CtxshareError
from .util import now

MAX_FETCH_BYTES = 24 * 1024 * 1024
BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
)
FALLBACK_HINT = (
    "Fallback: buka link itu di browser, salin seluruh percakapan, lalu jalankan "
    "`ctxshare paste` (atau /import → tempel teks) di ctxshare."
)

GEMINI_QUERY_SIG = "2/I6RLabiFhpc6/c/GWsz9d7+SZbxRprpiaZYQsfFTe/0="
GEMINI_GRAPHQL = (
    "https://gemini.google.com/_/BardChatUi/graphql/schemas/"
    "GEMINI_WEB_GRAPHQL/executeQuery?e=share&hl=en"
)
GEMINI_ASSETS = "https://www.gstatic.com/bg3/boq-bard-web/islands/assets/"


# --------------------------------------------------------------------------
# Small HTTP layer
# --------------------------------------------------------------------------


def _http(
    url: str,
    timeout: float = 45.0,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    method: Optional[str] = None,
    max_bytes: int = MAX_FETCH_BYTES,
) -> Tuple[bytes, str, Dict[str, str]]:
    request_headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            content_type = str(response.headers.get("Content-Type") or "")
            response_headers = {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = exc.read(8192).decode("utf-8", "replace").strip()
            detail = _error_detail(body)
        except Exception:
            pass
        suffix = f": {detail}" if detail else ""
        raise CtxshareError(f"HTTP {exc.code} saat mengambil data{suffix}") from exc
    except urllib.error.URLError as exc:
        raise CtxshareError(f"Gagal terhubung: {exc.reason}") from exc
    except socket.timeout as exc:
        raise CtxshareError(f"Waktu habis setelah {timeout} detik") from exc
    if len(raw) > max_bytes:
        raise CtxshareError(f"Respons terlalu besar (>{max_bytes} byte)")
    return raw, content_type, response_headers


def _error_detail(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except ValueError:
        return " ".join(raw.split())[:300]
    if isinstance(payload, dict):
        error = payload.get("error", payload)
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        return str(error)[:300]
    return " ".join(raw.split())[:300]


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# IDs, URLs, labels
# --------------------------------------------------------------------------


def stable_id(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def detect_provider(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc.lower()
    path = urllib.parse.urlsplit(url).path
    if "chatgpt.com" in host or "chat.openai.com" in host:
        return "chatgpt"
    if "claude.ai" in host:
        return "claude"
    if "gemini.google.com" in host or "share.gemini.google" in host or host in ("g.co", "www.g.co"):
        if "gemini" in host or "/gemini/share" in path:
            return "gemini"
        return "gemini" if path.startswith("/gemini/share") else ""
    return ""


def canonical_share_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    provider = detect_provider(url)
    if provider == "chatgpt":
        match = re.search(r"/share/([A-Za-z0-9_-]+)", parsed.path)
        if match:
            return f"https://chatgpt.com/share/{match.group(1)}"
    if provider == "claude":
        match = re.search(r"/share/([0-9a-fA-F-]{8,})", parsed.path)
        if match:
            return f"https://claude.ai/share/{match.group(1)}"
    if provider == "gemini":
        match = re.search(r"/share/([A-Za-z0-9_-]+)", parsed.path)
        if match:
            return f"https://gemini.google.com/share/{match.group(1)}"
        match = re.search(r"/gemini/share/([A-Za-z0-9_-]+)", parsed.path)
        if match:
            return f"https://gemini.google.com/share/{match.group(1)}"
        match = re.search(r"^/([A-Za-z0-9_-]{8,})$", parsed.path)
        if match and "share.gemini.google" in parsed.netloc.lower():
            return f"https://gemini.google.com/share/{match.group(1)}"
    return url.strip()


def _title_from_messages(messages: List[Message], fallback: str = "Percakapan") -> str:
    for message in messages:
        if message.role == "user" and message.content.strip():
            text = " ".join(message.content.split())
            return text[:70]
    return fallback


def _iso_to_ts(value: Any) -> float:
    from datetime import datetime

    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return now()


# --------------------------------------------------------------------------
# ChatGPT
# --------------------------------------------------------------------------


def fetch_chatgpt(url: str, timeout: float = 45.0) -> Capsule:
    canonical = canonical_share_url(url)
    match = re.search(r"/share/([A-Za-z0-9_-]+)", canonical)
    if not match:
        raise CapsuleError("Link ChatGPT share tidak valid")
    share_id = match.group(1)
    api_url = f"https://chatgpt.com/backend-api/share/{share_id}"
    headers = {
        "Accept": "application/json",
        "Referer": canonical,
    }
    data: Any = None
    try:
        raw, _content_type, _headers = _http(api_url, timeout=timeout, headers=headers)
        text = _decode(raw).strip()
        if text:
            data = json.loads(text)
    except (CtxshareError, ValueError):
        data = None
    if isinstance(data, dict) and ("mapping" in data or "linear_conversation" in data):
        return _parse_chatgpt_data(
            data,
            source_url=canonical,
            origin="chatgpt",
            seed=canonical,
        )

    # Fallback: decode the React Router stream embedded in the HTML page.
    try:
        raw, _content_type, _headers = _http(canonical, timeout=timeout)
        html_text = _decode(raw)
        data = _decode_chatgpt_react_router(html_text)
        if isinstance(data, dict):
            return _parse_chatgpt_data(
                data,
                source_url=canonical,
                origin="chatgpt",
                seed=canonical,
            )
    except Exception as exc:
        raise CapsuleError(f"Gagal membaca share ChatGPT: {exc}\n{FALLBACK_HINT}") from exc
    raise CapsuleError(
        "Struktur share ChatGPT tidak dikenali (mungkin format baru).\n" + FALLBACK_HINT
    )


def _parse_chatgpt_data(
    data: Dict[str, Any],
    source_url: str = "",
    origin: str = "chatgpt",
    seed: str = "",
) -> Capsule:
    title = str(data.get("title") or "").strip()
    messages: List[Message] = []
    system_parts: List[str] = []

    linear = data.get("linear_conversation")
    mapping = data.get("mapping")
    nodes: List[Dict[str, Any]] = []
    if isinstance(linear, list) and linear:
        nodes = [node for node in linear if isinstance(node, dict)]
    elif isinstance(mapping, dict):
        by_id = {str(key): value for key, value in mapping.items() if isinstance(value, dict)}
        current = data.get("current_node")
        path: List[str] = []
        seen = set()
        while current and current in by_id and current not in seen:
            seen.add(current)
            path.append(current)
            parent = by_id[current].get("parent")
            current = str(parent) if parent else None
        if path:
            path.reverse()
            nodes = [by_id[node_id] for node_id in path]
        else:
            # Fall back to a depth-first walk from the root.
            children = {
                str(key): [str(child) for child in (value.get("children") or [])]
                for key, value in by_id.items()
            }
            roots = [
                key
                for key, value in by_id.items()
                if not value.get("parent") or str(value.get("parent")) not in by_id
            ]
            stack = list(reversed(roots))
            visited = set()
            while stack:
                node_id = stack.pop()
                if node_id in visited or node_id not in by_id:
                    continue
                visited.add(node_id)
                nodes.append(by_id[node_id])
                stack.extend(reversed(children.get(node_id, [])))

    for node in nodes:
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata") or {}
        if metadata.get("is_visually_hidden_from_conversation"):
            continue
        author = message.get("author") or {}
        role = str(author.get("role") or "").lower()
        content = message.get("content") or {}
        text = _chatgpt_parts_text(content)
        if not text.strip():
            continue
        if role == "system":
            system_parts.append(text.strip())
            continue
        if role == "tool":
            role = "assistant"
            text = "[tool output]\n" + text
        elif role not in ("user", "assistant"):
            role = "assistant"
        messages.append(Message(role=role, content=text.strip(), ts=_number(message.get("create_time"))))

    messages = _dedupe_messages(messages)
    if not messages:
        raise CapsuleError("Share ChatGPT tidak berisi pesan teks.\n" + FALLBACK_HINT)
    if not title:
        title = _title_from_messages(messages, "ChatGPT conversation")
    capsule_id = stable_id(seed or source_url or f"chatgpt:{title}")
    return capsule_from_messages(
        title=title,
        messages=messages,
        system="\n\n".join(system_parts).strip(),
        provider="ChatGPT",
        model=str(data.get("default_model_slug") or "ChatGPT")[:120],
        source_url=source_url,
        origin=origin,
        capsule_id=capsule_id,
    )


def _chatgpt_parts_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        if isinstance(content.get("text"), str):
            return str(content["text"])
        return ""
    chunks: List[str] = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict):
            if isinstance(part.get("text"), str):
                chunks.append(str(part["text"]))
            elif part.get("asset_pointer") or part.get("image_url") or part.get("url"):
                chunks.append("[lampiran media]")
    return "\n".join(chunk for chunk in chunks if chunk)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _dedupe_messages(messages: List[Message]) -> List[Message]:
    result: List[Message] = []
    for message in messages:
        content = message.content.strip()
        if not content:
            continue
        if result and result[-1].role == message.role and result[-1].content.strip() == content:
            continue
        result.append(Message(message.role, content, message.ts))
    return result


class _StreamRefDecoder:
    """Decode the flat reference array used by ChatGPT's React Router payload."""

    def __init__(self, values: List[Any]):
        self.values = values
        self.memo: Dict[int, Any] = {}
        self.active: set = set()

    def resolve(self, index: int) -> Any:
        if index < 0:
            return None
        if index >= len(self.values):
            return None
        if index in self.memo:
            return self.memo[index]
        if index in self.active:
            return None
        self.active.add(index)
        value = self.values[index]
        result = self._resolve_value(value)
        self.active.discard(index)
        self.memo[index] = result
        return result

    def _resolve_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            result: Dict[Any, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and key.startswith("_") and key[1:].isdigit():
                    resolved_key = self.resolve(int(key[1:]))
                else:
                    resolved_key = key
                if isinstance(item, int) and not isinstance(item, bool):
                    resolved_value = self.resolve(item)
                else:
                    resolved_value = self._resolve_value(item)
                try:
                    result[str(resolved_key)] = resolved_value
                except Exception:
                    continue
            return result
        if isinstance(value, list):
            return [self._resolve_value(item) if not isinstance(item, int) else self.resolve(item) for item in value]
        return value


def _find_key(value: Any, wanted: str, depth: int = 0) -> Any:
    if depth > 12:
        return None
    if isinstance(value, dict):
        if wanted in value:
            return value
        for item in value.values():
            found = _find_key(item, wanted, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key(item, wanted, depth + 1)
            if found is not None:
                return found
    return None


def _decode_chatgpt_react_router(html_text: str) -> Optional[Dict[str, Any]]:
    match = re.search(
        r'window\.__reactRouterContext\.streamController\.enqueue\(("(?:[^"\\]|\\.)*")\)',
        html_text,
    )
    if not match:
        return None
    payload = json.loads(match.group(1))
    values = json.loads(payload)
    if not isinstance(values, list):
        return None
    root = _StreamRefDecoder(values).resolve(0)
    container = _find_key(root, "linear_conversation") or _find_key(root, "mapping")
    if not isinstance(container, dict):
        return None
    title = container.get("title") or _find_key(root, "pageTitle") or ""
    return {
        "title": title,
        "create_time": container.get("create_time"),
        "mapping": container.get("mapping"),
        "linear_conversation": container.get("linear_conversation"),
        "current_node": container.get("current_node"),
        "default_model_slug": container.get("default_model_slug"),
    }


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------


def fetch_claude(url: str, timeout: float = 45.0) -> Capsule:
    canonical = canonical_share_url(url)
    match = re.search(r"/share/([0-9a-fA-F-]{8,})", canonical)
    if not match:
        raise CapsuleError("Link Claude share tidak valid")
    share_id = match.group(1)
    api_url = (
        f"https://claude.ai/api/chat_snapshots/{share_id}"
        "?rendering_mode=messages&render_all_tools=true"
    )
    headers = {
        "Accept": "application/json",
        "Referer": canonical,
        "Origin": "https://claude.ai",
        "anthropic-client-platform": "web_claude_ai",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    try:
        raw, _content_type, _headers = _http(api_url, timeout=timeout, headers=headers)
        data = json.loads(_decode(raw))
    except (CtxshareError, ValueError) as exc:
        raise CapsuleError(f"Gagal mengambil share Claude: {exc}\n{FALLBACK_HINT}") from exc
    if not isinstance(data, dict):
        raise CapsuleError("Respons Claude bukan objek JSON.\n" + FALLBACK_HINT)
    return _parse_claude_snapshot(data, source_url=canonical, origin="claude")


def _parse_claude_snapshot(
    data: Dict[str, Any], source_url: str = "", origin: str = "claude"
) -> Capsule:
    raw_messages = data.get("chat_messages")
    if not isinstance(raw_messages, list):
        raise CapsuleError("Share Claude tidak memiliki chat_messages.\n" + FALLBACK_HINT)
    ordered = sorted(
        [item for item in raw_messages if isinstance(item, dict)],
        key=lambda item: _number(item.get("index"), 0),
    )
    messages: List[Message] = []
    for item in ordered:
        sender = str(item.get("sender") or "").lower()
        role = "user" if sender in ("human", "user") else "assistant"
        text = _claude_message_text(item)
        if text.strip():
            messages.append(Message(role=role, content=text.strip(), ts=_iso_to_ts(item.get("created_at"))))
    messages = _dedupe_messages(messages)
    if not messages:
        raise CapsuleError("Share Claude tidak berisi pesan teks.\n" + FALLBACK_HINT)
    title = str(data.get("snapshot_name") or data.get("name") or "").strip() or _title_from_messages(
        messages, "Claude conversation"
    )
    provider_model = str(data.get("model") or "").strip()
    return capsule_from_messages(
        title=title,
        messages=messages,
        provider="Claude",
        model=provider_model[:120],
        source_url=source_url,
        origin=origin,
        capsule_id=stable_id(source_url or f"claude:{data.get('uuid') or title}"),
        **{"note": ""},
    )


def _claude_message_text(item: Dict[str, Any]) -> str:
    chunks: List[str] = []
    content = item.get("content")
    if isinstance(content, str) and content.strip():
        chunks.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text)
                elif part.get("type") in ("tool_use", "tool_result"):
                    nested = part.get("content")
                    if isinstance(nested, str):
                        chunks.append(nested)
    if not chunks:
        text = item.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------


def fetch_gemini(url: str, timeout: float = 45.0) -> Capsule:
    canonical = canonical_share_url(url)
    match = re.search(r"/share/([A-Za-z0-9_-]+)", canonical)
    if not match:
        raise CapsuleError("Link Gemini share tidak valid")
    share_id = match.group(1)
    signature = GEMINI_QUERY_SIG
    data, errors = _gemini_query(share_id, signature, canonical, timeout)
    if data is None:
        refreshed = _discover_gemini_signature(share_id, timeout)
        if refreshed and refreshed != signature:
            signature = refreshed
            data, errors = _gemini_query(share_id, signature, canonical, timeout)
    if data is None:
        detail = _error_detail(json.dumps(errors)) if errors else "tidak ada data"
        raise CapsuleError(
            f"Gagal mengambil share Gemini: {detail}\n{FALLBACK_HINT}"
        )
    return _parse_gemini_shared(data, source_url=canonical, origin="gemini")


def _gemini_query(
    share_id: str, signature: str, referer: str, timeout: float
) -> Tuple[Optional[Dict[str, Any]], Optional[List[Any]]]:
    body = json.dumps(
        {
            "querySignature": signature,
            "variables": {"externalUrlId": share_id},
        }
    ).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://gemini.google.com",
        "Referer": referer,
        "X-Goog-AuthUser": "0",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    try:
        raw, _content_type, _headers = _http(
            GEMINI_GRAPHQL, timeout=timeout, headers=headers, data=body
        )
        text = _decode(raw).strip()
        payload = json.loads(text)
    except (CtxshareError, ValueError):
        return None, None
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return None, None
    errors: List[Any] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if entry.get("errors"):
            errors.extend(entry["errors"])
        rpcs = (((entry.get("data") or {}).get("viewer") or {}).get("rpcs") or {})
        shared = rpcs.get("getSharedConversation")
        if isinstance(shared, dict) and isinstance(shared.get("sharedConversation"), dict):
            return shared["sharedConversation"], errors or None
    return None, errors or None


def _discover_gemini_signature(share_id: str, timeout: float) -> Optional[str]:
    try:
        raw, _content_type, _headers = _http(
            f"https://gemini.google.com/share/{share_id}", timeout=timeout
        )
        page = _decode(raw)
        islands = re.search(
            r'https://www\.gstatic\.com/bg3/boq-bard-web/islands/assets/islands-[^"]+\.js',
            page,
        )
        if not islands:
            return None
        islands_raw, _content_type, _headers = _http(islands.group(0), timeout=timeout)
        islands_js = _decode(islands_raw)
        viewer = re.search(r"shareViewer-([A-Za-z0-9_-]+)\.js", islands_js)
        if not viewer:
            return None
        viewer_raw, _content_type, _headers = _http(
            GEMINI_ASSETS + viewer.group(0), timeout=timeout
        )
        viewer_js = _decode(viewer_raw)
        match = re.search(r"id:\s*`(2/[^`]+)`", viewer_js) or re.search(
            r'id:\s*"(2/[^"]+)"', viewer_js
        )
        return match.group(1) if match else None
    except Exception:
        return None


def _parse_gemini_shared(
    shared: Dict[str, Any], source_url: str = "", origin: str = "gemini"
) -> Capsule:
    content = shared.get("content") or {}
    kind = str(content.get("content") or "")
    turns: List[Dict[str, Any]] = []
    if kind == "conversation" and isinstance(content.get("conversation"), dict):
        turns = [
            turn
            for turn in (content["conversation"].get("turns") or [])
            if isinstance(turn, dict)
        ]
    elif isinstance(shared.get("conversationTurns"), list):
        turns = [turn for turn in shared["conversationTurns"] if isinstance(turn, dict)]
    elif kind == "singleResponse":
        single = content.get("singleResponse") or {}
        turns = [single] if isinstance(single, dict) else []
    if not turns:
        raise CapsuleError(
            "Share Gemini ini tidak berisi percakapan teks "
            f"(content: {kind or 'kosong'}).\n{FALLBACK_HINT}"
        )

    messages: List[Message] = []
    for turn in turns:
        request = turn.get("request") or {}
        query = request.get("query") or {}
        user_text = query.get("text")
        if isinstance(user_text, str) and user_text.strip():
            messages.append(
                Message("user", user_text.strip(), ts=_iso_to_ts(turn.get("timestamp")))
            )
        response = turn.get("response") or {}
        parsed = response.get("selectedParsedResponse")
        if isinstance(parsed, dict):
            assistant_text = _gemini_ast_text(parsed).strip()
        else:
            assistant_text = ""
        if assistant_text:
            messages.append(
                Message(
                    "assistant",
                    assistant_text,
                    ts=_iso_to_ts(turn.get("timestamp")),
                )
            )
    messages = _dedupe_messages(messages)
    if not messages:
        raise CapsuleError("Share Gemini tidak berisi pesan teks.\n" + FALLBACK_HINT)

    meta = shared.get("landingPageMetadata") or {}
    title = str(meta.get("title") or "").strip() or _title_from_messages(
        messages, "Gemini conversation"
    )
    mode = ((meta.get("conversationLabelInfo") or {}).get("modeTitle") or "").strip()
    model = f"Gemini {mode}".strip() if mode else "Gemini"
    return capsule_from_messages(
        title=title,
        messages=messages,
        provider="Gemini",
        model=model[:120],
        source_url=source_url,
        origin=origin,
        capsule_id=stable_id(source_url or f"gemini:{shared.get('externalUrlId') or title}"),
    )


def _gemini_ast_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_gemini_ast_text(item) for item in node)
    if not isinstance(node, dict):
        return ""
    typename = str(node.get("__typename") or "")
    if "InlineCitations" in typename or "Citation" in typename:
        return ""
    if typename.endswith("Text") or typename.endswith("Heading"):
        text = str(node.get("text") or "")
        level = _number(node.get("headingLevel"), 0)
        if level and text:
            return "\n" + "#" * min(int(level), 6) + " " + text + "\n"
        return text
    if typename.endswith("Code"):
        code = str(node.get("code") or "")
        info = str(node.get("info") or "")
        return f"\n```{info}\n{code}\n```\n"
    if typename.endswith("Table"):
        return _gemini_table(node)
    if typename.endswith("UnorderedList") or typename.endswith("OrderedList"):
        return _gemini_list(node)
    chunks: List[str] = []
    for key in ("constrainedChildren", "children", "content", "items", "parts", "rows"):
        if key in node:
            chunks.append(_gemini_ast_text(node[key]))
    if chunks:
        return _join_ast_chunks(chunks)
    text = node.get("text")
    return str(text) if isinstance(text, str) else ""


def _join_ast_chunks(chunks: List[str]) -> str:
    result = ""
    for chunk in chunks:
        if not chunk:
            continue
        if (
            result
            and re.search(r"[.!?:]\s*$", result)
            and re.match(r"^[A-Z0-9#]", chunk)
        ):
            result += "\n"
        result += chunk
    return result


def _gemini_list(node: Dict[str, Any]) -> str:
    entries = node.get("flatEntries") or []
    ordered = str(node.get("__typename") or "").endswith("OrderedList")
    counter = 0
    lines: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "LEAF":
            continue
        leaf = entry.get("leaf")
        text = _gemini_ast_text(leaf).strip()
        if not text:
            continue
        depth = max(0, int(_number(entry.get("depth"), 2)) - 2)
        if ordered:
            counter += 1
            prefix = f"{counter}. "
        else:
            prefix = "- "
        lines.append("  " * depth + prefix + text.replace("\n", "\n" + "  " * (depth + 1)))
    return "\n" + "\n".join(lines) + ("\n" if lines else "")


def _gemini_table(node: Dict[str, Any]) -> str:
    rows = node.get("rows") or []
    lines: List[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        cells: List[str] = []
        for cell in row.get("tableCells") or []:
            if isinstance(cell, dict):
                text = _gemini_ast_text(cell.get("constrainedChildren") or cell).strip()
                cells.append(" ".join(text.split()))
        if cells:
            lines.append("| " + " | ".join(cells) + " |")
            if index == 0:
                lines.append("| " + " | ".join("---" for _ in cells) + " |")
    return "\n" + "\n".join(lines) + ("\n" if lines else "")


# --------------------------------------------------------------------------
# Plain transcript parsing
# --------------------------------------------------------------------------

ROLE_WORDS = (
    "you|user|human|me|assistant|ai|chatgpt|claude|gemini|model|system|system prompt"
)
HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:[-*]\s*)?(?:\*\*|__)?\s*"
    r"(?:\d+[.)]\s*)?"
    rf"({ROLE_WORDS})"
    r"\s*(?:\*\*|__)?\s*(?:\([^)]*\))?\s*"
    r"(?:said|says|responded|responds|replied|wrote)?\s*:?\s*$",
    re.IGNORECASE,
)
INLINE_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*"
    r"(?:\d+[.)]\s*)?"
    rf"({ROLE_WORDS})"
    r"\s*(?:\*\*|__)?\s*(?:\([^)]*\))?\s*"
    r"(?:said|says|responded|responds|replied|wrote)?\s*:\s*(.+)$",
    re.IGNORECASE,
)


def _role_for(label: str) -> str:
    word = label.strip().lower()
    if word.startswith("system"):
        return "system"
    if word in ("you", "user", "human", "me"):
        return "user"
    return "assistant"


def _provider_for_labels(labels: Iterable[str]) -> str:
    lowered = " ".join(label.lower() for label in labels)
    if "chatgpt" in lowered:
        return "ChatGPT"
    if "claude" in lowered:
        return "Claude"
    if "gemini" in lowered:
        return "Gemini"
    return "Tempelan"


def parse_transcript(
    text: str,
    title: str = "",
    source_url: str = "",
    origin: str = "paste",
) -> Capsule:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise CapsuleError("Teks kosong")
    lines = normalized.split("\n")
    messages: List[Message] = []
    labels: List[str] = []
    current_role: Optional[str] = None
    current_label = ""
    buffer: List[str] = []
    saw_header = False
    preamble: List[str] = []

    def flush() -> None:
        nonlocal buffer, current_role
        if current_role is None:
            return
        content = "\n".join(buffer).strip()
        if content:
            messages.append(Message(current_role, content))
        buffer = []

    for line in lines:
        header = HEADER_RE.match(line)
        if header:
            flush()
            current_role = _role_for(header.group(1))
            current_label = header.group(1).strip()
            labels.append(current_label)
            saw_header = True
            continue
        inline = INLINE_RE.match(line)
        if inline:
            flush()
            current_role = _role_for(inline.group(1))
            current_label = inline.group(1).strip()
            labels.append(current_label)
            saw_header = True
            buffer = [inline.group(2)]
            continue
        if current_role is None:
            preamble.append(line)
        else:
            buffer.append(line)
    flush()

    if not saw_header:
        # No role markers: keep the whole text as one user message, but pull a
        # markdown heading out as the title when possible.
        content_lines = [line for line in lines if line.strip()]
        inferred_title = title
        if content_lines and content_lines[0].startswith("# "):
            inferred_title = inferred_title or content_lines[0][2:].strip()
            content_lines = content_lines[1:]
        content = "\n".join(content_lines).strip()
        if not content:
            raise CapsuleError("Tidak ada teks percakapan")
        capsule = capsule_from_messages(
            title=inferred_title or content.strip().splitlines()[0][:70],
            messages=[Message("user", content)],
            provider="Tempelan",
            source_url=source_url,
            origin=origin,
            note="Label peran tidak terdeteksi; seluruh teks disimpan sebagai satu pesan user.",
        )
        return capsule

    if preamble and preamble[0].strip().startswith("# ") and not title:
        title = preamble[0].strip()[2:].strip()
        preamble = preamble[1:]
    preamble_text = "\n".join(preamble).strip()
    if preamble_text:
        messages.insert(0, Message("user", preamble_text))

    # Merge consecutive messages with the same role.
    merged: List[Message] = []
    for message in messages:
        if merged and merged[-1].role == message.role:
            merged[-1] = Message(
                message.role,
                merged[-1].content.rstrip() + "\n\n" + message.content.lstrip(),
                merged[-1].ts,
            )
        else:
            merged.append(message)
    messages = [message for message in merged if message.content.strip()]
    if not messages:
        raise CapsuleError("Tidak ada pesan yang bisa diimpor")
    system_chunks = [message.content for message in messages if message.role == "system"]
    messages = [message for message in messages if message.role != "system"]
    if not messages:
        raise CapsuleError("Transkrip hanya berisi system prompt")

    provider = _provider_for_labels(labels)
    inferred_title = title
    if not inferred_title:
        inferred_title = _title_from_messages(messages, "Tempelan")
    return capsule_from_messages(
        title=inferred_title[:200],
        messages=messages,
        system="\n\n".join(system_chunks).strip(),
        provider=provider,
        source_url=source_url,
        origin=origin,
        capsule_id=stable_id(source_url) if source_url else None,
    )


# --------------------------------------------------------------------------
# Saved HTML / exports
# --------------------------------------------------------------------------


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional["_Node"] = None):
        self.tag = tag
        self.attrs = attrs
        self.children: List[Any] = []
        self.parent = parent


VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "button",
    "form",
    "head",
    "title",
}
BLOCK_TAGS = {
    "p",
    "div",
    "li",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "pre",
    "blockquote",
    "tr",
    "table",
    "section",
    "article",
    "header",
    "footer",
    "br",
}


class _TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self.stack: List[_Node] = [self.root]
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        node = _Node(
            tag,
            {key.lower(): (value or "") for key, value in attrs},
            self.stack[-1],
        )
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            return
        node = _Node(
            tag,
            {key.lower(): (value or "") for key, value in attrs},
            self.stack[-1],
        )
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag in VOID_TAGS:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if data:
            self.stack[-1].children.append(data)


def _node_text(node: _Node, skip_targets: bool = True) -> str:
    chunks: List[str] = []

    def walk(current: _Node, inside_target: bool = False) -> None:
        for child in current.children:
            if isinstance(child, str):
                chunks.append(child)
                continue
            tag = child.tag
            if tag in SKIP_TAGS:
                continue
            target = _html_role(child) is not None
            if target and skip_targets and inside_target:
                continue
            if target and skip_targets and not inside_target:
                walk(child, True)
                chunks.append("\n")
                continue
            if tag == "br":
                chunks.append("\n")
                continue
            if tag == "li":
                chunks.append("\n- ")
                walk(child, inside_target)
                chunks.append("\n")
                continue
            if tag in BLOCK_TAGS:
                chunks.append("\n")
                walk(child, inside_target)
                chunks.append("\n")
                continue
            walk(child, inside_target)

    walk(node)
    text = "".join(chunks)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _html_role(node: _Node) -> Optional[str]:
    role_attr = node.attrs.get("data-message-author-role")
    if role_attr:
        return "user" if role_attr in ("user", "human") else "assistant"
    testid = (node.attrs.get("data-testid") or "").lower()
    if testid in ("user-message", "user-query"):
        return "user"
    if testid in ("assistant-message", "model-response"):
        return "assistant"
    if "user-query" in testid:
        return "user"
    if "model-response" in testid:
        return "assistant"
    if node.tag in ("user-query", "model-response"):
        return "user" if node.tag == "user-query" else "assistant"
    classes = (node.attrs.get("class") or "").lower()
    if "font-claude-message" in classes:
        return "assistant"
    return None


def parse_html(
    html_text: str,
    source_url: str = "",
    title: str = "",
    origin: str = "file",
    provider_hint: str = "",
) -> Capsule:
    if "__reactRouterContext" in html_text:
        try:
            data = _decode_chatgpt_react_router(html_text)
            if isinstance(data, dict):
                capsule = _parse_chatgpt_data(
                    data,
                    source_url=source_url,
                    origin="chatgpt",
                    seed=source_url or f"html:{title}",
                )
                if title:
                    capsule.title = title
                return capsule
        except Exception:
            pass
    parser = _TreeParser()
    try:
        parser.feed(html_text)
    except Exception as exc:
        raise CapsuleError(f"HTML tidak bisa dibaca: {exc}") from exc
    root = parser.root
    collected: List[Tuple[str, str]] = []

    def walk(node: _Node) -> None:
        role = _html_role(node)
        if role is not None:
            text = _node_text(node, skip_targets=True)
            if text.strip():
                collected.append((role, text.strip()))
            return
        for child in node.children:
            if isinstance(child, _Node):
                walk(child)

    walk(root)
    provider = provider_hint
    if not provider:
        if _find_node(root, lambda n: bool(n.attrs.get("data-message-author-role"))) is not None:
            provider = "chatgpt"
        elif any("chatgpt" in (node.attrs.get("data-testid") or "") for node in _iter_nodes(root)):
            provider = "chatgpt"
        elif _find_node(root, lambda n: "font-claude-message" in (n.attrs.get("class") or "")) is not None:
            provider = "claude"
        elif _find_node(root, lambda n: n.tag in ("user-query", "model-response")) is not None:
            provider = "gemini"
    provider = provider or "generic"
    messages = [Message("user" if role == "user" else "assistant", text) for role, text in collected]
    messages = _dedupe_messages(messages)

    if not messages:
        plain = _node_text(root, skip_targets=False)
        if not plain.strip():
            raise CapsuleError("HTML tidak berisi teks percakapan")
        return parse_transcript(
            plain,
            title=title,
            source_url=source_url,
            origin=origin,
        )

    page_title = title or _html_title(html_text)
    label = {"chatgpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini"}.get(
        provider, "Tempelan"
    )
    return capsule_from_messages(
        title=(title or page_title or _title_from_messages(messages, label))[:200],
        messages=messages,
        provider=label,
        source_url=source_url,
        origin=f"{provider}-html" if provider in ("chatgpt", "claude", "gemini") else origin,
        capsule_id=stable_id(source_url) if source_url else None,
    )


def _iter_nodes(root: _Node):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        for child in reversed(node.children):
            if isinstance(child, _Node):
                stack.append(child)


def _find_node(root: _Node, predicate) -> Optional[_Node]:
    for node in _iter_nodes(root):
        if predicate(node):
            return node
    return None


def _html_title(html_text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    text = html_module.unescape(match.group(1)).strip()
    for prefix in ("ChatGPT - ", "Claude - ", "Gemini - ", "Google Gemini - "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.strip()


# --------------------------------------------------------------------------
# JSON exports
# --------------------------------------------------------------------------


def capsules_from_json(
    data: Any, source_name: str = "", origin: str = "export"
) -> List[Capsule]:
    if isinstance(data, dict) and str(data.get("format") or "").startswith("ctxshare.capsule/"):
        return [Capsule.from_dict(data)]
    if isinstance(data, dict) and isinstance(data.get("conversations"), list):
        return capsules_from_json(data["conversations"], source_name=source_name, origin=origin)
    if isinstance(data, list):
        capsules: List[Capsule] = []
        if data and all(isinstance(item, dict) for item in data):
            if any("mapping" in item or "linear_conversation" in item for item in data):
                for index, item in enumerate(data):
                    try:
                        seed = str(
                            item.get("conversation_id")
                            or item.get("id")
                            or item.get("create_time")
                            or index
                        )
                        capsules.append(
                            _parse_chatgpt_data(
                                item,
                                origin=f"{origin}-chatgpt",
                                seed=f"{source_name}:{seed}",
                            )
                        )
                    except CapsuleError:
                        continue
                if capsules:
                    return capsules
            if any("chat_messages" in item for item in data):
                for index, item in enumerate(data):
                    try:
                        seed = str(item.get("uuid") or item.get("name") or index)
                        capsule = _parse_claude_snapshot(
                            item, origin=f"{origin}-claude"
                        )
                        capsule.id = stable_id(f"{source_name}:{seed}")
                        capsules.append(capsule)
                    except CapsuleError:
                        continue
                if capsules:
                    return capsules
            if all("role" in item and "content" in item for item in data):
                messages = []
                for item in data:
                    role = str(item.get("role") or "user").lower()
                    if role not in ("system", "user", "assistant"):
                        role = "user"
                    content = item.get("content")
                    if isinstance(content, list):
                        content = "\n".join(
                            str(part.get("text") or "") if isinstance(part, dict) else str(part)
                            for part in content
                        )
                    if content:
                        messages.append(Message(role, str(content)))
                if messages:
                    return [
                        capsule_from_messages(
                            title=_title_from_messages(messages, source_name or "Export"),
                            messages=messages,
                            provider="Tempelan",
                            origin=origin,
                        )
                    ]
        raise CapsuleError("Format JSON export tidak dikenali")

    if not isinstance(data, dict):
        raise CapsuleError("JSON bukan objek percakapan")
    if "mapping" in data or "linear_conversation" in data:
        return [
            _parse_chatgpt_data(
                data,
                origin=f"{origin}-chatgpt",
                seed=f"{source_name}:{data.get('conversation_id') or data.get('title')}",
            )
        ]
    if "chat_messages" in data:
        capsule = _parse_claude_snapshot(data, origin=f"{origin}-claude")
        capsule.id = stable_id(f"{source_name}:{data.get('uuid') or data.get('snapshot_name')}")
        return [capsule]
    messages = data.get("messages") or data.get("conversation")
    if isinstance(messages, list):
        parsed: List[Message] = []
        for item in messages:
            if isinstance(item, dict):
                role = str(item.get("role") or item.get("sender") or "user").lower()
                if role in ("human",):
                    role = "user"
                if role in ("ai", "bot"):
                    role = "assistant"
                if role not in ("system", "user", "assistant"):
                    role = "user"
                content = item.get("content") or item.get("text") or ""
                if isinstance(content, list):
                    content = "\n".join(
                        str(part.get("text") or "") if isinstance(part, dict) else str(part)
                        for part in content
                    )
                if content:
                    parsed.append(Message(role, str(content)))
        if parsed:
            return [
                capsule_from_messages(
                    title=str(data.get("title") or data.get("name") or _title_from_messages(parsed, "Export")),
                    messages=parsed,
                    provider="Tempelan",
                    origin=origin,
                )
            ]
    raise CapsuleError("Format JSON export tidak dikenali")


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


def fetch_source(url: str, timeout: float = 45.0) -> List[Capsule]:
    canonical = canonical_share_url(url)
    provider = detect_provider(canonical)
    if provider == "chatgpt":
        return [fetch_chatgpt(canonical, timeout=timeout)]
    if provider == "claude":
        return [fetch_claude(canonical, timeout=timeout)]
    if provider == "gemini":
        return [fetch_gemini(canonical, timeout=timeout)]

    raw, content_type, _headers = _http(canonical, timeout=timeout)
    text = _decode(raw)
    stripped = text.lstrip()
    if "json" in content_type.lower() or stripped.startswith(("{", "[")):
        try:
            return capsules_from_json(json.loads(text), source_name=canonical, origin="url")
        except ValueError as exc:
            raise CapsuleError(f"Respons bukan JSON yang valid: {exc}") from exc
    if "html" in content_type.lower() or stripped.startswith("<"):
        return [
            parse_html(
                text,
                source_url=canonical,
                origin="url",
            )
        ]
    return [
        parse_transcript(
            text,
            source_url=canonical,
            origin="url",
        )
    ]


def load_file(path: Path) -> List[Capsule]:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CtxshareError(f"Gagal membaca {path}: {exc}") from exc
    if len(raw) > MAX_FETCH_BYTES:
        raise CapsuleError(f"File terlalu besar (>{MAX_FETCH_BYTES} byte)")
    text = raw.decode("utf-8", "replace")
    stripped = text.lstrip()
    suffix = path.suffix.lower()
    if suffix == ".json" or stripped.startswith(("{", "[")):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise CapsuleError(f"JSON tidak valid: {exc}") from exc
        return capsules_from_json(data, source_name=path.name, origin="file")
    if suffix in (".html", ".htm") or stripped.startswith("<!doctype") or stripped.startswith("<html"):
        return [parse_html(text, source_url="", origin="file")]
    if Capsule.is_code(stripped):
        return [Capsule.decode_code(stripped)]
    return [parse_transcript(text, origin="file")]


def source_label(value: str) -> str:
    return {
        "chatgpt": "ChatGPT",
        "claude": "Claude",
        "gemini": "Gemini",
        "paste": "Tempelan",
        "file": "File",
        "url": "URL",
    }.get(value, value or "-")
