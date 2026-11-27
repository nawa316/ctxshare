"""Read-only HTTP server for shareable context links."""

from __future__ import annotations

import functools
import html
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from . import __version__
from .capsule import TARGETS, Capsule
from .errors import CtxshareError
from .store import Store
from .util import (
    atomic_write,
    fmt_time,
    http_get_json,
    lan_ip,
    now,
    read_json_file,
    runtime_path,
    safe_filename,
)

TOKEN_RE = re.compile(r"^/c/([A-Za-z0-9_-]{12,80})(\.json|\.txt)?$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
}


class ShareServer:
    def __init__(
        self,
        store: Store,
        host: str = "0.0.0.0",
        port: int = 8787,
        public_base_url: str = "",
        verbose: bool = False,
    ):
        self.store = store
        self.host = host
        self.requested_port = int(port)
        self.port = int(port)
        self.public_base_url = public_base_url.strip().rstrip("/")
        self.verbose = verbose
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.started_at = 0.0

    def start(self, attempts: int = 20) -> "ShareServer":
        if self.httpd is not None:
            return self
        handler = functools.partial(ShareHandler, store=self.store, share_server=self)
        httpd: Optional[ThreadingHTTPServer] = None
        last_error: Optional[Exception] = None
        ports = [self.requested_port]
        if self.requested_port != 0:
            ports.extend(self.requested_port + offset for offset in range(1, attempts))
        for port in ports:
            try:
                httpd = ThreadingHTTPServer((self.host, port), handler)
                self.port = httpd.server_address[1]
                break
            except OSError as exc:
                last_error = exc
                continue
        if httpd is None:
            raise CtxshareError(
                f"Tidak bisa membuka port {self.requested_port}-"
                f"{self.requested_port + attempts - 1}: {last_error}"
            )
        httpd.daemon_threads = True
        self.httpd = httpd
        self.started_at = now()
        self.thread = threading.Thread(target=httpd.serve_forever, name="ctxshare-http", daemon=True)
        self.thread.start()
        self._write_runtime()
        return self

    def stop(self) -> None:
        httpd = self.httpd
        self.httpd = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None
        self._remove_runtime()

    def serve_forever(self) -> None:
        if self.httpd is None:
            self.start()
        assert self.httpd is not None
        try:
            self.httpd.serve_forever()
        finally:
            self.stop()

    def base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url
        host = self.host
        if host in ("0.0.0.0", "", "::"):
            host = lan_ip()
        return f"http://{host}:{self.port}"

    def link(self, token: str) -> str:
        return f"{self.base_url()}/c/{token}"

    def _write_runtime(self) -> None:
        info = {
            "pid": os.getpid(),
            "host": self.host,
            "port": self.port,
            "started_at": self.started_at,
            "public_base_url": self.public_base_url,
            "version": __version__,
        }
        try:
            atomic_write(runtime_path(), json.dumps(info, indent=2) + "\n")
        except OSError:
            pass

    def _remove_runtime(self) -> None:
        path = runtime_path()
        try:
            info = read_json_file(path)
            if isinstance(info, dict) and int(info.get("port", -1)) == self.port:
                if int(info.get("pid", -1)) == os.getpid():
                    path.unlink()
        except (OSError, ValueError, CtxshareError):
            pass


