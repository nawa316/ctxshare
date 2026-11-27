# Contributing

Terima kasih sudah mau membantu ctxshare.

## Menjalankan

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q src
```

Tidak ada dependensi pihak ketiga; target minimum Python 3.9.

## Prinsip

- **Bukan chat client.** ctxshare hanya membaca share/export dan menyusun
  context capsule. Jangan menambahkan panggilan API model atau API key.
- **Selalu sediakan fallback.** Share page bisa berubah kapan saja. Parser
  baru harus punya jalur paste/export yang jelas.
- **Jangan simpan rahasia.** Tidak ada cookie/login; hanya konten yang memang
  sudah dibagikan publik oleh pengguna.
- **Jaga privasi.** Link server memakai token acak, tidak ada listing, dan
  default TTL 72 jam.

## Menambah parser provider

1. Tambah deteksi URL di `src/ctxshare/ingest.py` (`detect_provider`,
   `canonical_share_url`).
2. Implementasikan `fetch_<provider>()` dan parser murni
   `_parse_<provider>_data()` agar mudah diuji tanpa jaringan.
3. Tambahkan fixture sintetis di `tests/test_ingest.py`.
4. Perbarui README (sumber yang didukung + fallback).

## Pull request

- Sertakan test untuk perilaku baru.
- Jangan mengubah format `ctxshare.capsule/1` tanpa menaikkan versi format.
- Jelaskan perubahan perilaku di deskripsi PR.
