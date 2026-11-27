"""Curses TUI: import, inspect and share context capsules."""

from __future__ import annotations

import curses
import logging
import os
import queue
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .capsule import TARGETS, Capsule, target_label, target_window
from .config import save_config
from .errors import CtxshareError
from .ingest import parse_transcript
from .server import ShareServer
from .share import create_gist, resolve_input
from .store import Store
from .util import (
    clipboard_text,
    copy_to_clipboard,
    data_dir,
    fmt_relative,
    fmt_time,
    fmt_tokens,
    log_path,
    now,
    open_url,
    safe_filename,
    unique_suffix,
)

PAIR_HEADER = 1
PAIR_USER = 2
PAIR_ASSISTANT = 3
PAIR_SYSTEM = 4
PAIR_DIM = 5
PAIR_ERROR = 6
PAIR_ACCENT = 7
PAIR_BORDER = 8

PASTE_TEMPLATE = """# Judul percakapan

You said:

Assistant said:

"""

logger = logging.getLogger("ctxshare.tui")


def run_tui(
    config: Dict[str, Any],
    auto_serve: bool = True,
    target: str = "chatgpt",
) -> int:
    _setup_logging()
    store = Store()
    app = TuiApp(config, store, auto_serve=auto_serve, target=target)
    try:
        curses.wrapper(app.run)
    except curses.error as exc:
        raise CtxshareError(
            "TUI membutuhkan terminal interaktif (curses). "
            f"Jalankan langsung di Termux/terminal, bukan lewat pipe. ({exc})"
        ) from exc
    finally:
        app.cleanup()
    return 0


def _setup_logging() -> None:
    try:
        logging.basicConfig(
            filename=str(log_path()),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            force=True,
        )
    except OSError:
        pass


def _is_escape(ch: Any) -> bool:
    return isinstance(ch, str) and ch == "\x1b"


def _char_width(char: str) -> int:
    if not char:
        return 0
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def fit_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    result: List[str] = []
    used = 0
    for char in text:
        if char in "\r\n\t":
            char = " "
        if unicodedata.category(char).startswith("C"):
            continue
        size = _char_width(char)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


def wrap_text(text: str, width: int) -> List[str]:
    width = max(8, width)
    lines: List[str] = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        raw = raw.replace("\t", "    ").replace("\x00", "")
        if not raw.strip():
            lines.append("")
            continue
        wrapped = textwrap.wrap(
            raw,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
            drop_whitespace=True,
        )
        lines.extend(wrapped or [""])
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