def running_server(timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    info = read_json_file(runtime_path())
    if not isinstance(info, dict):
        return None
    try:
        port = int(info.get("port"))
    except (TypeError, ValueError):
        return None
    host = str(info.get("host") or "0.0.0.0")
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        payload = http_get_json(f"http://{probe_host}:{port}/healthz", timeout=timeout)
    except CtxshareError:
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    result = dict(info)
    result["probe_host"] = probe_host
    return result


def share_base_url(info: Dict[str, Any], public_base_url: str = "") -> str:
    if public_base_url:
        return public_base_url.strip().rstrip("/")
    saved = str(info.get("public_base_url") or "").strip().rstrip("/")
    if saved:
        return saved
    host = str(info.get("host") or "0.0.0.0")
    if host in ("0.0.0.0", "", "::"):
        host = lan_ip()
    return f"http://{host}:{int(info.get('port'))}"


class ShareHandler(BaseHTTPRequestHandler):
    server_version = f"ctxshare/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        *args: Any,
        store: Store,
        share_server: ShareServer,
        **kwargs: Any,
    ):
        self.store = store
        self.share_server = share_server
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.share_server.verbose:
            super().log_message(fmt, *args)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: Optional[Dict[str, str]] = None,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        csp = (
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src data:; base-uri 'none'; form-action 'self'"
        )
        self.send_header("Content-Security-Policy", csp)
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_text(
        self,
        status: int,
        text: str,
        content_type: str = "text/plain; charset=utf-8",
        extra_headers: Optional[Dict[str, str]] = None,
        head_only: bool = False,
    ) -> None:
        self._send(
            status,
            text.encode("utf-8"),
            content_type,
            extra_headers=extra_headers,
            head_only=head_only,
        )

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _not_found(self, head_only: bool = False) -> None:
        self._send_text(
            404,
            _page(
                "Link tidak ditemukan",
                "<h1>404</h1><p>Link tidak ditemukan atau sudah kedaluwarsa.</p>"
                '<p><a href="/">ctxshare</a></p>',
            ),
            content_type="text/html; charset=utf-8",
            head_only=head_only,
        )

    def do_GET(self) -> None:
        self._handle(head_only=False)

    def do_HEAD(self) -> None:
        self._handle(head_only=True)

    def _handle(self, head_only: bool) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/healthz":
            self._send_json(200, {"ok": True, "app": "ctxshare", "version": __version__})
            return
        if path == "/":
            self._send_text(
                200,
                _landing_page(),
                content_type="text/html; charset=utf-8",
                head_only=head_only,
            )
            return
        if path == "/go":
            token = (query.get("token") or [""])[0].strip()
            if not re.match(r"^[A-Za-z0-9_-]{12,80}$", token):
                self._send_text(400, "Token tidak valid", head_only=head_only)
                return
            self.send_response(302)
            self.send_header("Location", f"/c/{token}")
            self.send_header("Content-Length", "0")
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            return

        match = TOKEN_RE.match(path)
        if not match:
            self._not_found(head_only)
            return
        token, extension = match.groups()
        record = self.store.get_share(token)
        if record is None:
            self._not_found(head_only)
            return
        capsule: Capsule = record["capsule"]
        target = (query.get("target") or ["generic"])[0].lower()
        if target not in TARGETS:
            target = "generic"
        meta = {
            "token": token,
            "expires_at": record.get("expires_at"),
            "created_at": record.get("created_at"),
        }

        if extension == ".json":
            filename = f"ctxshare-{safe_filename(capsule.id, 'capsule')}.json"
            self._send(
                200,
                capsule.to_json(pretty=True).encode("utf-8"),
                "application/json; charset=utf-8",
                extra_headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                head_only=head_only,
            )
            return
        if extension == ".txt":
            self._send_text(
                200,
                capsule.handoff_prompt(target),
                extra_headers={
                    "Content-Disposition": (
                        f'attachment; filename="ctxshare-'
                        f'{safe_filename(capsule.id, "capsule")}-{target}.txt"'
                    )
                },
                head_only=head_only,
            )
            return
        self._send_text(
            200,
            _capsule_page(capsule, meta, target),
            content_type="text/html; charset=utf-8",
            head_only=head_only,
        )


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

