"""Command line interface for ctxshare."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .capsule import TARGETS, Capsule, target_label
from .config import config_path, load_config, save_config
from .errors import CtxshareError
from .ingest import parse_transcript
from .server import ShareServer, running_server, share_base_url
from .share import create_gist, resolve_input
from .store import Store
from .util import (
    clipboard_text,
    data_dir,
    db_path,
    fmt_relative,
    fmt_time,
    fmt_tokens,
    is_tty,
    lan_ip,
    log_path,
    open_url,
)

EXAMPLE_TEXT = """contoh:
  ctxshare                              buka TUI
  ctxshare import https://chatgpt.com/share/...    impor share ChatGPT
  ctxshare import https://claude.ai/share/...     impor share Claude
  ctxshare import https://gemini.google.com/share/...  impor share Gemini
  ctxshare import --clipboard            impor transkrip yang disalin
  ctxshare import chatgpt-export.json    impor export (pilih percakapan)
  ctxshare list                          daftar context di library
  ctxshare share 1a2b3c4d                link LAN + prompt handoff
  ctxshare share 1a2b3c4d --gist         link secret gist
  ctxshare show 1a2b3c4d --prompt --target claude
  ctxshare serve --port 8787             server link
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctxshare",
        description="Pindahkan context window antar ChatGPT, Claude, Gemini lewat link.",
        epilog=EXAMPLE_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ctxshare {__version__}")
    parser.add_argument("--data-dir", help="folder data (default ~/.local/share/ctxshare)")
    parser.add_argument("--config", help="path config.json")
    sub = parser.add_subparsers(dest="command")

    tui = sub.add_parser("tui", help="buka antarmuka terminal")
    tui.add_argument("--no-serve", action="store_true", help="jangan jalankan server link")
    tui.add_argument(
        "--target",
        choices=("chatgpt", "claude", "gemini", "generic"),
        default=None,
        help="target prompt default",
    )

    imp = sub.add_parser("import", help="impor share link / file / kode / transkrip")
    imp.add_argument("source", nargs="?", help="URL, file, kode ctx1..., token, atau '-'")
    imp.add_argument("--clipboard", action="store_true", help="baca transkrip dari clipboard")
    imp.add_argument("--paste", action="store_true", help="buka editor untuk menempel transkrip")
    imp.add_argument("--title", help="override judul")
    imp.add_argument("--all", action="store_true", help="impor semua percakapan dari export")
    imp.add_argument("--index", type=int, help="pilih percakapan ke-N (1-based) dari export")
    imp.add_argument("--list", action="store_true", help="tampilkan pilihan tanpa menyimpan")
    imp.add_argument("--dry-run", action="store_true", help="jangan simpan")
    imp.add_argument("--json", action="store_true", help="output JSON")
    imp.add_argument("--timeout", type=float, default=None)

    paste = sub.add_parser("paste", help="impor transkrip dari clipboard/editor")
    paste.add_argument("--editor", action="store_true", help="paksa buka editor")
    paste.add_argument("--title")
    paste.add_argument("--json", action="store_true")

    listing = sub.add_parser("list", help="daftar context di library")
    listing.add_argument("query", nargs="?", default="")
    listing.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="tampilkan satu capsule")
    show.add_argument("capsule", nargs="?", default="last")
    show.add_argument("--json", action="store_true")
    show.add_argument("--prompt", action="store_true", help="cetak prompt handoff")
    show.add_argument(
        "--target",
        choices=("chatgpt", "claude", "gemini", "generic"),
        default=None,
    )

    share = sub.add_parser("share", help="buat link + prompt handoff")
    share.add_argument("capsule", nargs="?", default="last")
    share.add_argument("--gist", action="store_true", help="unggah juga ke secret GitHub gist")
    share.add_argument("--ttl", type=float, default=None, help="masa berlaku link (jam); 0 = selamanya")
    share.add_argument("--serve", action="store_true", help="jalankan server LAN sementara")
    share.add_argument("--port", type=int, default=None, help="port server")
    share.add_argument(
        "--target",
        choices=("chatgpt", "claude", "gemini", "generic"),
        default=None,
        help="provider tujuan untuk prompt",
    )
    share.add_argument(
        "--print",
        dest="print_mode",
        choices=("summary", "link", "code", "prompt", "json"),
        default="summary",
    )
    share.add_argument("--json", action="store_true")
    share.add_argument("--open", action="store_true", help="buka URL target di browser")
    share.add_argument("--max-chars", type=int, default=0, help="potong transkrip")

    serve = sub.add_parser("serve", help="jalankan server link")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--public-url", default=None)
    serve.add_argument("--verbose", action="store_true")

    export = sub.add_parser("export", help="ekspor capsule")
    export.add_argument("capsule", nargs="?", default="last")
    export.add_argument("--format", choices=("json", "md", "prompt"), default="md")
    export.add_argument("--target", choices=("chatgpt", "claude", "gemini", "generic"), default=None)
    export.add_argument("--max-chars", type=int, default=0)
    export.add_argument("--out", "-o")

    delete = sub.add_parser("delete", help="hapus capsule dari library")
    delete.add_argument("capsule")
    delete.add_argument("--yes", "-y", action="store_true")

    cfg = sub.add_parser("config", help="lihat/ubah config")
    cfg.add_argument("--path", action="store_true")
    cfg.add_argument("--show", action="store_true")
    cfg.add_argument("--init", action="store_true")

    sub.add_parser("doctor", help="cek koneksi, gh, server, clipboard")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ["CTXSHARE_DATA"] = os.path.expanduser(args.data_dir)
    if args.config:
        os.environ["CTXSHARE_CONFIG"] = os.path.expanduser(args.config)
    if not args.command:
        if is_tty():
            args.command = "tui"
        else:
            parser.print_help()
            return 0

    try:
        config = load_config()
        if args.command == "config":
            return _cmd_config(args, config)
        if args.command == "doctor":
            return _cmd_doctor(config)
        if args.command == "tui":
            from .tui import run_tui

            target = args.target or str((config.get("share") or {}).get("target") or "chatgpt")
            return run_tui(
                config,
                auto_serve=not args.no_serve,
                target=target,
            )
        store = Store()
        try:
            dispatch = {
                "import": _cmd_import,
                "paste": _cmd_paste,
                "list": _cmd_list,
                "show": _cmd_show,
                "share": _cmd_share,
                "serve": _cmd_serve,
                "export": _cmd_export,
                "delete": _cmd_delete,
            }
            return int(dispatch[args.command](args, config, store) or 0)
        finally:
            store.close()
    except CtxshareError as exc:
        print(f"ctxshare: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nDibatalkan.", file=sys.stderr)
        return 130