class TuiApp:
    def __init__(
        self,
        config: Dict[str, Any],
        store: Store,
        auto_serve: bool = True,
        target: str = "chatgpt",
    ):
        self.config = config
        self.store = store
        self.auto_serve = auto_serve
        self.target = target if target in TARGETS else "chatgpt"
        self.stdscr: Any = None
        self.running = True
        self.cleaned = False
        self.rows: List[Dict[str, Any]] = []
        self.selected = 0
        self.list_offset = 0
        self.query = ""
        self.viewing: Optional[Capsule] = None
        self.view_scroll = 0
        self.status = ""
        self.status_error = False
        self.status_until = 0.0
        self.last_error = ""
        self.server: Optional[ShareServer] = None
        self.server_error = ""
        self.server_attempted = False
        self._view_cache_key: Any = None
        self._view_cache: List[Tuple[str, int]] = []
        self.refresh_list()
        if self.auto_serve:
            self.ensure_server()

    # -- lifecycle ---------------------------------------------------------

    def ensure_server(self, retry: bool = False) -> Tuple[Optional[ShareServer], str]:
        if self.server is not None:
            return self.server, ""
        if not self.auto_serve:
            self.server_error = "server link dimatikan (--no-serve)"
            return None, self.server_error
        if self.server_attempted and not retry and self.server_error:
            return None, self.server_error
        self.server_attempted = True
        self.server_error = ""
        share_cfg = self.config.get("share") or {}
        try:
            server = ShareServer(
                self.store,
                host=str(share_cfg.get("host") or "0.0.0.0"),
                port=int(share_cfg.get("port") or 8787),
                public_base_url=str(share_cfg.get("public_base_url") or ""),
            )
            server.start()
            self.server = server
            return server, ""
        except CtxshareError as exc:
            self.server_error = str(exc)
            logger.warning("server tidak aktif: %s", exc)
            return None, self.server_error

    def cleanup(self) -> None:
        if self.cleaned:
            return
        self.cleaned = True
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                logger.exception("gagal menghentikan server")
            self.server = None
        try:
            self.store.close()
        except Exception:
            logger.exception("gagal menutup database")

    def run(self, stdscr: Any) -> None:
        self.stdscr = stdscr
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass
        curses.noecho()
        curses.raw()
        stdscr.keypad(True)
        stdscr.timeout(80)
        self._init_colors()
        self.set_status(
            "i impor · p tempel · Enter buka · s bagikan · ? bantuan",
            ttl=10,
        )
        while self.running:
            self.draw()
            self.tick()
            key = self.getkey()
            if key is None:
                continue
            self.handle_key(key)

    def _init_colors(self) -> None:
        if not curses.has_colors():
            return
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            return
        definitions = [
            (PAIR_HEADER, curses.COLOR_BLACK, curses.COLOR_CYAN),
            (PAIR_USER, curses.COLOR_GREEN, -1),
            (PAIR_ASSISTANT, curses.COLOR_CYAN, -1),
            (PAIR_SYSTEM, curses.COLOR_YELLOW, -1),
            (PAIR_DIM, curses.COLOR_WHITE, -1),
            (PAIR_ERROR, curses.COLOR_RED, -1),
            (PAIR_ACCENT, curses.COLOR_MAGENTA, -1),
            (PAIR_BORDER, curses.COLOR_BLUE, -1),
        ]
        for pair, fg, bg in definitions:
            try:
                curses.init_pair(pair, fg, bg)
            except curses.error:
                pass

    def getkey(self, timeout: int = 80) -> Any:
        if self.stdscr is None:
            return None
        self.stdscr.timeout(timeout)
        try:
            return self.stdscr.get_wch()
        except curses.error:
            return None

    def tick(self) -> None:
        if self.status_until and now() > self.status_until:
            self.status = ""
            self.status_error = False
            self.status_until = 0

    def set_status(self, text: str, error: bool = False, ttl: float = 7) -> None:
        self.status = text
        self.status_error = error
        self.status_until = now() + ttl
        if error:
            self.last_error = text
            logger.warning(text)

    def _attr(self, pair: int, extra: int = 0) -> int:
        try:
            return curses.color_pair(pair) | extra
        except curses.error:
            return extra

    # -- drawing helpers ---------------------------------------------------

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if self.stdscr is None:
            return
        try:
            height, width = self.stdscr.getmaxyx()
        except curses.error:
            return
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        clipped = fit_text(text, width - x)
        if not clipped:
            return
        try:
            self.stdscr.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    def fill(self, y: int, x: int, height: int, width: int, attr: int = 0) -> None:
        if self.stdscr is None:
            return
        try:
            max_h, max_w = self.stdscr.getmaxyx()
        except curses.error:
            return
        for row in range(max(0, y), min(max_h, y + height)):
            self.put(row, x, " " * max(0, min(max_w - x, width)), attr)

    def draw(self) -> None:
        if self.stdscr is None:
            return
        self.stdscr.erase()
        try:
            height, width = self.stdscr.getmaxyx()
        except curses.error:
            return
        if height < 8 or width < 24:
            self.put(0, 0, "Terminal terlalu kecil. Perbesar jendela.", self._attr(PAIR_ERROR, curses.A_BOLD))
            self.stdscr.refresh()
            return
        if self.viewing is not None:
            self._draw_viewer(height, width)
        else:
            self._draw_library(height, width)
        try:
            self.stdscr.refresh()
        except curses.error:
            pass

    def _draw_header(self, height: int, width: int, title: str) -> None:
        server = "link ON" if self.server else "link OFF"
        left = f" ctxshare  {title}"
        right = f"{target_label(self.target)} · {server} "
        if len(left) + len(right) > width:
            left = fit_text(left, max(4, width - len(right)))
        padded = left + " " * max(0, width - len(left) - len(right)) + right
        attr = self._attr(PAIR_HEADER, curses.A_BOLD)
        self.put(0, 0, fit_text(padded, width), attr)
        try:
            self.stdscr.chgat(0, 0, width, attr)
        except curses.error:
            pass

    # -- library -----------------------------------------------------------

    def refresh_list(self) -> None:
        try:
            self.rows = self.store.list_capsules(self.query)
        except CtxshareError as exc:
            self.rows = []
            self.set_status(str(exc), error=True)
        if self.selected >= len(self.rows):
            self.selected = max(0, len(self.rows) - 1)

    def current_row(self) -> Optional[Dict[str, Any]]:
        if 0 <= self.selected < len(self.rows):
            return self.rows[self.selected]
        return None

    def _draw_library(self, height: int, width: int) -> None:
        title = "library" + (f"  /{self.query}" if self.query else "")
        self._draw_header(height, width, title)
        top = 1
        bottom = height - 2
        list_height = max(1, bottom - top)
        if not self.rows:
            self._draw_empty(width, top, list_height)
        else:
            if self.selected < self.list_offset:
                self.list_offset = self.selected
            if self.selected >= self.list_offset + list_height:
                self.list_offset = self.selected - list_height + 1
            for row_index in range(list_height):
                index = self.list_offset + row_index
                if index >= len(self.rows):
                    break
                row = self.rows[index]
                selected = index == self.selected
                marker = "›" if selected else " "
                source = f"{row['provider']}/{row['model']}" if row["model"] else row["provider"]
                meta = f"{source} · {int(row['message_count'])}p · {fmt_tokens(row['tokens'])}"
                title_text = str(row["title"])
                if width >= 58:
                    available = max(10, width - len(meta) - 4)
                    line = f"{marker} {fit_text(title_text, available):<{available}} {meta}"
                    attr = (
                        self._attr(PAIR_ACCENT, curses.A_REVERSE | curses.A_BOLD)
                        if selected
                        else 0
                    )
                    self.put(top + row_index, 0, fit_text(line, width), attr)
                else:
                    line = f"{marker} {title_text}"
                    attr = (
                        self._attr(PAIR_ACCENT, curses.A_REVERSE | curses.A_BOLD)
                        if selected
                        else 0
                    )
                    self.put(top + row_index, 0, fit_text(line, width), attr)

        status_row = height - 2
        self.fill(status_row, 0, 1, width, self._attr(PAIR_DIM))
        if self.status:
            marker = "!" if self.status_error else "·"
            text = f" {marker} {self.status}"
            attr = self._attr(PAIR_ERROR if self.status_error else PAIR_DIM, curses.A_BOLD)
        else:
            row = self.current_row()
            if row:
                text = (
                    f" {row['id']} · {row['provider']}"
                    + (f"/{row['model']}" if row["model"] else "")
                    + f" · diimpor {fmt_relative(row['imported_at'])}"
                )
                if row.get("source_url") and width > 70:
                    text += f" · {row['source_url']}"
            else:
                text = " Belum ada context. Tekan i untuk impor."
            attr = self._attr(PAIR_DIM)
        self.put(status_row, 0, fit_text(text, width), attr)
        hint = "Enter buka · i impor · p tempel · s bagikan · d hapus · / cari · ? bantuan · q keluar"
        if width < 70:
            hint = "Enter buka · i impor · p tempel · s share · ? help · q"
        self.put(height - 1, 0, fit_text(hint, width), self._attr(PAIR_DIM, curses.A_DIM))

    def _draw_empty(self, width: int, top: int, height: int) -> None:
        lines = [
            "Belum ada context window.",
            "",
            "  i   impor link share ChatGPT / Claude / Gemini",
            "  p   tempel transkrip dari clipboard / editor",
            "  ?   bantuan dan alur handoff",
            "",
            "ctxshare memindahkan konteks, bukan menjadi chat client.",
        ]
        start = top + max(0, (height - len(lines)) // 2)
        for index, line in enumerate(lines):
            attr = self._attr(PAIR_DIM, curses.A_BOLD if index == 0 else curses.A_DIM)
            self.put(start + index, 2, fit_text(line, width - 4), attr)

    # -- viewer ------------------------------------------------------------

    def open_viewer(self, capsule: Capsule) -> None:
        self.viewing = capsule
        self.view_scroll = 0
        self._view_cache_key = None
        self.set_status("", ttl=0)

    def _view_lines(self, width: int) -> List[Tuple[str, int]]:
        capsule = self.viewing
        if capsule is None:
            return []
        key = (width, capsule.id, len(capsule.messages), capsule.system, capsule.title)
        if key == self._view_cache_key:
            return self._view_cache
        lines: List[Tuple[str, int]] = []
        body_width = max(10, width - 4)
        if capsule.system.strip():
            lines.append(("SYSTEM PROMPT", self._attr(PAIR_SYSTEM, curses.A_BOLD)))
            for line in wrap_text(capsule.system, body_width):
                lines.append(("  " + line, self._attr(PAIR_SYSTEM)))
            lines.append(("", 0))
        for message in capsule.messages:
            if message.role == "user":
                label = "YOU"
                attr = self._attr(PAIR_USER, curses.A_BOLD)
                body_attr = self._attr(PAIR_USER)
            elif message.role == "assistant":
                label = "ASSISTANT"
                attr = self._attr(PAIR_ASSISTANT, curses.A_BOLD)
                body_attr = self._attr(PAIR_ASSISTANT)
            else:
                label = "SYSTEM"
                attr = self._attr(PAIR_SYSTEM, curses.A_BOLD)
                body_attr = self._attr(PAIR_SYSTEM)
            lines.append((label, attr))
            for line in wrap_text(message.content, body_width):
                lines.append(("  " + line, body_attr))
            lines.append(("", 0))
        while lines and not lines[-1][0].strip():
            lines.pop()
        self._view_cache_key = key
        self._view_cache = lines
        return lines

    def _draw_viewer(self, height: int, width: int) -> None:
        capsule = self.viewing
        if capsule is None:
            return
        self._draw_header(height, width, capsule.title)
        source = capsule.provider or "-"
        if capsule.model:
            source += f"/{capsule.model}"
        meta = (
            f" {source} · {len(capsule.messages)} pesan · ~{capsule.tokens()} token · "
            f"{fmt_time(capsule.created_at)}"
        )
        if capsule.source_url and width > 90:
            meta += f" · {capsule.source_url}"
        self.put(1, 0, fit_text(meta, width), self._attr(PAIR_DIM, curses.A_DIM))
        top = 2
        view_height = max(1, height - 4)
        lines = self._view_lines(width)
        max_scroll = max(0, len(lines) - view_height)
        self.view_scroll = max(0, min(self.view_scroll, max_scroll))
        for index, (text, attr) in enumerate(lines[self.view_scroll : self.view_scroll + view_height]):
            self.put(top + index, 0, text, attr)
        status_row = height - 2
        self.fill(status_row, 0, 1, width, self._attr(PAIR_DIM))
        if self.status:
            marker = "!" if self.status_error else "·"
            text = f" {marker} {self.status}"
            attr = self._attr(PAIR_ERROR if self.status_error else PAIR_DIM, curses.A_BOLD)
        else:
            position = f"baris {self.view_scroll + 1}/{max(1, len(lines))}"
            text = f" {position} · target {target_label(self.target)} · {capsule.id}"
            attr = self._attr(PAIR_DIM)
        self.put(status_row, 0, fit_text(text, width), attr)
        hint = "Esc kembali · s bagikan · c salin prompt · t target · e ekspor · o sumber · d hapus · ? bantuan"
        if width < 76:
            hint = "Esc kembali · s share · c prompt · t target · e ekspor · ? help"
        self.put(height - 1, 0, fit_text(hint, width), self._attr(PAIR_DIM, curses.A_DIM))

    # -- keys --------------------------------------------------------------

    def handle_key(self, key: Any) -> None:
        if isinstance(key, int):
            if key == curses.KEY_RESIZE:
                self._view_cache_key = None
                return
            if key in (curses.KEY_F1,):
                self.show_help()
                return
            if key in (curses.KEY_F10,):
                self.quit()
                return
            if key in (curses.KEY_UP,):
                self.move(-1)
                return
            if key in (curses.KEY_DOWN,):
                self.move(1)
                return
            if key in (curses.KEY_PPAGE,):
                self.page(-1)
                return
            if key in (curses.KEY_NPAGE,):
                self.page(1)
                return
            if key in (curses.KEY_ENTER,):
                self.activate()
                return
            if key in (curses.KEY_HOME,):
                self.home()
                return
            if key in (curses.KEY_END,):
                self.end()
                return
            return
        if key in ("\x11", "\x03"):
            self.quit()
            return
        if key == "\x0c":
            try:
                self.stdscr.clear()
            except curses.error:
                pass
            return
        if _is_escape(key):
            if self.viewing is not None:
                self.viewing = None
                self._view_cache_key = None
                self.set_status("", ttl=0)
            else:
                self.set_status("Tekan q untuk keluar atau ? untuk bantuan.", ttl=4)
            return
        if self.viewing is not None:
            self._viewer_command(key)
        else:
            self._library_command(key)

    def _library_command(self, key: str) -> None:
        lower = key.lower()
        if lower == "q":
            self.quit()
        elif lower == "j":
            self.move(1)
        elif lower == "k":
            self.move(-1)
        elif lower == "g":
            self.selected = 0
        elif lower == "G":
            self.selected = max(0, len(self.rows) - 1)
        elif lower in ("i",):
            self.import_flow()
        elif lower in ("p",):
            self.paste_flow()
        elif lower in ("s",):
            self.share_selected()
        elif lower in ("d",):
            self.delete_selected()
        elif lower in ("r",):
            self.refresh_list()
            self.set_status("Library dimuat ulang.", ttl=3)
        elif lower in ("/",):
            self.search_flow()
        elif lower == "x" and self.query:
            self.query = ""
            self.refresh_list()
            self.set_status("Filter dihapus.", ttl=3)
        elif lower == "o":
            self.open_selected_source()
        elif lower in ("?", "h"):
            self.show_help()
        elif lower in ("v",):
            self.show_server()
        elif key == "\n" or key == "\r":
            self.activate()

    def _viewer_command(self, key: str) -> None:
        lower = key.lower()
        if lower in ("q",):
            self.viewing = None
            self._view_cache_key = None
            self.set_status("", ttl=0)
        elif lower == "j":
            self.view_scroll += 1
        elif lower == "k":
            self.view_scroll = max(0, self.view_scroll - 1)
        elif lower == "g":
            self.view_scroll = 0
        elif lower == "G":
            lines = self._view_lines(self.stdscr.getmaxyx()[1] if self.stdscr else 80)
            view = max(1, (self.stdscr.getmaxyx()[0] if self.stdscr else 24) - 4)
            self.view_scroll = max(0, len(lines) - view)
        elif lower in ("s",):
            self.share_current()
        elif lower in ("c",):
            self.copy_current_prompt()
        elif lower in ("t",):
            self.choose_target()
        elif lower in ("e",):
            self.export_current()
        elif lower in ("o",):
            self.open_current_source()
        elif lower in ("d",):
            self.delete_current_view()
        elif lower in ("?", "h"):
            self.show_help()
        elif key == "\n" or key == "\r":
            return

    def move(self, delta: int) -> None:
        if self.viewing is not None:
            self.view_scroll = max(0, self.view_scroll + delta)
            return
        if not self.rows:
            return
        self.selected = max(0, min(len(self.rows) - 1, self.selected + delta))

    def page(self, direction: int) -> None:
        step = max(2, (self.stdscr.getmaxyx()[0] - 4) if self.stdscr else 10)
        if self.viewing is not None:
            self.view_scroll = max(0, self.view_scroll + direction * step)
        else:
            self.move(direction * step)

    def home(self) -> None:
        if self.viewing is not None:
            self.view_scroll = 0
        else:
            self.selected = 0

    def end(self) -> None:
        if self.viewing is not None:
            lines = self._view_lines(self.stdscr.getmaxyx()[1] if self.stdscr else 80)
            self.view_scroll = max(0, len(lines) - 5)
        else:
            self.selected = max(0, len(self.rows) - 1)

    def activate(self) -> None:
        if self.viewing is not None:
            return
        row = self.current_row()
        if row is None:
            self.import_flow()
            return
        try:
            capsule = self.store.get_capsule(str(row["id"]))
        except CtxshareError as exc:
            self.set_status(str(exc), error=True)
            return
        self.open_viewer(capsule)

    # -- actions -----------------------------------------------------------

    def import_flow(self) -> None:
        source = PromptDialog(
            self,
            "Impor context",
            "",
            label="Link share ChatGPT/Claude/Gemini, gist, path file, kode ctx1, atau token:",
        ).run()
        if not source:
            return
        job = JobDialog(
            self,
            "Mengambil context…",
            lambda: resolve_input(str(source), self.store),
        )
        capsules = job.run()
        if job.error:
            TextDialog(
                self,
                "Gagal impor",
                _error_lines(job.error),
            ).run()
            return
        if capsules:
            self.preview_and_save(capsules)

    def paste_flow(self) -> None:
        text = ""
        try:
            text = clipboard_text()
        except CtxshareError:
            text = ""
        if text.strip():
            single = text.strip()
            if "\n" not in single and (
                single.startswith(("http://", "https://", "ctx1."))
                or Path(os.path.expanduser(single)).is_file()
            ):
                job = JobDialog(self, "Mengambil context…", lambda: resolve_input(single, self.store))
                capsules = job.run()
                if job.error:
                    TextDialog(self, "Gagal impor", _error_lines(job.error)).run()
                elif capsules:
                    self.preview_and_save(capsules)
                return
            try:
                capsule = parse_transcript(text, origin="paste")
            except CtxshareError as exc:
                TextDialog(self, "Gagal membaca transkrip", _error_lines(exc)).run()
                return
            self.preview_and_save([capsule])
            return
        try:
            text = self.edit_text_in_editor()
        except CtxshareError as exc:
            self.set_status(str(exc), error=True)
            return
        if not text.strip():
            return
        try:
            capsule = parse_transcript(text, origin="paste")
        except CtxshareError as exc:
            TextDialog(self, "Gagal membaca transkrip", _error_lines(exc)).run()
            return
        self.preview_and_save([capsule])

    def edit_text_in_editor(self) -> str:
        command = _editor_command()
        fd, path_str = tempfile.mkstemp(prefix="ctxshare-paste-", suffix=".md")
        path = Path(path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(PASTE_TEMPLATE)
            curses.def_prog_mode()
            curses.endwin()
            try:
                subprocess.call(command + [str(path)])
            finally:
                curses.reset_prog_mode()
                try:
                    self.stdscr.clear()
                    self.stdscr.refresh()
                except curses.error:
                    pass
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.set_status(f"Gagal membuka editor: {exc}", error=True)
            return ""
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        if text.strip() == PASTE_TEMPLATE.strip():
            self.set_status("Transkrip masih kosong.", error=True)
            return ""
        return text

    def preview_and_save(self, capsules: List[Capsule]) -> None:
        if not capsules:
            return
        capsule = capsules[0]
        if len(capsules) > 1:
            items = []
            for index, item in enumerate(capsules, 1):
                source = f"{item.provider}/{item.model}" if item.model else item.provider
                items.append(
                    (
                        f"{index:>2}. {item.title}",
                        index - 1,
                        f"{source} · {len(item.messages)} pesan · ~{item.tokens()} token",
                    )
                )
            choice = ListDialog(
                self,
                f"{len(capsules)} percakapan ditemukan",
                items,
                footer="Enter pilih · Esc batal",
            ).run()
            if choice is None:
                return
            capsule = capsules[int(choice)]
        preview = PreviewDialog(self, capsule).run()
        if preview is None:
            return
        try:
            capsule_id = self.store.save_capsule(capsule)
        except CtxshareError as exc:
            self.set_status(str(exc), error=True)
            return
        self.query = ""
        self.refresh_list()
        for index, row in enumerate(self.rows):
            if row["id"] == capsule_id:
                self.selected = index
                break
        self.set_status(f"Tersimpan: {capsule_id} — {capsule.title}", ttl=6)

    def share_selected(self) -> None:
        row = self.current_row()
        if row is None:
            self.set_status("Belum ada context untuk dibagikan.", error=True)
            return
        try:
            capsule = self.store.get_capsule(str(row["id"]))
        except CtxshareError as exc:
            self.set_status(str(exc), error=True)
            return
        self.share_capsule(capsule)

    def share_current(self) -> None:
        if self.viewing is not None:
            self.share_capsule(self.viewing)

    def share_capsule(self, capsule: Capsule) -> None:
        try:
            ShareDialog(self, capsule).run()
        except CtxshareError as exc:
            self.set_status(str(exc), error=True)
        self.refresh_list()

    def delete_selected(self) -> None:
        row = self.current_row()
        if row is None:
            return
        if not ConfirmDialog(self, f"Hapus '{row['title']}' dari library?").run():
            return
        try:
            self.store.delete_capsule(str(row["id"]))
        except CtxshareError as exc:
            self.set_status(str(exc), error=True)
            return
        self.refresh_list()
        self.set_status("Capsule dihapus.", ttl=4)

    def delete_current_view(self) -> None:
        capsule = self.viewing
        if capsule is None:
            return
        if not ConfirmDialog(self, f"Hapus '{capsule.title}' dari library?").run():
            return
        try:
            self.store.delete_capsule(capsule.id)
        except CtxshareError as exc:
            self.set_status(str(exc), error=True)
            return
        self.viewing = None
        self._view_cache_key = None
        self.refresh_list()
        self.set_status("Capsule dihapus.", ttl=4)

    def copy_current_prompt(self) -> None:
        capsule = self.viewing
        if capsule is None:
            return
        self.copy(capsule.handoff_prompt(self.target), f"Prompt {target_label(self.target)} disalin")

    def choose_target(self) -> None:
        items = [
            (f"{'●' if key == self.target else ' '} {TARGETS[key]['label']}", key, TARGETS[key]["hint"])
            for key in ("chatgpt", "claude", "gemini", "generic")
        ]
        selected = ListDialog(self, "Target handoff", items, footer="Enter pilih · Esc batal").run()
        if not selected:
            return
        self.target = str(selected)
        self.config.setdefault("share", {})["target"] = self.target
        try:
            save_config(self.config)
        except CtxshareError:
            pass
        self.set_status(f"Target: {target_label(self.target)}", ttl=4)

    def export_current(self) -> None:
        capsule = self.viewing
        if capsule is None:
            return
        folder = data_dir() / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (
            f"{safe_filename(capsule.title, capsule.id)}-{target_label(self.target).lower()}-"
            f"{unique_suffix()}.md"
        )
        path.write_text(capsule.to_markdown() + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self.set_status(f"Diekspor ke {path}", ttl=8)

    def open_selected_source(self) -> None:
        row = self.current_row()
        if row is None:
            return
        url = str(row.get("source_url") or "")
        if not url:
            self.set_status("Capsule ini tidak punya URL sumber.", error=True)
            return
        self._open_url(url)

    def open_current_source(self) -> None:
        if self.viewing is None:
            return
        if not self.viewing.source_url:
            self.set_status("Capsule ini tidak punya URL sumber.", error=True)
            return
        self._open_url(self.viewing.source_url)

    def _open_url(self, url: str) -> None:
        try:
            mechanism = open_url(url)
            self.set_status(f"Membuka {url} via {mechanism}", ttl=5)
        except CtxshareError as exc:
            self.set_status(str(exc), error=True)

    def copy(self, text: str, label: str = "Disalin") -> None:
        try:
            mechanism = copy_to_clipboard(text)
            self.set_status(f"{label} ({mechanism}).", ttl=5)
        except CtxshareError as exc:
            self.set_status(str(exc), error=True)

    def search_flow(self) -> None:
        query = PromptDialog(
            self,
            "Cari library",
            self.query,
            label="Kata kunci judul/provider/model (kosong = semua):",
        ).run()
        if query is None:
            return
        self.query = query
        self.selected = 0
        self.list_offset = 0
        self.refresh_list()
        self.set_status(
            f"{len(self.rows)} hasil" + (f" untuk '{query}'" if query else ""),
            ttl=4,
        )

    def show_server(self) -> None:
        server, error = self.ensure_server()
        if server is None:
            TextDialog(self, "Server link", [error or "Server tidak aktif."]).run()
            return
        stats = self.store.stats()
        lines = [
            f"Base URL : {server.base_url()}/",
            f"Bind     : {server.host}:{server.port}",
            f"Library  : {stats['capsules']} capsule",
            f"Shares   : {stats['shares']} link",
            "",
            "Link dibuat lewat s / Share. Server berhenti saat TUI ditutup.",
            "Untuk link publik: gunakan gist (g di dialog share), atau pasang",
            "tunnel lalu isi share.public_base_url di config.",
        ]
        TextDialog(self, "Server link", lines).run()

    def show_help(self) -> None:
        lines = [
            "ALUR HANDOFF",
            "  1. Di ChatGPT/Claude/Gemini: buat Share link percakapan.",
            "  2. Di ctxshare: i (impor link), atau p (tempel transkrip).",
            "  3. Pilih capsule → s → pilih target → c salin prompt.",
            "  4. Buka chatbot target, New chat, tempel prompt, lanjutkan.",
            "",
            "LIBRARY",
            "  Enter   buka transcript",
            "  i       impor link share / gist / file / kode ctx1 / token",
            "  p       tempel transkrip dari clipboard (fallback: editor)",
            "  s       bagikan sebagai link / gist / kode",
            "  d       hapus capsule",
            "  /       cari, x hapus filter",
            "  r       muat ulang, o buka URL sumber, v status server",
            "  q       keluar (Ctrl+Q juga)",
            "",
            "VIEWER",
            "  Esc     kembali · PgUp/PgDn scroll · g/G atas/bawah",
            "  s       link/gist · c salin prompt target · t ganti target",
            "  e       ekspor Markdown · o buka sumber · d hapus",
            "",
            "TARGET",
            "  ChatGPT (~128k token), Claude (~200k), Gemini (~1M).",
            "  Prompt dibuat berbeda untuk tiap target, termasuk instruksi",
            "  agar model melanjutkan dari pesan terakhir.",
            "",
            "FALLBACK",
            "  Kalau share link tidak bisa dibaca (login/format berubah),",
            "  buka di browser → salin transkrip → p. Format yang dikenali:",
            "  'You said:' / 'ChatGPT said:', 'Human:' / 'Assistant:',",
            "  heading Markdown, dan label provider.",
            "",
            "PRIVASI",
            "  Link LAN hidup selama TUI/server hidup. Gist bersifat secret",
            "  (tidak terdaftar) tetapi siapa pun dengan URL bisa membaca.",
        ]
        TextDialog(self, "Bantuan ctxshare", lines).run()

    def quit(self) -> None:
        self.running = False


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------


class Dialog:
    title = "Dialog"

    def __init__(self, app: TuiApp):
        self.app = app
        self.done = False
        self.result: Any = None

    def run(self) -> Any:
        while not self.done and self.app.running:
            self.app.draw()
            self.draw()
            key = self.app.getkey(80)
            if key is None:
                self.app.tick()
                self.tick()
                continue
            self.handle_key(key)
        self.app.tick()
        return self.result

    def tick(self) -> None:
        pass

    def handle_key(self, key: Any) -> None:
        if _is_escape(key):
            self.done = True

    def rect(self, width: int, height: int) -> Tuple[int, int, int, int]:
        try:
            screen_h, screen_w = self.app.stdscr.getmaxyx()
        except curses.error:
            screen_h, screen_w = 24, 80
        width = max(20, min(width, screen_w - 2))
        height = max(6, min(height, screen_h - 2))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        return x, y, width, height

    def frame(self, width: int, height: int, title: Optional[str] = None) -> Tuple[int, int, int, int]:
        x, y, width, height = self.rect(width, height)
        self.app.fill(y, x, height, width, 0)
        border = self.app._attr(PAIR_BORDER)
        top = "┌" + "─" * (width - 2) + "┐"
        bottom = "└" + "─" * (width - 2) + "┘"
        self.app.put(y, x, top, border)
        self.app.put(y + height - 1, x, bottom, border)
        for row in range(1, height - 1):
            self.app.put(y + row, x, "│", border)
            self.app.put(y + row, x + width - 1, "│", border)
        label = title if title is not None else self.title
        label = fit_text(f" {label} ", width - 4)
        self.app.put(y, x + 2, label, self.app._attr(PAIR_ACCENT, curses.A_BOLD))
        return x + 1, y + 1, width - 2, height - 2

    def draw(self) -> None:
        try:
            self.app.stdscr.refresh()
        except curses.error:
            pass


class ListDialog(Dialog):
    def __init__(
        self,
        app: TuiApp,
        title: str,
        items: Sequence[Tuple[str, Any, str]],
        footer: str = "Enter pilih · Esc tutup",
    ):
        super().__init__(app)
        self.title = title
        self.items = list(items)
        self.footer = footer
        self.selected = 0
        self.offset = 0

    def draw(self) -> None:
        longest = max((len(item[0]) + len(item[2]) + 8) for item in self.items) if self.items else 48
        width = min(84, max(48, longest))
        height = min(len(self.items) + 4, 18) if self.items else 7
        x, y, inner_w, inner_h = self.frame(width, height)
        if not self.items:
            self.app.put(y, x + 1, "Kosong.", self.app._attr(PAIR_DIM))
            self.app.put(y + inner_h - 1, x + 1, self.footer, self.app._attr(PAIR_DIM, curses.A_DIM))
            self._refresh()
            return
        visible = max(1, inner_h - 2)
        if self.selected < self.offset:
            self.offset = self.selected
        if self.selected >= self.offset + visible:
            self.offset = self.selected - visible + 1
        for row in range(visible):
            index = self.offset + row
            if index >= len(self.items):
                break
            label, _value, detail = self.items[index]
            marker = "›" if index == self.selected else " "
            line = f"{marker} {label}"
            attr = (
                self.app._attr(PAIR_ACCENT, curses.A_BOLD | curses.A_REVERSE)
                if index == self.selected
                else 0
            )
            self.app.put(y + row, x + 1, fit_text(line, inner_w - 2), attr)
            if detail and inner_w > len(line) + 6:
                detail_text = fit_text(detail, inner_w - len(line) - 3)
                self.app.put(
                    y + row,
                    x + 1 + len(line) + 1,
                    detail_text,
                    self.app._attr(PAIR_DIM, curses.A_DIM),
                )
        self.app.put(
            y + inner_h - 1,
            x + 1,
            fit_text(self.footer, inner_w - 1),
            self.app._attr(PAIR_DIM, curses.A_DIM),
        )
        self._refresh()

    def _refresh(self) -> None:
        try:
            self.app.stdscr.refresh()
        except curses.error:
            pass

    def handle_key(self, key: Any) -> None:
        if _is_escape(key):
            self.done = True
        elif isinstance(key, str) and key in ("\n", "\r"):
            if self.items:
                self.result = self.items[self.selected][1]
                self.done = True
        elif isinstance(key, int) and key in (curses.KEY_ENTER,):
            if self.items:
                self.result = self.items[self.selected][1]
                self.done = True
        elif key in (curses.KEY_UP, "k"):
            self.selected = max(0, self.selected - 1)
        elif key in (curses.KEY_DOWN, "j"):
            self.selected = min(len(self.items) - 1, self.selected + 1)
        elif key == curses.KEY_PPAGE:
            self.selected = max(0, self.selected - 8)
        elif key == curses.KEY_NPAGE:
            self.selected = min(len(self.items) - 1, self.selected + 8)
        elif key in (curses.KEY_HOME, "g"):
            self.selected = 0
        elif key in (curses.KEY_END, "G"):
            self.selected = max(0, len(self.items) - 1)


class PromptDialog(Dialog):
    def __init__(self, app: TuiApp, title: str, initial: str = "", label: str = ""):
        super().__init__(app)
        self.title = title
        self.label = label
        self.text = initial
        self.cursor = len(initial)

    def run(self) -> Any:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        return super().run()

    def draw(self) -> None:
        width = min(80, max(50, min(len(self.label) + 6, 80)))
        x, y, inner_w, inner_h = self.frame(width, 8)
        if self.label:
            self.app.put(
                y,
                x + 1,
                fit_text(self.label, inner_w - 2),
                self.app._attr(PAIR_DIM, curses.A_DIM),
            )
        input_y = y + 2 if self.label else y + 1
        self.app.put(input_y, x + 1, "› ", self.app._attr(PAIR_ACCENT, curses.A_BOLD))
        available = max(1, inner_w - 3)
        start = 0
        if self.cursor >= available:
            start = self.cursor - available + 1
        self.app.put(input_y, x + 3, self.text[start : start + available], 0)
        cursor_x = x + 3 + max(0, self.cursor - start)
        try:
            self.app.stdscr.move(input_y, min(cursor_x, x + inner_w - 1))
        except curses.error:
            pass
        self.app.put(
            y + inner_h - 1,
            x + 1,
            "Enter lanjut · Esc batal · Ctrl+U hapus baris",
            self.app._attr(PAIR_DIM, curses.A_DIM),
        )
        try:
            self.app.stdscr.refresh()
        except curses.error:
            pass

    def handle_key(self, key: Any) -> None:
        if _is_escape(key):
            self.done = True
            return
        if isinstance(key, str):
            if key in ("\n", "\r"):
                self.result = self.text.strip()
                self.done = True
            elif key in ("\x7f", "\b"):
                if self.cursor > 0:
                    self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
                    self.cursor -= 1
            elif key == "\x15":
                self.text = ""
                self.cursor = 0
            elif key == "\x17":
                start = self.cursor
                while start > 0 and self.text[start - 1].isspace():
                    start -= 1
                while start > 0 and not self.text[start - 1].isspace():
                    start -= 1
                self.text = self.text[:start] + self.text[self.cursor :]
                self.cursor = start
            elif key == "\x01":
                self.cursor = 0
            elif key == "\x05":
                self.cursor = len(self.text)
            elif key.isprintable():
                self.text = self.text[: self.cursor] + key + self.text[self.cursor :]
                self.cursor += 1
        elif key == curses.KEY_ENTER:
            self.result = self.text.strip()
            self.done = True
        elif key == curses.KEY_BACKSPACE:
            if self.cursor > 0:
                self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
                self.cursor -= 1
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self.cursor = len(self.text)
        elif key == curses.KEY_DC:
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]


class ConfirmDialog(Dialog):
    def __init__(self, app: TuiApp, message: str):
        super().__init__(app)
        self.message = message

    def draw(self) -> None:
        width = min(72, max(44, min(len(self.message) + 4, 72)))
        lines = wrap_text(self.message, width - 4)
        x, y, inner_w, inner_h = self.frame(width, min(len(lines) + 6, 12))
        for index, line in enumerate(lines[: inner_h - 3]):
            self.app.put(y + index, x + 1, line, self.app._attr(PAIR_SYSTEM, curses.A_BOLD))
        self.app.put(
            y + inner_h - 1,
            x + 1,
            "y = ya · n/Esc = batal",
            self.app._attr(PAIR_DIM, curses.A_DIM),
        )
        try:
            self.app.stdscr.refresh()
        except curses.error:
            pass

    def handle_key(self, key: Any) -> None:
        if _is_escape(key):
            self.result = False
            self.done = True
        elif isinstance(key, str) and key.lower() == "y":
            self.result = True
            self.done = True
        elif isinstance(key, str) and key.lower() == "n":
            self.result = False
            self.done = True
        elif isinstance(key, str) and key in ("\n", "\r"):
            self.result = False
            self.done = True
        elif isinstance(key, int) and key == curses.KEY_ENTER:
            self.result = False
            self.done = True


class TextDialog(Dialog):
    def __init__(self, app: TuiApp, title: str, lines: Sequence[str]):
        super().__init__(app)
        self.title = title
        self.raw_lines = list(lines)
        self.scroll = 0

    def draw(self) -> None:
        try:
            screen_h, screen_w = self.app.stdscr.getmaxyx()
        except curses.error:
            screen_h, screen_w = 24, 80
        width = min(84, screen_w - 2)
        wrapped: List[str] = []
        for line in self.raw_lines:
            if not line:
                wrapped.append("")
            else:
                wrapped.extend(wrap_text(line, max(10, width - 4)))
        height = min(len(wrapped) + 4, screen_h - 2)
        x, y, inner_w, inner_h = self.frame(width, height)
        view = inner_h - 2
        self.scroll = max(0, min(self.scroll, max(0, len(wrapped) - view)))
        for index, line in enumerate(wrapped[self.scroll : self.scroll + view]):
            self.app.put(y + index, x + 1, fit_text(line, inner_w - 2), 0)
        footer = "Esc tutup"
        if len(wrapped) > view:
            footer = f"PgUp/PgDn · {self.scroll + 1}/{max(1, len(wrapped) - view + 1)} · Esc tutup"
        self.app.put(
            y + inner_h - 1,
            x + 1,
            fit_text(footer, inner_w - 1),
            self.app._attr(PAIR_DIM, curses.A_DIM),
        )
        try:
            self.app.stdscr.refresh()
        except curses.error:
            pass

    def handle_key(self, key: Any) -> None:
        if _is_escape(key) or key in ("q",):
            self.done = True
        elif key in (curses.KEY_PPAGE, curses.KEY_UP, "k"):
            self.scroll = max(0, self.scroll - 3)
        elif key in (curses.KEY_NPAGE, curses.KEY_DOWN, "j"):
            self.scroll += 3


class JobDialog(Dialog):
    def __init__(self, app: TuiApp, title: str, func: Callable[[], Any]):
        super().__init__(app)
        self.title = title
        self.func = func
        self.result_queue: "queue.Queue[Tuple[bool, Any]]" = queue.Queue()
        self.error: Optional[BaseException] = None
        self.value: Any = None
        self.started = time.time()
        threading.Thread(target=self._work, name="ctxshare-job", daemon=True).start()

    def _work(self) -> None:
        try:
            self.result_queue.put((True, self.func()))
        except BaseException as exc:  # noqa: BLE001 - shown to user
            self.result_queue.put((False, exc))

    def run(self) -> Any:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        while not self.done and self.app.running:
            self.app.draw()
            self.draw()
            key = self.app.getkey(80)
            if key is not None and _is_escape(key):
                self.done = True
                break
            self.tick()
            if self.done:
                break
        self.app.tick()
        return self.value

    def tick(self) -> None:
        try:
            ok, value = self.result_queue.get_nowait()
        except queue.Empty:
            return
        if ok:
            self.value = value
        else:
            self.error = value if isinstance(value, BaseException) else Exception(str(value))
        self.done = True

    def draw(self) -> None:
        elapsed = time.time() - self.started
        spinner = "|/-\\"[int(elapsed * 8) % 4]
        message = f"{spinner}  {self.title}  ({elapsed:.1f}s)"
        width = min(72, max(48, len(message) + 4))
        x, y, inner_w, inner_h = self.frame(width, 7)
        self.app.put(
            y + 1,
            x + 1,
            fit_text(message, inner_w - 2),
            self.app._attr(PAIR_ACCENT, curses.A_BOLD),
        )
        self.app.put(
            y + inner_h - 1,
            x + 1,
            "Esc membatalkan tampilan",
            self.app._attr(PAIR_DIM, curses.A_DIM),
        )
        try:
            self.app.stdscr.refresh()
        except curses.error:
            pass


class PreviewDialog(Dialog):
    def __init__(self, app: TuiApp, capsule: Capsule):
        super().__init__(app)
        self.title = "Pratinjau impor"
        self.capsule = capsule

    def draw(self) -> None:
        capsule = self.capsule
        try:
            screen_h, screen_w = self.app.stdscr.getmaxyx()
        except curses.error:
            screen_h, screen_w = 24, 80
        width = min(84, screen_w - 2)
        source = (capsule.provider or "-") + (f"/{capsule.model}" if capsule.model else "")
        first = next((m.content for m in capsule.messages if m.role == "user"), capsule.messages[0].content)
        last = capsule.messages[-1].content
        window = target_window(self.app.target)
        tokens = capsule.tokens()
        lines = [
            capsule.title,
            f"{len(capsule.messages)} pesan · ~{tokens} token · dari {source}",
            f"dibuat {fmt_time(capsule.created_at)} · target {target_label(self.app.target)}",
        ]
        if window and tokens > window * 0.8:
            lines.append(f"! Konteks ~{tokens} token, mendekati batas {target_label(self.app.target)}.")
        lines.extend(
            [
                "",
                "Pertama: " + " ".join(first.split())[:180],
                "Terakhir: " + " ".join(last.split())[:180],
                "",
                "Enter/s simpan · t ubah judul · Esc batal",
            ]
        )
        wrapped: List[str] = []
        for line in lines:
            wrapped.extend(wrap_text(line, width - 4) if line else [""])
        height = min(len(wrapped) + 4, screen_h - 2)
        x, y, inner_w, inner_h = self.frame(width, height)
        for index, line in enumerate(wrapped[: inner_h - 1]):
            attr = 0
            if index == 0:
                attr = self.app._attr(PAIR_ACCENT, curses.A_BOLD)
            elif line.startswith("!"):
                attr = self.app._attr(PAIR_ERROR, curses.A_BOLD)
            elif line.startswith(("Pertama:", "Terakhir:")):
                attr = self.app._attr(PAIR_DIM, curses.A_DIM)
            self.app.put(y + index, x + 1, fit_text(line, inner_w - 2), attr)
        try:
            self.app.stdscr.refresh()
        except curses.error:
            pass

    def handle_key(self, key: Any) -> None:
        if _is_escape(key):
            self.done = True
        elif isinstance(key, str) and key.lower() in ("\n", "\r", "s"):
            self.result = self.capsule
            self.done = True
        elif isinstance(key, int) and key == curses.KEY_ENTER:
            self.result = self.capsule
            self.done = True
        elif isinstance(key, str) and key.lower() == "t":
            title = PromptDialog(self.app, "Ubah judul", self.capsule.title, label="Judul:").run()
            if title:
                self.capsule.title = title


class ShareDialog(Dialog):
    def __init__(self, app: TuiApp, capsule: Capsule):
        super().__init__(app)
        self.title = "Bagikan context"
        self.capsule = capsule
        share_cfg = app.config.get("share") or {}
        ttl = float(share_cfg.get("ttl_hours") or 0)
        record = app.store.create_share(capsule.id, ttl_hours=ttl)
        self.token = str(record["token"])
        self.expires_at = record.get("expires_at")
        self.server, self.server_error = app.ensure_server()
        self.link = self.server.link(self.token) if self.server else ""
        self.gist_url = ""
        self.gist_error = ""
        self.uploading = False
        self.gist_queue: "queue.Queue[Tuple[bool, Any]]" = queue.Queue()
        self._code: Optional[str] = None

    def run(self) -> Any:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        return super().run()

    def start_gist(self) -> None:
        if self.uploading or self.gist_url:
            return
        self.uploading = True
        self.gist_error = ""
        capsule = self.capsule

        def work() -> None:
            try:
                self.gist_queue.put((True, create_gist(capsule)))
            except BaseException as exc:  # noqa: BLE001 - shown to user
                self.gist_queue.put((False, exc))

        threading.Thread(target=work, name="ctxshare-gist", daemon=True).start()
        self.app.set_status("Mengunggah secret gist…", ttl=0)

    def tick(self) -> None:
        try:
            ok, value = self.gist_queue.get_nowait()
        except queue.Empty:
            return
        self.uploading = False
        if ok:
            self.gist_url = str(value)
            self.app.set_status("Gist dibuat. Tekan l untuk menyalin link.", ttl=6)
        else:
            self.gist_error = str(value)
            self.app.set_status(f"Gist gagal: {value}", error=True, ttl=12)

    def draw(self) -> None:
        try:
            screen_h, screen_w = self.app.stdscr.getmaxyx()
        except curses.error:
            screen_h, screen_w = 24, 80
        width = min(84, screen_w - 2)
        expires = fmt_time(self.expires_at) if self.expires_at else "tidak kedaluwarsa"
        target = self.app.target
        window = target_window(target)
        tokens = self.capsule.tokens()
        lines: List[str] = [
            f"Capsule : {self.capsule.title}",
            f"Pesan   : {len(self.capsule.messages)} · ~{tokens} token · {expires}",
            "",
            f"Target prompt: {target_label(target)}  [1] ChatGPT  [2] Claude  [3] Gemini  [0] generic",
        ]
        if window:
            percent = int(tokens * 100 / window)
            lines.append(f"Estimasi context: {percent}% dari ~{fmt_tokens(window)} token.")
            if tokens > window * 0.8:
                lines.append("! Prompt mungkin terpotong oleh context window target.")
        lines.extend(
            [
                "",
                "Link LAN:",
            ]
        )
        if self.link:
            lines.append(f"  {self.link}")
            lines.append("  hidup selama server/TUI berjalan, hanya di jaringan ini")
        else:
            lines.append(f"  TIDAK AKTIF: {self.server_error or 'server belum jalan'}")
            lines.append("  r = coba nyalakan server lagi")
        lines.append("")
        lines.append("Link publik (secret gist GitHub, tidak terdaftar):")
        if self.uploading:
            lines.append("  mengunggah…")
        elif self.gist_url:
            lines.append(f"  {self.gist_url}")
        elif self.gist_error:
            lines.append(f"  gagal: {fit_text(self.gist_error, width - 6)}")
        else:
            lines.append("  g = unggah sekarang")
        lines.extend(
            [
                "",
                "c salin prompt target · l salin link · k kode offline · o buka target",
                "g gist · r server · 1/2/3/0 ganti target · Esc tutup",
                "PERINGATAN: siapa pun yang punya link dapat membaca percakapan ini.",
            ]
        )
        wrapped: List[str] = []
        for line in lines:
            wrapped.extend(wrap_text(line, width - 4) if line else [""])
        height = min(len(wrapped) + 4, screen_h - 2)
        x, y, inner_w, inner_h = self.frame(width, height)
        for index, line in enumerate(wrapped[: inner_h - 1]):
            attr = 0
            if index == 0:
                attr = self.app._attr(PAIR_ACCENT, curses.A_BOLD)
            elif line.startswith("Target"):
                attr = self.app._attr(PAIR_SYSTEM, curses.A_BOLD)
            elif line.startswith("Link"):
                attr = self.app._attr(PAIR_SYSTEM, curses.A_BOLD)
            elif "TIDAK AKTIF" in line or line.startswith("!") or line.startswith("  gagal:"):
                attr = self.app._attr(PAIR_ERROR, curses.A_BOLD)
            elif line.startswith("  http"):
                attr = self.app._attr(PAIR_ASSISTANT, curses.A_BOLD)
            elif line.startswith("PERINGATAN"):
                attr = self.app._attr(PAIR_ERROR)
            self.app.put(y + index, x + 1, fit_text(line, inner_w - 2), attr)
        try:
            self.app.stdscr.refresh()
        except curses.error:
            pass

    def _best_link(self) -> str:
        return self.gist_url or self.link

    def handle_key(self, key: Any) -> None:
        if _is_escape(key):
            self.done = True
            return
        if not isinstance(key, str):
            return
        lower = key.lower()
        if lower in ("1", "2", "3", "0"):
            self.app.target = {"1": "chatgpt", "2": "claude", "3": "gemini", "0": "generic"}[lower]
            self.app.config.setdefault("share", {})["target"] = self.app.target
            try:
                save_config(self.app.config)
            except CtxshareError:
                pass
            self.app.set_status(f"Target: {target_label(self.app.target)}", ttl=4)
        elif lower == "c":
            self.app.copy(
                self.capsule.handoff_prompt(self.app.target),
                f"Prompt {target_label(self.app.target)} disalin",
            )
        elif lower == "l":
            link = self._best_link()
            if link:
                self.app.copy(link, "Link disalin")
            else:
                self.app.set_status("Belum ada link. Tekan r atau g.", error=True)
        elif lower == "k":
            if self._code is None:
                self._code = self.capsule.encode_code()
            if len(self._code) > 120_000:
                self.app.set_status(
                    f"Kode terlalu besar ({len(self._code)} karakter); gunakan link.",
                    error=True,
                    ttl=8,
                )
            else:
                self.app.copy(self._code, f"Kode ctx1 disalin ({len(self._code)} karakter)")
        elif lower == "g":
            self.start_gist()
        elif lower == "r":
            self.server, self.server_error = self.app.ensure_server(retry=True)
            self.link = self.server.link(self.token) if self.server else ""
            if self.link:
                self.app.set_status("Server link aktif.", ttl=4)
            else:
                self.app.set_status(f"Server gagal: {self.server_error}", error=True)
        elif lower == "o":
            url = TARGETS[self.app.target].get("url")
            if url:
                self.app._open_url(url)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _editor_command() -> List[str]:
    configured = os.environ.get("EDITOR", "").strip()
    if configured:
        command = shlex.split(configured)
        if command and shutil.which(command[0]):
            return command
    for candidate in ("nano", "vi", "vim"):
        if shutil.which(candidate):
            return [candidate]
    raise CtxshareError("Editor tidak ditemukan. Set $EDITOR (mis. nano).")


def _error_lines(error: Any) -> List[str]:
    text = str(error) if error else "Terjadi kesalahan."
    return text.splitlines() or [text]
