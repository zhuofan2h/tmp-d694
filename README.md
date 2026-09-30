# tmp

Scratch repo. Auto-updated data files.

## Usage

Playlist (free channels, grouped by country):

```
https://iptv-api.zhuofan2h.workers.dev/playlist.m3u
```

Filtered:

```
https://iptv-api.zhuofan2h.workers.dev/m3u?country=ID
https://iptv-api.zhuofan2h.workers.dev/m3u?country=ID,MY&premium=1
https://iptv-api.zhuofan2h.workers.dev/m3u?premium=only      # hanya channel premium
https://iptv-api.zhuofan2h.workers.dev/m3u?search=hbo&premium=1
```

JSON:

- `GET /api/channels?country=ID&search=…&premium=only&limit=…`
- `GET /api/countries`
- `GET /api/stats`
- `GET /health`

Query parameters:

| Param | Values | Meaning |
|---|---|---|
| `country` | `ID`, `ID,MY` | filter kode negara |
| `search` | teks | cari nama channel |
| `premium` | `1` / `only` | `1` = semua, `only` = hanya premium |
| `free` | `0` | sama dengan `premium=1` (dipertahankan kompatibel) |
| `limit` | angka | batasi hasil; nilai tak valid diabaikan (bukan error) |
| `dead` | `1` | sertakan channel yang gagal health-check |

Web: `https://iptv-web.zhuofan2h.workers.dev/`

## Health check (jaminan playability)

`tools/health.py` berjalan setiap cron **setelah** `collect.py`:

1. Probe semua channel URL dengan header yang dideklarasikan.
2. Kalau gagal, coba varian perbaikan: timeout lebih lama, User-Agent
   VLC/ffmpeg, tanpa Referer, `http://` → `https://`.
3. Varian yang berhasil disimpan balik ke `hls`/`header_iptv`.
4. **Fase B (konfirmasi):** semua yang lolos fase A di-probe ulang dengan
   koneksi baru. Host flaky / kena rate-limit (jawab 200 sekali, reset
   berikutnya) gugur di sini dan tidak dipublikasikan.
5. Channel yang tetap gagal ditandai `dead: true` dan **dikeluarkan dari
   `playlist.m3u`**; begitu hidup lagi (lolos 2 probe), otomatis masuk
   kembali. `HEALTH_CONFIRM=0` menonaktifkan fase B.
6. **Fase C (verifikasi artefak):** `playlist.m3u` yang baru ditulis
   di-parse ulang dan **setiap entry di-probe lagi** (per-host, pelan).
   Entry yang gagal → channel dimatikan → playlist ditulis ulang →
   diulang sampai bersih (maks 3 putaran). Kalau belum bersih, script
   exit 1 dan CI membiarkan data lama tetap terpublish.
7. Hasilnya ditulis ke `data/health.json` dan `stats.json`
   (`playlist_verified: true/false`).

Konsekuensinya: **setiap entry di `playlist.m3u` sudah terverifikasi
mengembalikan stream valid pada saat publish** — diverifikasi dua kali
(fase B) dan ulang terhadap file hasil tulis (fase C). Channel
mati/geo-block tetap ada di `data/channels.json` (lihat `?dead=1`),
tidak dihapus.

Jalankan lokal:

```
pip install pycryptodome
python tools/collect.py && python tools/health.py
OUT_DIR=data python tools/health.py   # override: HEALTH_THREADS, HEALTH_TIMEOUT,
                                      # HEALTH_FAILS, HEALTH_PER_HOST,
                                      # HEALTH_CONFIRM, HEALTH_VERIFY_ROUNDS
```

`HEALTH_FAILS=2` bila ingin toleransi 2 run gagal berturut-turut sebelum
channel ditandai mati (default: 1 = jaminan ketat).

## Tests

```
python -m unittest discover -s tools -p 'test_*.py'              # 27 tests
node --test worker/test.mjs worker/integration.test.mjs          # 9 + 11 tests
```

Keduanya dijalankan oleh cron sebelum data dipublish.

## Player setup

| App | How |
|---|---|
| TiviMate | Settings → Playlists → Add → URL |
| IPTV Smarters | Add Playlist → M3U URL |
| VLC | Media → Open Network Stream |
| Kodi | PVR IPTV Simple Client → M3U URL |

## Notes

- Some streams need specific headers — already embedded as
  `#EXTVLCOPT`/`#KODIPROP` (User-Agent, Referer, Origin).
- Some streams are geo-restricted; the health check runs from the CI
  vantage point, so a channel may still work better from your country.
- `.mpd`/`.flv` formats need a capable player (Kodi, TiviMate, ExoPlayer
  apps). Browsers: `.m3u8` only.
- Data refreshed automatically every 30 minutes.

## Layout

```
data/        playlist + JSON (generated)
tools/       collector (collect.py) + health checker (health.py)
worker/      Cloudflare Worker API (+ unit tests)
web/         web UI  (mirrored in web-worker/public/)
web-worker/  static asset worker
```