# --------------------------------------------------------------------------
# Import / paste
# --------------------------------------------------------------------------


def _cmd_import(args: argparse.Namespace, config: Dict[str, Any], store: Store) -> int:
    timeout = args.timeout if args.timeout is not None else float(
        (config.get("fetch") or {}).get("timeout") or 45
    )
    source = args.source
    capsules: List[Capsule]
    if args.clipboard:
        text = clipboard_text()
        single = text.strip()
        if "\n" not in single and (
            single.startswith(("http://", "https://", "ctx1."))
            or Path(os.path.expanduser(single)).is_file()
        ):
            capsules = resolve_input(single, store)
        else:
            capsule = parse_transcript(text, title=args.title or "", origin="paste")
            capsules = [capsule]
    elif args.paste or (source is None and not sys.stdin.isatty()):
        if args.paste:
            text = _edit_text(_PASTE_TEMPLATE, suffix=".md")
            capsule = parse_transcript(text, title=args.title or "", origin="paste")
        else:
            text = sys.stdin.read()
            capsule = parse_transcript(text, title=args.title or "", origin="stdin")
        capsules = [capsule]
    elif source is not None:
        if source == "-":
            text = sys.stdin.read()
            capsule = parse_transcript(text, title=args.title or "", origin="stdin")
            capsules = [capsule]
        else:
            capsules = _resolve_with_progress(source, store, timeout)
    else:
        raise CtxshareError(
            "Sumber kosong. Berikan link/file, gunakan --clipboard, --paste, atau '-' untuk stdin."
        )

    if args.title:
        for capsule in capsules:
            capsule.title = args.title

    if not capsules:
        raise CtxshareError("Tidak ada percakapan yang ditemukan")
    if len(capsules) > 1 and args.list:
        _print_choices(capsules)
        return 0
    if len(capsules) > 1 and args.index is None and not args.all:
        _print_choices(capsules)
        newest = max(capsules, key=lambda item: item.created_at)
        print(
            f"\n{len(capsules)} percakapan ditemukan. Mengimpor yang terbaru: "
            f"'{newest.title}'.\nGunakan --index N atau --all untuk memilih.",
            file=sys.stderr,
        )
        capsules = [newest]
    elif args.index is not None:
        if not 1 <= args.index <= len(capsules):
            raise CtxshareError(f"--index harus 1..{len(capsules)}")
        capsules = [capsules[args.index - 1]]
    if args.list:
        _print_choices(capsules)
        return 0

    max_chars = int((config.get("import") or {}).get("max_chars") or 0)
    results = []
    for capsule in capsules:
        if max_chars > 0:
            capsule = capsule.truncated(max_chars)
        if args.dry_run:
            results.append(
                {
                    "id": capsule.id,
                    "title": capsule.title,
                    "provider": capsule.provider,
                    "model": capsule.model,
                    "messages": len(capsule.messages),
                    "tokens": capsule.tokens(),
                    "saved": False,
                }
            )
            continue
        capsule_id = store.save_capsule(capsule)
        results.append(
            {
                "id": capsule_id,
                "title": capsule.title,
                "provider": capsule.provider,
                "model": capsule.model,
                "messages": len(capsule.messages),
                "tokens": capsule.tokens(),
                "source_url": capsule.source_url,
                "saved": True,
            }
        )

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for result in results:
        if result["saved"]:
            print(
                f"✓ Tersimpan: {result['id']}  {result['title']}  "
                f"({result['provider']}/{result['model']}, {result['messages']} pesan, "
                f"~{result['tokens']} token)"
            )
        else:
            print(
                f"· Pratinjau: {result['title']}  "
                f"({result['provider']}/{result['model']}, {result['messages']} pesan)"
            )
    saved = [result for result in results if result["saved"]]
    if saved:
        print("\nLangkah berikutnya:")
        print(f"  ctxshare show {saved[0]['id']} --prompt --target claude")
        print(f"  ctxshare share {saved[0]['id']} --gist")
    return 0


