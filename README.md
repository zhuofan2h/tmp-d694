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
   - 403 yang badan errornya menyebut blokir negara otomatis diberi label
     `403-geo` (mis. CloudFront *"block access from your country"*).
   - Channel yang hanya hidup dengan header yang **tidak bisa dibawa baris
     M3U** (mis. `Cookie`) ditandai `no_playlist: true`: tetap hidup dan
     tampil di API, tetapi dikeluarkan dari `playlist.m3u`. Contoh nyata:
     Trans7/TransTV butuh `Cookie`.
6. **Fase C (verifikasi artefak):** `playlist.m3u` yang baru ditulis
   di-parse ulang dan **setiap entry di-probe lagi** (per-host, pelan,
   dengan satu retry tenang untuk menampung throttle). Entry yang gagal →
   channel dimatikan / ditandai `no_playlist` → playlist ditulis ulang →
   diulang **selalu diakhiri sebuah pass verifikasi** (maks 5 drop).
   Kalau belum bersih, script exit 1 dan CI membiarkan data lama tetap
   terpublish — tidak pernah mempublikasikan playlist yang gagal
   diverifikasi.
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
                                      # HEALTH_CONFIRM, HEALTH_VERIFY_ROUNDS,
                                      # HEALTH_GEO_MODE, HEALTH_MODE,
                                      # HEALTH_FRAGMENTS, HTTPS_PROXY
```

`HEALTH_FAILS=2` bila ingin toleransi 2 run gagal berturut-turut sebelum
channel ditandai mati (default: 1 = jaminan ketat).

## Multi-lokasi & geo-block

Cron menjalankan checker dari **beberapa lokasi** (matrix runner ubuntu /
windows / macos di `.github/workflows/cron.yml`):

- tiap runner probe dalam mode fragment (`HEALTH_MODE=fragment`,
  `HEALTH_FRAGMENT=…`, `HEALTH_VANTAGE=…`) dan mengunggah hasilnya;
- job `publish` menggabungkan semua fragment
  (`HEALTH_FRAGMENTS='frags/*.json'`) lalu membangun playlist + fase C;
- **aturan merge: channel HIDUP kalau satu lokasi saja bisa memutarnya** —
  geo-block di satu negara tidak menyembunyikannya dari negara yang bisa
  menonton.

`HEALTH_GEO_MODE` (default **`keep`**):

| Nilai | Perilaku |
|---|---|
| `keep` | `403-geo` tidak membunuh channel: tetap di playlist, ditandai `geo_limited: true`, fase C hanya menandainya |
| `drop` | mode ketat: geo diperlakukan seperti kegagalan lain |

Egress lain (mis. dari Indonesia/Malaysia) bisa dipakai lewat env standar
urllib: `HTTPS_PROXY=http://host:port` (simpan sebagai repo secret).

## Tests

```
python -m unittest discover -s tools -p 'test_*.py'              # 42 tests
node --test worker/test.mjs worker/integration.test.mjs          # 10 + 11 tests
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
- Some streams are geo-restricted. Under the default `HEALTH_GEO_MODE=keep`
  they stay in the playlist flagged `geo_limited: true` — playability from
  your country may differ from the checker's. `stats.json` shows the count
  and the mode.
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
