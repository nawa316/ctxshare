"""Small helpers: paths, HTTP, clipboard, formatting."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from . import USER_AGENT
from .errors import CtxshareError

MAX_HTTP_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def home_dir() -> Path:
    return Path(os.path.expanduser("~"))


def data_dir() -> Path:
    override = os.environ.get("CTXSHARE_DATA")
    path = Path(override).expanduser() if override else home_dir() / ".local" / "share" / "ctxshare"
    ensure_dir(path)
    return path


def config_dir() -> Path:
    override = os.environ.get("CTXSHARE_CONFIG_DIR")
    path = Path(override).expanduser() if override else home_dir() / ".config" / "ctxshare"
    ensure_dir(path)
    return path


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def db_path() -> Path:
    return data_dir() / "ctxshare.db"


def log_path() -> Path:
    return data_dir() / "ctxshare.log"


def runtime_path() -> Path:
    return data_dir() / "server.json"


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json_file(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise CtxshareError(f"Gagal membaca {path}: {exc}") from exc


# --------------------------------------------------------------------------
# IDs and formatting
# --------------------------------------------------------------------------


def short_id(nbytes: int = 6) -> str:
    return secrets.token_hex(nbytes)


def share_token() -> str:
    return secrets.token_urlsafe(18)


def now() -> float:
    return time.time()


def fmt_time(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return "-"


def fmt_relative(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    delta = max(0, int(now() - float(ts)))
    if delta < 60:
        return "baru saja"
    if delta < 3600:
        return f"{delta // 60} mnt lalu"
    if delta < 86400:
        return f"{delta // 3600} jam lalu"
    if delta < 86400 * 30:
        return f"{delta // 86400} hari lalu"
    return fmt_time(ts)


def fmt_tokens(count: float) -> str:
    count = float(count)
    if count < 1000:
        return str(int(count))
    if count < 1_000_000:
        value = count / 1000.0
        return f"{value:.1f}k".replace(".0k", "k")
    value = count / 1_000_000.0
    return f"{value:.1f}M".replace(".0M", "M")


def estimate_tokens(text: str) -> int:
    """Cheap cross-provider estimate (~4 characters per token)."""
    if not text:
        return 0
    return max(1, round(len(text) / 4.0))


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 5:
        return text[:limit]
    head = (limit - 3) // 2
    tail = limit - 3 - head
    return text[:head] + "..." + text[-tail:]


def safe_filename(value: str, fallback: str = "ctxshare") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return (slug or fallback)[:60]


def unique_suffix() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------


def lan_ip() -> str:
    """Best-effort LAN address; falls back to 127.0.0.1 when offline."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127."):
                return str(address)
    except OSError:
        pass
    return "127.0.0.1"


def http_get(
    url: str,
    timeout: float = 30.0,
    max_bytes: int = MAX_HTTP_BYTES,
    headers: Optional[Dict[str, str]] = None,
) -> bytes:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            raw = exc.read(2048).decode("utf-8", "replace").strip()
            detail = _error_detail(raw)
        except Exception:  # pragma: no cover - defensive
            pass
        raise CtxshareError(f"HTTP {exc.code} untuk {url}" + (f": {detail}" if detail else "")) from exc
    except urllib.error.URLError as exc:
        raise CtxshareError(f"Gagal menghubungi {url}: {exc.reason}") from exc
    except socket.timeout as exc:  # pragma: no cover - platform dependent
        raise CtxshareError(f"Waktu habis saat menghubungi {url}") from exc
    if len(data) > max_bytes:
        raise CtxshareError(f"Respons dari {url} terlalu besar (>{max_bytes} byte)")
    return data


def _error_detail(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw[:300]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        if error:
            return str(error)[:300]
        if payload.get("message"):
            return str(payload["message"])[:300]
    return raw[:300]


def http_get_text(url: str, timeout: float = 30.0) -> str:
    return http_get(url, timeout=timeout).decode("utf-8", "replace")


def http_get_json(url: str, timeout: float = 30.0) -> Any:
    raw = http_get(url, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CtxshareError(f"Respons dari {url} bukan JSON yang valid") from exc


# --------------------------------------------------------------------------
# Clipboard / environment
# --------------------------------------------------------------------------


def copy_to_clipboard(text: str) -> str:
    """Copy text and return the mechanism used.

    Tries native helpers first, then OSC 52 (works in Termux and most modern
    terminal emulators, including over SSH).
    """
    candidates = []
    if shutil.which("termux-clipboard-set"):
        candidates.append(["termux-clipboard-set"])
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        candidates.append(["wl-copy"])
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        candidates.append(["xclip", "-selection", "clipboard"])
    if shutil.which("pbcopy"):
        candidates.append(["pbcopy"])
    for command in candidates:
        try:
            subprocess.run(
                command,
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=True,
            )
            return command[0]
        except (OSError, subprocess.SubprocessError):
            continue
    try:
        stream = sys.__stdout__ or sys.stdout
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        if len(encoded) <= 100_000:
            stream.write(f"\033]52;c;{encoded}\a")
            stream.flush()
            return "OSC 52"
    except (OSError, ValueError, AttributeError):
        pass
    raise CtxshareError("Clipboard tidak tersedia; salin teks secara manual")


def clipboard_text() -> str:
    """Read text from the system clipboard using common Termux/desktop tools."""
    commands = [
        ["termux-clipboard-get"],
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["pbpaste"],
    ]
    for command in commands:
        if not shutil.which(command[0]):
            continue
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=6,
                check=True,
            )
            text = result.stdout.decode("utf-8", "replace")
            if text.strip():
                return text
        except (OSError, subprocess.SubprocessError):
            continue
    raise CtxshareError(
        "Clipboard tidak tersedia. Salin transkrip ke file lalu impor file, "
        "atau tempel lewat editor (`ctxshare paste --editor`)."
    )


def open_url(url: str) -> str:
    """Open an http(s) URL with the first available platform helper."""
    if not url.startswith(("http://", "https://")):
        raise CtxshareError(f"URL tidak aman untuk dibuka: {url}")
    commands = []
    if shutil.which("termux-open-url"):
        commands.append(["termux-open-url", url])
    if shutil.which("am"):
        commands.append(["am", "start", "-a", "android.intent.action.VIEW", "-d", url])
    if os.environ.get("DISPLAY") and shutil.which("xdg-open"):
        commands.append(["xdg-open", url])
    if sys.platform == "darwin" and shutil.which("open"):
        commands.append(["open", url])
    for command in commands:
        try:
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return command[0]
        except OSError:
            continue
    raise CtxshareError(f"Tidak bisa membuka browser otomatis. Buka manual: {url}")


def is_tty() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())