STYLE = """
:root { color-scheme: light dark; --bg:#f7f7f8; --fg:#1b1b1f; --muted:#666;
        --card:#ffffff; --line:#e3e3e8; --user:#e8f0fe; --assistant:#f1f3f4;
        --system:#fff8e1; --accent:#0b6bcb; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111214; --fg:#e8e8ea; --muted:#9aa0a6; --card:#1b1c1f;
          --line:#2c2e33; --user:#17304d; --assistant:#202226;
          --system:#332d17; --accent:#7ab7ff; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
main { max-width: 900px; margin: 0 auto; padding: 18px 16px 48px; }
header h1 { font-size: 1.45rem; margin: 8px 0 4px; overflow-wrap:anywhere; }
.meta { color:var(--muted); font-size:.86rem; margin:0 0 16px; }
.details { background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:12px 14px; margin:14px 0; }
.details summary { cursor:pointer; font-weight:600; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; margin:10px 0 0; }
.msg { background:var(--card); border:1px solid var(--line); border-radius:14px;
       padding:10px 14px; margin:12px 0; }
.msg.user { background:var(--user); }
.msg.assistant { background:var(--assistant); }
.msg.system { background:var(--system); font-size:.92rem; }
.role { font-size:.72rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
        color:var(--muted); margin-bottom:4px; }
.body { white-space:pre-wrap; overflow-wrap:anywhere; }
.actions { display:flex; flex-wrap:wrap; gap:8px; margin:10px 0 4px; }
button, .button { appearance:none; border:1px solid var(--line); background:var(--card);
       color:var(--fg); border-radius:10px; padding:8px 12px; font:inherit;
       font-size:.9rem; cursor:pointer; text-decoration:none; }
button:hover, .button:hover { border-color:var(--accent); }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
button.active { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent) inset; }
.tabs { display:flex; gap:6px; flex-wrap:wrap; margin:12px 0 4px; }
footer { color:var(--muted); font-size:.8rem; margin-top:28px; border-top:1px solid var(--line);
         padding-top:12px; }
a { color:var(--accent); }
.warn { border-left:3px solid #d19a00; padding:6px 10px; color:var(--muted);
        font-size:.86rem; }
input[type=text] { width:100%; padding:10px 12px; border-radius:10px;
        border:1px solid var(--line); background:var(--card); color:var(--fg); font:inherit; }
form { display:flex; gap:8px; margin-top:14px; }
#toast { position:fixed; bottom:18px; left:50%; transform:translateX(-50%);
         background:#222; color:#fff; padding:8px 14px; border-radius:999px;
         opacity:0; transition:opacity .18s; font-size:.86rem; pointer-events:none; }
#toast.show { opacity:.94; }
"""

SCRIPT = """
function toast(text) {
  var el = document.getElementById('toast');
  if (!el) return;
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(function(){ el.classList.remove('show'); }, 1600);
}
function copyText(text, label) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(function(){ toast(label); },
      function(){ fallbackCopy(text, label); });
  } else { fallbackCopy(text, label); }
}
function fallbackCopy(text, label) {
  var area = document.createElement('textarea');
  area.value = text; area.style.position='fixed'; area.style.opacity='0';
  document.body.appendChild(area); area.select();
  try { document.execCommand('copy'); toast(label); }
  catch (err) { toast('Gagal menyalin'); }
  document.body.removeChild(area);
}
function copyEl(id, label) {
  var el = document.getElementById(id);
  if (el) copyText(el.innerText, label);
}
function showTarget(name) {
  ['chatgpt','claude','gemini','generic'].forEach(function(key){
    var box = document.getElementById('box-' + key);
    var btn = document.getElementById('tab-' + key);
    if (box) box.style.display = (key === name) ? 'block' : 'none';
    if (btn) btn.classList.toggle('active', key === name);
  });
}
"""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="id"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{html.escape(title)} · ctxshare</title><style>{STYLE}</style></head>"
        f'<body><main>{body}</main><div id="toast"></div>'
        f"<script>{SCRIPT}</script></body></html>"
    )


