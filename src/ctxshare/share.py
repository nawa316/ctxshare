"""Sharing capsules: secret gists, links, files and offline codes."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import List, Optional

from . import ingest
from .capsule import Capsule
from .errors import CapsuleError, CtxshareError
from .store import Store
from .util import http_get_json, http_get_text

GIST_RE = re.compile(r"gist\.github\.com/(?:[^/]+/)?([0-9a-fA-F]{8,})")
LOCAL_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{12,80}$")
CTXSHARE_LINK_RE = re.compile(r"/c/([A-Za-z0-9_-]{12,80})(?:\.(?:json|txt))?$")


def create_gist(capsule: Capsule, timeout: float = 60.0) -> str:
    """Upload a capsule as a secret gist and return its URL."""
    if not shutil.which("gh"):
        raise CtxshareError(
            "GitHub CLI (`gh`) tidak ditemukan. Pasang dengan `pkg install gh`, "
            "atau bagikan lewat link server / kode ctx1."
        )
    tempdir = tempfile.mkdtemp(prefix="ctxshare-gist-")
    try:
        capsule_path = os.path.join(tempdir, "capsule.json")
        readme_path = os.path.join(tempdir, "README.md")
        with open(capsule_path, "w", encoding="utf-8") as handle:
            handle.write(capsule.to_json(pretty=True))
        with open(readme_path, "w", encoding="utf-8") as handle:
            handle.write(capsule.to_markdown() + "\n")
        description = f"ctxshare: {capsule.title} ({len(capsule.messages)} pesan)"
        command = [
            "gh",
            "gist",
            "create",
            "--desc",
            description[:250],
            "README.md",
            "capsule.json",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=tempdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CtxshareError("Upload gist timeout") from exc
        except OSError as exc:
            raise CtxshareError(f"Gagal menjalankan gh: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:800]
            hint = ""
            if "gist" in detail.lower() and "scope" in detail.lower():
                hint = (
                    "\nToken gh belum punya scope 'gist'. Jalankan:\n"
                    "  gh auth refresh -h github.com -s gist"
                )
            raise CtxshareError(
                "Gagal membuat gist. Pastikan `gh auth login` sudah dilakukan."
                + hint
                + (f"\n{detail}" if detail else "")
            )
        urls = re.findall(r"https://gist\.github\.com/\S+", result.stdout)
        if not urls:
            raise CtxshareError("gh tidak mengembalikan URL gist")
        return urls[-1].strip()
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


def gist_id(url: str) -> Optional[str]:
    match = GIST_RE.search(url)
    return match.group(1) if match else None


def fetch_gist(url: str, timeout: float = 30.0) -> Capsule:
    identifier = gist_id(url)
    if not identifier:
        raise CapsuleError("URL gist tidak valid")
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    api_url = f"https://api.github.com/gists/{identifier}"
    try:
        payload = http_get_json(api_url, timeout=timeout)
    except CtxshareError as exc:
        raise CapsuleError(
            f"Gagal mengambil gist: {exc}\n"
            "Jika kena rate limit GitHub, set GITHUB_TOKEN atau gunakan link .json langsung."
        ) from exc
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict) or not files:
        raise CapsuleError("Gist tidak berisi file")
    candidates = []
    if "capsule.json" in files:
        candidates.append(files["capsule.json"])
    candidates.extend(value for key, value in files.items() if key.lower().endswith(".json"))
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content") or ""
        raw_url = entry.get("raw_url")
        if entry.get("truncated") or not content:
            if raw_url:
                try:
                    content = http_get_text(str(raw_url), timeout=timeout)
                except CtxshareError as exc:
                    raise CapsuleError(f"Gagal mengunduh file gist: {exc}") from exc
        if content:
            return Capsule.from_json(content)
    raise CapsuleError("Gist tidak memiliki file capsule.json")


def _ctshare_json_url(url: str) -> Optional[str]:
    parsed = urllib.parse.urlsplit(url)
    match = CTXSHARE_LINK_RE.search(parsed.path)
    if not match:
        return None
    path = parsed.path
    if not path.endswith(".json"):
        path = path + ".json"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def resolve_input(value: str, store: Optional[Store] = None) -> List[Capsule]:
    """Resolve a link, gist, code, local token, file or pasted transcript."""
    text = (value or "").strip()
    if not text:
        raise CapsuleError("Link/kode/path kosong")

    if Capsule.is_code(text):
        return [Capsule.decode_code(text)]

    candidate = Path(os.path.expanduser(text))
    try:
        if candidate.is_file():
            return ingest.load_file(candidate)
    except OSError as exc:
        raise CapsuleError(f"Gagal membaca file: {exc}") from exc

    if text.startswith("ctxshare://"):
        token = text[len("ctxshare://") :].strip("/")
        return _from_local_token(token, store)

    if "gist.github.com" in text:
        return [fetch_gist(text)]

    if text.startswith("http://") or text.startswith("https://"):
        json_url = _ctshare_json_url(text)
        if json_url:
            return ingest.fetch_source(json_url)
        return ingest.fetch_source(text)

    if LOCAL_TOKEN_RE.match(text) and store is not None:
        return _from_local_token(text, store)

    # A full transcript pasted into the import box.
    if "\n" in text:
        return [ingest.parse_transcript(text)]

    raise CapsuleError(
        "Format tidak dikenal. Gunakan link share ChatGPT/Claude/Gemini, URL gist, "
        "kode ctx1..., path file (.json/.html/.md/.txt), token lokal, atau tempel transkrip."
    )


def _from_local_token(token: str, store: Optional[Store]) -> List[Capsule]:
    if store is None:
        raise CapsuleError("Token lokal membutuhkan database ctxshare")
    record = store.get_share(token)
    if record is not None:
        return [record["capsule"]]
    try:
        return [store.get_capsule(token)]
    except CtxshareError as exc:
        raise CapsuleError(
            "Token lokal tidak ditemukan atau link sudah kedaluwarsa."
        ) from exc


def save_capsules(store: Store, capsules: List[Capsule]) -> List[str]:
    return [store.save_capsule(capsule) for capsule in capsules]


def capsule_to_json(capsule: Capsule) -> str:
    return json.dumps(capsule.to_dict(), ensure_ascii=False, indent=2) + "\n"
