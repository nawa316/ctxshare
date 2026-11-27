# ctxshare

[![CI](https://github.com/nawa316/ctxshare/actions/workflows/ci.yml/badge.svg)](https://github.com/nawa316/ctxshare/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

Pindahkan **context window** dari satu chat ke chat lain — ChatGPT ↔ Claude ↔
Gemini — lewat **share link**, secret gist, atau kode offline. TUI kecil tanpa
API key, tanpa dependensi, cocok untuk Termux.

ctxshare **bukan chat client**. Ia mengambil percakapan yang sudah ada,
menyimpannya sebagai *capsule* portabel, lalu membuat prompt yang siap ditempel
ke chat baru di provider lain.

```text
ChatGPT share link ──┐
Claude share link ───┼──> ctxshare ──> capsule lokal ──> link / gist / kode
Gemini share link ───┤                                      │
paste / export ──────┘                                      ▼
                                              prompt siap tempel + link
```

## Kenapa

- Chat di ChatGPT sudah panjang, ingin dilanjutkan di Claude atau Gemini.
- Punya banyak chat terpisah dan ingin memindahkan konteksnya tanpa copy-paste
  manual satu per satu.
- Ingin mengirim konteks ke perangkat lain lewat link, bukan menyalin ribuan
  baris teks.
- Tidak mau menyimpan API key atau bergantung pada API model berbayar.

## Fitur

- **Impor share link**
  - ChatGPT: `chatgpt.com/share/...` (backend JSON + fallback HTML).
  - Claude: `claude.ai/share/...` (snapshot API).
  - Gemini: `gemini.google.com/share/...` (GraphQL web; *unofficial*).
- **Impor cadangan**: export `conversations.json` ChatGPT/Claude, halaman HTML
  yang disimpan, file `.md`/`.txt`, clipboard, editor, atau stdin.
- **Library lokal** (SQLite): daftar capsule, pencarian, viewer transkrip,
  hapus, ekspor.
- **Tiga cara berbagi**:
  - link LAN dari server bawaan (token acak, TTL, tanpa listing),
  - secret gist GitHub (link publik yang tidak terdaftar),
  - kode `ctx1.` terkompresi yang bisa ditempel di chat mana pun.
- **Prompt per target**: instruksi berbeda untuk ChatGPT, Claude, dan Gemini,
  termasuk estimasi pemakaian context window.
- **TUI mobile-friendly**: tombol utama, dialog, clipboard/editor fallback.
- **Nol dependensi**: Python stdlib + `gh` opsional untuk gist.

## Instalasi

### Termux / Android

```bash
pkg install python git gh
git clone https://github.com/nawa316/ctxshare.git ~/ctxshare
cd ~/ctxshare
python3 ./ctxshare
```

Kalau proyek ditaruh di sdcard (misalnya agar tidak memakai storage internal):

```bash
SDCARD=/storage/0000-0000/Android/data/com.termux/files
git clone https://github.com/nawa316/ctxshare.git "$SDCARD/ctxshare"
ln -sfn "$SDCARD/ctxshare" "$HOME/ctxshare"

# Android external storage biasanya noexec, jadi launcher di $PREFIX/bin
# memanggil interpreter Python (bukan mengeksekusi file di sdcard).
cat > "$PREFIX/bin/ctxshare" <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
exec python3 "$HOME/ctxshare/ctxshare" "$@"
SH
chmod +x "$PREFIX/bin/ctxshare"
```

Data tetap disimpan di storage internal yang aman:
`~/.local/share/ctxshare/` (dapat diubah dengan `CTXSHARE_DATA`).

### Desktop Linux/macOS

```bash
git clone https://github.com/nawa316/ctxshare.git
cd ctxshare
./ctxshare
```

Atau `pipx install .` (Python 3.9+). Tidak ada dependensi runtime.

## Mulai cepat

```bash
ctxshare doctor          # cek Python, jaringan, gh, clipboard, server, folder data

# Impor percakapan dari share link
ctxshare import https://chatgpt.com/share/6a3e...
ctxshare import https://claude.ai/share/d205...
ctxshare import https://gemini.google.com/share/5f23...

# Lihat library
ctxshare list
ctxshare show last
ctxshare show last --prompt --target claude

# Buat link LAN sementara + prompt
ctxshare share last --serve

# Atau unggah ke secret gist (butuh scope gist)
gh auth refresh -h github.com -s gist   # sekali saja
ctxshare share last --gist

# TUI
ctxshare
```

Alur lengkap:

1. Buka chat di ChatGPT/Claude/Gemini → **Share** → salin link.
2. `ctxshare` → `i` → tempel link.
3. Pilih capsule → `s` → pilih target `1/2/3` → `c` salin prompt.
4. Buka chatbot target → **New chat** → tempel prompt → lanjutkan percakapan.

Kalau share link tidak bisa dibaca (login, format berubah, rate limit), salin
seluruh transkrip dari browser lalu tekan `p` di TUI atau jalankan:

```bash
ctxshare paste            # coba clipboard, fallback ke $EDITOR
ctxshare import -         # tempel lewat stdin
ctxshare import chat.txt  # file transkrip
```

Format transkrip yang dikenali: `You said:` / `ChatGPT said:`, `Human:` /
`Assistant:`, heading Markdown, dan label provider (`Claude:`, `Gemini:`).
Tanpa label, seluruh teks disimpan sebagai satu pesan user.

## TUI

**Library**

| Tombol | Aksi |
| --- | --- |
| `Enter` | buka transkrip |
| `i` | impor link / gist / file / kode / token |
| `p` | tempel transkrip dari clipboard (fallback editor) |
| `s` | bagikan capsule terpilih |
| `d` | hapus capsule |
| `/` | cari, `x` hapus filter |
| `r` | muat ulang |
| `o` | buka URL sumber di browser |
| `v` | status server link |
| `?` / `F1` | bantuan |
| `q` / `Ctrl+Q` | keluar |

**Viewer**

| Tombol | Aksi |
| --- | --- |
| `Esc` / `q` | kembali ke library |
| `PgUp`/`PgDn`, `j`/`k` | scroll |
| `g` / `G` | atas / bawah |
| `s` | dialog share |
| `c` | salin prompt target |
| `t` | ganti target (ChatGPT/Claude/Gemini/generic) |
| `e` | ekspor Markdown ke folder data |
| `o` | buka URL sumber |
| `d` | hapus capsule |

Di dialog share: `1` ChatGPT, `2` Claude, `3` Gemini, `0` generic, `c` salin
prompt, `l` salin link, `k` kode offline, `g` gist, `r` nyalakan ulang server,
`o` buka browser.

## Perintah CLI

| Perintah | Fungsi |
| --- | --- |
| `ctxshare` / `ctxshare tui` | buka TUI |
| `ctxshare import <sumber>` | impor share URL, gist, file, kode `ctx1.`, token, atau `-` |
| `ctxshare paste` | impor dari clipboard/editor |
| `ctxshare list [kata]` | daftar capsule |
| `ctxshare show <id> [--prompt] [--target ...]` | tampilkan transkrip/prompt |
| `ctxshare share <id> [--serve] [--gist]` | buat link + token share |
| `ctxshare serve` | server link headless |
| `ctxshare export <id> --format md/json/prompt` | ekspor |
| `ctxshare delete <id>` | hapus |
| `ctxshare config --show` | lihat config |
| `ctxshare doctor` | diagnosa |

Opsi impor penting:

```bash
ctxshare import conversations.json --list     # lihat isi export
ctxshare import conversations.json --index 2  # pilih percakapan ke-2
ctxshare import conversations.json --all      # impor semua
ctxshare import --clipboard                   # dari clipboard
ctxshare import --paste                       # buka $EDITOR
ctxshare import - --title "Riset kopi"        # stdin
```

Opsi share penting:

```bash
ctxshare share last --print link       # hanya URL
ctxshare share last --print code       # kode ctx1...
ctxshare share last --print prompt     # prompt siap tempel
ctxshare share last --print json       # metadata untuk automation
ctxshare share last --ttl 24           # link berlaku 24 jam
ctxshare share last --serve --port 8787
ctxshare share last --gist --target claude
ctxshare share last --open             # buka chat target di browser
```

## Sumber impor & fallback

| Sumber | Otomatis | Catatan |
| --- | --- | --- |
| ChatGPT share | ya | JSON `backend-api/share`; fallback decode React Router dari HTML |
| Claude share | ya | `api/chat_snapshots` dengan header browser; bisa 403/429 |
| Gemini share | ya | GraphQL web tidak resmi; signature query diperbarui otomatis bila berubah |
| ChatGPT export | ya | `conversations.json`, pilih `--index`/`--all` |
| Claude export | ya | `conversations.json` dengan `chat_messages` |
| Halaman HTML | ya | DOM yang sudah dirender (atribut `data-message-author-role`, `font-claude-message`, `<user-query>`) |
| Teks tempel | ya | label peran atau satu pesan user |
| PDF/gambar | tidak | konversi dulu ke teks |

Kalau Gemini mengubah query internalnya, ctxshare mencoba membaca signature
baru dari bundle web. Kalau tetap gagal, gunakan fallback salin transkrip.

## Mode berbagi

### Link LAN

TUI menyalakan server otomatis di port `8787` (fallback ke port berikutnya).
`ctxshare share --serve` melakukan hal yang sama dari CLI.

- Token acak ~144-bit; tidak ada endpoint daftar capsule.
- Default TTL 72 jam (`share.ttl_hours`, bisa `--ttl 0` untuk selamanya).
- Link hanya hidup selama server/TUI berjalan dan dapat diakses di jaringan
  yang sama.
- Halaman link menampilkan transkrip, tombol salin prompt per provider,
  download `.txt`, dan download capsule `.json`.

Untuk link publik tanpa gist, pasang tunnel (cloudflared/ngrok) lalu isi
`share.public_base_url` di config. Server tidak melakukan autentikasi selain
token, jadi jangan ekspos ke internet tanpa pemahaman risikonya.

### Secret gist

```bash
gh auth refresh -h github.com -s gist
ctxshare share last --gist
```

Gist berisi `README.md` (transkrip) dan `capsule.json` (mesin). Gist "secret"
tidak muncul di pencarian, tetapi **siapa pun yang memiliki URL dapat
membacanya**. Hapus dengan `gh gist delete <id>` atau dari GitHub.

### Kode offline

```bash
ctxshare share last --print code
```

Kode `ctx1.` adalah JSON capsule + checksum yang dikompres zlib dan di-encode
base64url. Bisa ditempel ke chat/email, lalu diimpor dengan:

```bash
ctxshare import 'ctx1....'
```

### File

```bash
ctxshare export last --format json -o capsule.json
ctxshare export last --format prompt --target gemini -o prompt.txt
```

## Prompt & context window

`ctxshare show --prompt` menghasilkan satu pesan yang bisa langsung ditempel:

- menyatakan provider asal dan target,
- menjelaskan bahwa transkrip adalah konteks, bukan permintaan baru,
- menyertakan system prompt bila ada,
- meminta model menjawab pertanyaan terakhir yang belum dijawab, atau
  membalas “Konteks diterima.” lalu menunggu pesan berikutnya.

Estimasi context window target (perkiraan, bisa berbeda per model/plan):

| Target | Perkiraan |
| --- | --- |
| ChatGPT | ~128k token |
| Claude | ~200k token |
| Gemini | ~1M token |

Kalau prompt terlalu besar, atur `import.max_chars` atau pakai
`ctxshare export --max-chars`. Pesan terbaru dipertahankan; pesan lama dipotong
dengan catatan.

## Config

`~/.config/ctxshare/config.json` (permission `600`):

```json
{
  "version": 1,
  "share": {
    "host": "0.0.0.0",
    "port": 8787,
    "auto_serve": true,
    "ttl_hours": 72,
    "public_base_url": "",
    "target": "chatgpt"
  },
  "fetch": { "timeout": 45 },
  "import": { "max_chars": 0 }
}
```

- `share.auto_serve`: TUI menyalakan server link otomatis.
- `share.public_base_url`: base URL tunnel, mis. `https://abc.trycloudflare.com`.
- `import.max_chars`: potong transkrip saat impor (0 = tanpa potong).
- `CTXSHARE_DATA`: folder database/log (default `~/.local/share/ctxshare`).
- `CTXSHARE_CONFIG`: path config alternatif.

## Privasi & keamanan

- ctxshare hanya membaca halaman share yang memang dapat diakses publik; tidak
  memakai cookie, login, atau API key.
- Jangan mengimpor percakapan yang tidak berhak kamu bagikan.
- Link LAN/secret gist/kode `ctx1.` memberi akses baca penuh ke transkrip.
  Perlakukan seperti dokumen sensitif.
- Server lokal tidak punya login; token adalah satu-satunya rahasia. Jalankan
  hanya di jaringan tepercaya, pakai TTL, dan tutup TUI/server setelah selesai.
- Semua data tersimpan lokal di SQLite; tidak ada telemetry.
- Parser Gemini memakai endpoint web tidak resmi dan dapat berubah sewaktu-waktu.
  Gunakan fallback paste/export bila perlu.

## Troubleshooting

**`bad interpreter: Permission denied` di Termux/sdcard**  
External storage Android biasanya `noexec`. Gunakan wrapper di `$PREFIX/bin`
seperti pada bagian instalasi, atau jalankan `python3 ~/ctxshare/ctxshare`.

**`HTTP 403` / `HTTP 429` saat impor Claude/Gemini**  
Share page sedang membatasi akses. Buka link di browser, salin transkrip, lalu
`ctxshare paste`.

**`This API operation needs the "gist" scope`**  
Jalankan `gh auth refresh -h github.com -s gist`, lalu coba `--gist` lagi.

**`Clipboard tidak tersedia`**  
Pasang Termux:API (`pkg install termux-api` + app Termux:API), atau gunakan
`ctxshare paste --editor`, `ctxshare import file.txt`, atau `ctxshare import -`.

**`Terminal terlalu kecil`**  
Perbesar jendela Termux (minimal ~24 kolom × 8 baris).

**Link LAN tidak bisa dibuka dari HP lain**  
Pastikan satu jaringan, port tidak diblokir, dan HP tidak memakai hotspot
dengan client isolation. Cek `ctxshare doctor` atau `v` di TUI.

**Prompt terpotong di chatbot target**  
Kurangi `import.max_chars`, pakai `--max-chars`, atau pilih Gemini yang context
window-nya lebih besar. Ingat batas context window adalah perkiraan.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q src
```

Struktur:

```text
ctxshare
├── src/ctxshare
│   ├── capsule.py   format capsule + kode ctx1 + prompt target
│   ├── ingest.py    parser ChatGPT/Claude/Gemini, HTML, export, transkrip
│   ├── store.py     SQLite library + share token
│   ├── share.py     gist, resolve link/kode/file
│   ├── server.py    server link + halaman HTML
│   ├── tui.py       antarmuka curses
│   ├── cli.py       perintah CLI
│   ├── config.py    config
│   └── util.py      path, HTTP, clipboard, format
├── tests
└── .github/workflows/ci.yml
```

Tes jaringan tidak dijalankan di CI (hanya parser dengan fixture). Untuk uji
live secara manual:

```bash
ctxshare import https://chatgpt.com/share/...
ctxshare import https://claude.ai/share/...
ctxshare import https://gemini.google.com/share/...
```

## Lisensi

MIT © 2026 [nawa316](https://github.com/nawa316)