def _landing_page() -> str:
    body = """
<header><h1>ctxshare</h1>
<p class="meta">Pindahkan context window antar ChatGPT, Claude, dan Gemini.</p></header>
<div class="details">
<p>Buka link <code>/c/&lt;token&gt;</code> yang dibuat oleh TUI/CLI ctxshare,
atau tempel token/link di bawah.</p>
<form action="/go" method="get">
  <input type="text" name="token" placeholder="token atau link ctxshare" autocomplete="off">
  <button class="primary" type="submit">Buka</button>
</form>
</div>
<footer>Server ini hanya menyajikan capsule yang token-nya diketahui.
Tidak ada daftar percakapan. Link dapat kedaluwarsa.</footer>
"""
    return _page("ctxshare", body)


def _capsule_page(capsule: Capsule, meta: Dict[str, Any], target: str) -> str:
    title = html.escape(capsule.title)
    source = html.escape(
        (capsule.provider or "chatbot lain")
        + (f" / {capsule.model}" if capsule.model else "")
    )
    expires = meta.get("expires_at")
    expiry_text = f"Kedaluwarsa {html.escape(fmt_time(expires))}" if expires else "Tanpa kedaluwarsa"
    meta_line = (
        f"{source} · {html.escape(fmt_time(capsule.created_at))} · "
        f"{len(capsule.messages)} pesan · ~{capsule.tokens()} token · {expiry_text}"
    )
    safe_source = (
        capsule.source_url
        if capsule.source_url.startswith(("http://", "https://"))
        else ""
    )
    source_link = (
        f'<p class="meta">Sumber: <a href="{html.escape(safe_source)}" rel="noreferrer noopener">'
        f"{html.escape(safe_source)}</a></p>"
        if safe_source
        else ""
    )
    messages = []
    for message in capsule.messages:
        role = message.role if message.role in ("user", "assistant", "system") else "system"
        label = {"user": "User", "assistant": "Assistant", "system": "System"}[role]
        messages.append(
            f'<article class="msg {role}"><div class="role">{label}</div>'
            f'<div class="body">{html.escape(message.content)}</div></article>'
        )
    tabs = []
    boxes = []
    for key in ("chatgpt", "claude", "gemini", "generic"):
        info = TARGETS[key]
        label = html.escape(info["label"])
        active = " active" if key == target else ""
        tabs.append(
            f'<button id="tab-{key}" class="{active.strip()}" onclick="showTarget(\'{key}\')">'
            f"{label}</button>"
        )
        prompt = html.escape(capsule.handoff_prompt(key))
        boxes.append(
            f'<div id="box-{key}" style="display:{"block" if key == target else "none"}">'
            f'<p class="meta">{html.escape(info["hint"])}</p>'
            f'<div class="actions"><button class="primary" onclick="copyEl(\'prompt-{key}\',\'Prompt {label} disalin\')">'
            f"Salin prompt {label}</button>"
            f'<a class="button" href="/c/{meta["token"]}.txt?target={key}" download>Download .txt</a></div>'
            f'<pre id="prompt-{key}">{prompt}</pre></div>'
        )
    note = f'<p class="warn">{html.escape(capsule.note)}</p>' if capsule.note else ""
    body = f"""
<header>
  <h1>{title}</h1>
  <p class="meta">{meta_line}</p>
  {source_link}
</header>
<div class="tabs">{''.join(tabs)}</div>
<div class="details">
  <p class="meta">Pilih provider tujuan, salin prompt, lalu tempel sebagai pesan pertama di chat baru.</p>
  {''.join(boxes)}
  <div class="actions">
    <a class="button" href="/c/{meta['token']}.json" download>Download capsule .json</a>
    <a class="button" href="/c/{meta['token']}.txt" download>Download prompt default</a>
  </div>
</div>
{note}
<section>{''.join(messages)}</section>
<footer>
  Dibagikan dengan ctxshare. Siapa pun yang memiliki link ini dapat membaca percakapan.
  {'Link berlaku sampai ' + html.escape(fmt_time(expires)) + '.' if expires else ''}
</footer>
"""
    return _page(capsule.title, body)