def _cmd_paste(args: argparse.Namespace, config: Dict[str, Any], store: Store) -> int:
    text = ""
    if not args.editor:
        try:
            text = clipboard_text()
        except CtxshareError:
            text = ""
    if not text.strip() or args.editor:
        text = _edit_text(_PASTE_TEMPLATE, suffix=".md")
    capsule = parse_transcript(text, title=args.title or "", origin="paste")
    capsule_id = store.save_capsule(capsule)
    if args.json:
        print(
            json.dumps(
                {
                    "id": capsule_id,
                    "title": capsule.title,
                    "provider": capsule.provider,
                    "messages": len(capsule.messages),
                    "tokens": capsule.tokens(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            f"✓ Tersimpan: {capsule_id}  {capsule.title}  "
            f"({capsule.provider}, {len(capsule.messages)} pesan, ~{capsule.tokens()} token)"
        )
        print(f"  ctxshare show {capsule_id} --prompt --target chatgpt")
    return 0


_PASTE_TEMPLATE = """# Judul percakapan

You said:

Assistant said:

"""


def _edit_text(initial: str, suffix: str = ".md") -> str:
    command = _editor_command()
    fd, path_str = tempfile.mkstemp(prefix="ctxshare-paste-", suffix=suffix)
    path = Path(path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(initial)
        try:
            result = subprocess.run(command + [str(path)])
        except OSError as exc:
            raise CtxshareError(f"Gagal membuka editor: {exc}") from exc
        if result.returncode != 0:
            raise CtxshareError("Editor ditutup dengan error; impor dibatalkan")
        text = path.read_text(encoding="utf-8")
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if not text.strip() or text.strip() == _PASTE_TEMPLATE.strip():
        raise CtxshareError("Teks masih kosong. Tempel transkrip ke editor lalu simpan.")
    return text


def _editor_command() -> List[str]:
    import shlex

    configured = os.environ.get("EDITOR", "").strip()
    if configured:
        command = shlex.split(configured)
        if shutil.which(command[0]):
            return command
    for candidate in ("nano", "vi", "vim"):
        if shutil.which(candidate):
            return [candidate]
    raise CtxshareError("Editor tidak ditemukan. Set $EDITOR (mis. nano) atau pakai --clipboard.")


def _resolve_with_progress(source: str, store: Store, timeout: float) -> List[Capsule]:
    from . import ingest

    text = source.strip()
    if text.startswith(("http://", "https://")):
        print(f"Mengambil {text} …", file=sys.stderr)
        if "gist.github.com" in text:
            return resolve_input(text, store)
        return ingest.fetch_source(text, timeout=timeout)
    return resolve_input(text, store)


def _print_choices(capsules: List[Capsule]) -> None:
    print(f"{'#':>2}  {'PESAN':>5}  {'TOKEN':>7}  {'SUMBER':<18}  JUDUL")
    for index, capsule in enumerate(capsules, 1):
        source = f"{capsule.provider}/{capsule.model}" if capsule.model else capsule.provider
        print(
            f"{index:>2}  {len(capsule.messages):>5}  {capsule.tokens():>7}  "
            f"{source[:18]:<18}  {capsule.title}"
        )


# --------------------------------------------------------------------------
# Library
# --------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace, config: Dict[str, Any], store: Store) -> int:
    rows = store.list_capsules(args.query)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("Library kosong. Impor context dulu:")
        print("  ctxshare import https://chatgpt.com/share/...")
        print("  ctxshare import --clipboard")
        return 0
    print(f"{'ID':<16} {'PESAN':>5} {'TOKEN':>7}  {'SUMBER':<20} {'DIIMPOR':<12} JUDUL")
    for row in rows:
        source = f"{row['provider']}/{row['model']}" if row["model"] else row["provider"]
        print(
            f"{row['id']:<16} {int(row['message_count']):>5} {fmt_tokens(row['tokens']):>7}  "
            f"{source[:20]:<20} {fmt_relative(row['imported_at']):<12} {row['title']}"
        )
    return 0


def _cmd_show(args: argparse.Namespace, config: Dict[str, Any], store: Store) -> int:
    capsule = store.get_capsule(args.capsule)
    target = args.target or str((config.get("share") or {}).get("target") or "chatgpt")
    if args.json:
        print(capsule.to_json(pretty=True), end="")
        return 0
    if args.prompt:
        print(capsule.handoff_prompt(target), end="")
        return 0
    print(f"# {capsule.title}")
    source = capsule.provider or "-"
    if capsule.model:
        source += f" / {capsule.model}"
    print(
        f"# {source} · {len(capsule.messages)} pesan · ~{capsule.tokens()} token · "
        f"id {capsule.id} · {fmt_time(capsule.created_at)}"
    )
    if capsule.source_url:
        print(f"# sumber: {capsule.source_url}")
    if capsule.note:
        print(f"# catatan: {capsule.note}")
    if capsule.system:
        print("\n[system]")
        print(capsule.system)
    for message in capsule.messages:
        print(f"\n[{message.role}]")
        print(message.content)
    return 0


def _cmd_delete(args: argparse.Namespace, config: Dict[str, Any], store: Store) -> int:
    capsule = store.get_capsule(args.capsule)
    if not args.yes:
        answer = input(f"Hapus '{capsule.title}' ({capsule.id})? [y/N] ").strip().lower()
        if answer not in ("y", "ya", "yes"):
            print("Dibatalkan.")
            return 1
    store.delete_capsule(capsule.id)
    print(f"✓ Dihapus: {capsule.id}")
    return 0


# --------------------------------------------------------------------------
# Share / serve / export
# --------------------------------------------------------------------------


def _cmd_share(args: argparse.Namespace, config: Dict[str, Any], store: Store) -> int:
    capsule = store.get_capsule(args.capsule)
    if args.max_chars and args.max_chars > 0:
        capsule = capsule.truncated(args.max_chars)
    share_cfg = config.get("share") or {}
    target = args.target or str(share_cfg.get("target") or "chatgpt")
    ttl = args.ttl if args.ttl is not None else float(share_cfg.get("ttl_hours") or 0)
    record = store.create_share(capsule.id, ttl_hours=ttl)
    token = str(record["token"])

    info = running_server()
    server: Optional[ShareServer] = None
    if args.serve and info is None:
        port = args.port if args.port is not None else int(share_cfg.get("port") or 8787)
        server = ShareServer(
            store,
            host=str(share_cfg.get("host") or "0.0.0.0"),
            port=port,
            public_base_url=str(share_cfg.get("public_base_url") or ""),
        )
        server.start()
        info = {"host": server.host, "port": server.port, "public_base_url": server.public_base_url}
        print(f"Server link aktif di {server.base_url()}/ (Ctrl+C untuk berhenti)\n", file=sys.stderr)
    if info is not None:
        link = f"{share_base_url(info, str(share_cfg.get('public_base_url') or ''))}/c/{token}"
    else:
        link = None

    gist_url: Optional[str] = None
    if args.gist:
        gist_url = create_gist(capsule)

    prompt = capsule.handoff_prompt(target)
    if args.print_mode == "json" or args.json:
        payload = {
            "ok": True,
            "id": capsule.id,
            "title": capsule.title,
            "token": token,
            "link": link,
            "gist": gist_url,
            "target": target,
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
            "messages": len(capsule.messages),
            "tokens": capsule.tokens(),
            "prompt": prompt,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.print_mode == "link":
        if not link:
            raise CtxshareError(
                "Tidak ada server link aktif. Jalankan `ctxshare serve` di terminal lain "
                "atau ulangi dengan `--serve`."
            )
        print(link)
    elif args.print_mode == "code":
        print(capsule.encode_code())
    elif args.print_mode == "prompt":
        print(prompt, end="")
    else:
        ttl_text = (
            f"{fmt_time(record['expires_at'])}" if record.get("expires_at") else "tidak kedaluwarsa"
        )
        print(f"Capsule  : {capsule.title}")
        print(f"ID       : {capsule.id}")
        print(
            f"Pesan    : {len(capsule.messages)} · ~{capsule.tokens()} token · "
            f"{capsule.provider}/{capsule.model}"
        )
        print(f"Target   : {target_label(target)}")
        print(f"Token    : {token}")
        print(f"Berlaku  : {ttl_text}")
        print(f"Link LAN : {link or '(server belum aktif — pakai --serve atau `ctxshare serve`)'}")
        if gist_url:
            print(f"Link gist: {gist_url}")
        print("Kode     : gunakan `--print code` untuk kode offline ctx1....")
        print(f"Prompt   : ctxshare show {capsule.id} --prompt --target {target}")
        print(f"Cara pakai: {TARGETS[target]['hint']}")

    if args.open:
        url = TARGETS[target].get("url")
        if url:
            try:
                mechanism = open_url(url)
                print(f"Membuka {url} via {mechanism}", file=sys.stderr)
            except CtxshareError as exc:
                print(f"ctxshare: {exc}", file=sys.stderr)

    if args.serve and server is not None:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nServer dihentikan.")
        finally:
            server.stop()
    return 0


def _cmd_serve(args: argparse.Namespace, config: Dict[str, Any], store: Store) -> int:
    share_cfg = config.get("share") or {}
    host = args.host or str(share_cfg.get("host") or "0.0.0.0")
    port = args.port if args.port is not None else int(share_cfg.get("port") or 8787)
    public_url = args.public_url or str(share_cfg.get("public_base_url") or "")
    store.purge_expired()
    server = ShareServer(
        store,
        host=host,
        port=port,
        public_base_url=public_url,
        verbose=args.verbose,
    )
    server.start()
    print(f"ctxshare server: {server.base_url()}/")
    print(f"  data   : {data_dir()}")
    print(f"  capsule: {store.count_capsules()} di library")
    print("Buat link dengan `ctxshare share <id>`. Tekan Ctrl+C untuk berhenti.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nServer dihentikan.")
    finally:
        server.stop()
    return 0


def _cmd_export(args: argparse.Namespace, config: Dict[str, Any], store: Store) -> int:
    capsule = store.get_capsule(args.capsule)
    if args.max_chars > 0:
        capsule = capsule.truncated(args.max_chars)
    target = args.target or str((config.get("share") or {}).get("target") or "chatgpt")
    if args.format == "json":
        text = capsule.to_json(pretty=True)
        extension = "json"
    elif args.format == "prompt":
        text = capsule.handoff_prompt(target)
        extension = "txt"
    else:
        text = capsule.to_markdown() + "\n"
        extension = "md"
    if args.out and args.out != "-":
        path = Path(os.path.expanduser(args.out))
        if path.is_dir():
            path = path / f"{capsule.filename_stem()}-{target}.{extension}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        print(f"✓ Ditulis ke {path}")
    else:
        print(text, end="" if text.endswith("\n") else "\n")
    return 0


# --------------------------------------------------------------------------
# Config / doctor
# --------------------------------------------------------------------------


def _cmd_config(args: argparse.Namespace, config: Dict[str, Any]) -> int:
    path = config_path()
    if args.init:
        save_config(config, path)
        print(f"✓ Config ditulis: {path}")
        return 0
    if args.path:
        print(path)
        return 0
    print(f"Config : {path}")
    print(f"Data   : {data_dir()}")
    print(f"DB     : {db_path()}")
    print("")
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0


def _cmd_doctor(config: Dict[str, Any]) -> int:
    print(f"ctxshare {__version__}")
    print(f"Python : {sys.version.split()[0]} ({sys.executable})")
    print(f"Config : {config_path()}")
    print(f"Data   : {data_dir()}")
    try:
        test_file = data_dir() / ".write-test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        print("✓ folder data dapat ditulis")
    except OSError as exc:
        print(f"✗ folder data tidak dapat ditulis: {exc}")

    print("\nKoneksi:")
    for host in ("chatgpt.com", "claude.ai", "gemini.google.com", "api.github.com"):
        try:
            with socket.create_connection((host, 443), timeout=5):
                print(f"  ✓ {host}")
        except OSError as exc:
            print(f"  ✗ {host}: {exc}")

    if shutil.which("gh"):
        try:
            result = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                status_text = (result.stdout or "") + (result.stderr or "")
                if "'gist'" in status_text or '"gist"' in status_text:
                    print("✓ gh terautentikasi (scope gist tersedia)")
                else:
                    print(
                        "! gh login, tetapi scope 'gist' belum ada. "
                        "Jalankan: gh auth refresh -h github.com -s gist"
                    )
            else:
                print("! gh terpasang tetapi belum login (`gh auth login`)")
        except (OSError, subprocess.SubprocessError):
            print("! gagal memeriksa gh")
    else:
        print("! gh tidak terpasang (link gist tidak tersedia)")

    clipboard_tools = [
        name
        for name in ("termux-clipboard-get", "wl-paste", "xclip", "pbpaste")
        if shutil.which(name)
    ]
    print(
        "✓ clipboard: " + ", ".join(clipboard_tools)
        if clipboard_tools
        else "! clipboard helper tidak ditemukan (pakai editor/import file)"
    )
    editor = os.environ.get("EDITOR") or next(
        (name for name in ("nano", "vi", "vim") if shutil.which(name)), ""
    )
    print(f"✓ editor: {editor}" if editor else "! editor tidak ditemukan (set $EDITOR)")

    info = running_server()
    if info:
        print(
            "✓ server link aktif: "
            f"{share_base_url(info, str((config.get('share') or {}).get('public_base_url') or ''))}/"
        )
    else:
        print("· server link belum aktif (TUI menyalakannya otomatis)")
    print(f"· LAN IP : {lan_ip()}")
    print(f"· DB     : {db_path()}")
    print(f"· log    : {log_path()}")
    print("\nTidak perlu API key. ctxshare hanya membaca share link dan menyusun prompt.")
    return 0
