# tmp

Scratch repo. Auto-updated data files.

## Usage

### Playlist untuk player (pilih gaya yang cocok)

Dua file, karena player berbeda cara membaca header stream:

| Gaya | File / URL | Untuk player |
|---|---|---|
| **VLC/Kodi** (tag `#EXTVLCOPT`/`#KODIPROP`) | `playlist.m3u` | VLC, Kodi, player desktop |
| **Pipe Android** (`URL\|User-Agent=…&Referer=…`) | `playlist-pipe.m3u` | OTT TV, Televizo, OTT Player, TiviMate, IPTV Smarters, OTT Navigator |

```
# via Worker (edge cache 15 menit)
https://iptv-api.zhuofan2h.workers.dev/playlist.m3u
https://iptv-api.zhuofan2h.workers.dev/playlist-pipe.m3u

# langsung dari repo (tanpa cache; dipakai bila workers.dev diblokir)
https://raw.githubusercontent.com/zhuofan2h/tmp-d694/main/data/playlist.m3u
https://raw.githubusercontent.com/zhuofan2h/tmp-d694/main/data/playlist-pipe.m3u
https://cdn.jsdelivr.net/gh/zhuofan2h/tmp-d694@main/data/playlist-pipe.m3u
```

Catatan player:

- **VLC tidak bisa** membaca `URL|…`, dan **player Android biasanya tidak
  membaca** `#EXTVLCOPT` — sebabnya ada dua file. Pakai file sesuai player.
- Channel yang butuh header tambahan (mis. `Cookie` Trans7/TransTV) hanya
  ada di file **pipe** (ditandai `pipe_only` di API) — di file VLC memang
  tidak akan bisa jalan.
- Channel ber-**DRM** (`ContentProtection` di manifest-nya) **tidak
  dipublish**: VLC/Televizo/OTT TV menolaknya, jadi kalau tetap dimasukkan
  justru muncul error. Ditandai `drm: true` di `?dead=1`/API.
- **Grup `Indonesia`** berisi SEMUA channel Indonesia: kode `ID` +
  jaringan `TVRI` + `TV Lokal` (dulu terpecah jadi 3 folder).
- Folder berakhiran **`(geo)`** = channel geo-restricted yang dari IP
  pengece selalu 403. Tidak dibuang (mungkin hidup dari jaringan yang
  ditujunya), tapi dipisah supaya folder utama **100% terverifikasi** —
  buka `Indonesia (geo)` kalau di jaringanmu channelnya jalan.

Filtered:

```
https://iptv-api.zhuofan2h.workers.dev/m3u?country=ID
https://iptv-api.zhuofan2h.workers.dev/m3u?country=ID&style=pipe
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
   - **Setiap 403 tidak membunuh channel** (`HEALTH_GEO_MODE=keep`): 403
     hanya berarti *"tidak boleh dari IP checker"* — penonton lah yang
     memutuskan. Channel ditandai `geo_limited: true`; `geo_status` berisi
     `403-geo` bila badan error eksplisit menyebut blokir negara (mis.
     CloudFront *"block access from your country"*), atau `403-blocked`
     untuk 403 generik.
   - Header yang **hanya bisa dibawa gaya pipe** (mis. `Cookie`) → channel
     ditandai `pipe_only: true`: keluar dari `playlist.m3u`, **tetap ada**
     di `playlist-pipe.m3u`. Contoh nyata: Trans7/TransTV butuh `Cookie`
     → kembali muncul untuk player Android.
   - Header yang **tidak bisa dibawa gaya mana pun** (nilainya merusak
     grammar `|a=b&c=d`) → `no_playlist: true`, keluar dari kedua file.
   - **DRM** (`drm_check` membaca manifest; ada `ContentProtection`) →
     `drm: true`, tidak dipublish di file mana pun (player biasa tetap
     menolaknya — memasukkan = error di layar). Tetap hidup di API.
6. **Fase C (verifikasi artefak):** KEDUA file (`playlist.m3u` +
   `playlist-pipe.m3u`) yang baru ditulis di-parse ulang dan **setiap
   entry di-probe seperti player sungguhan** (`probe_deep`): manifest →
   playlist varian → **satu segmen media** — jadi URL yang menjawab 200
   di manifest tapi 403/HTML di segmen tetap gugur. Per-host, pelan, satu
   retry tenang. Gagal → channel dimatikan / `pipe_only` → ditulis ulang →
   diulang, **selalu diakhiri sebuah pass verifikasi** (maks 5 drop).
   Kalau belum bersih, script exit 1 dan CI membiarkan data lama tetap
   terpublish — tidak pernah mempublikasikan playlist yang gagal
   diverifikasi. `HEALTH_DEEP=0` menonaktifkan langkah segmen.
7. Hasilnya ditulis ke `data/health.json` dan `stats.json`
   (`playlist_verified: true/false`, `playlist_entries` +
   `playlist_pipe_entries`, `pipe_only`, `drm_excluded`).

Konsekuensinya: **setiap entry di kedua playlist sudah terverifikasi
mengembalikan media valid pada saat publish** — diverifikasi dua kali
(fase B) dan ulang terhadap file hasil tulis sampai ke segmen (fase C).
Channel mati/geo-block/DRM tetap ada di `data/channels.json`
(lihat `?dead=1`), tidak dihapus.

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

`HEALTH_GEO_MODE` (default **`keep`**) — aturan intinya: **403 = urusan IP
penonton, bukan vonis mati.** URL stream di-fetch player dari koneksi
penonton, jadi yang menentukan bisa/tidaknya adalah IP penonton — bukan IP
checker (CI/Cloudflare). Contoh: API disajikan Cloudflare dari AS, penonton
buka lewat WiFi rumah di Indonesia → channel Indonesia yang 403 dari checker
tetap jalan untuk penontonnya.

| Nilai | Perilaku |
|---|---|
| `keep` | **Semua** 403 (`403`, `403-geo`, `403(vlc)` …) tidak membunuh channel: tetap hidup, tetap di playlist, ditandai `geo_limited: true` + `geo_status`. Fase C hanya menandainya. Satu-satunya 403 yang dikeluarkan dari playlist **VLC**: yang penyebabnya header yang tak bisa dibawa `#EXTVLCOPT` (mis. `Cookie`) → dipindah ke `playlist-pipe.m3u` (ditandai `pipe_only`, tetap hidup di API). |
| `drop` | mode ketat: 403 diperlakukan seperti kegagalan lain, dikeluarkan dari playlist. |

Yang tetap dianggap mati (bukan masalah IP): `404`, `400`, `401` (butuh API
key), timeout, dan host yang tidak konsisten (`flaky:*`).

Egress lain (mis. dari Indonesia/Malaysia) bisa dipakai lewat env standar
urllib: `HTTPS_PROXY=http://host:port` (simpan sebagai repo secret) — bukan
untuk menentukan hidup/mati (403 selalu dipertahankan), melainkan untuk
label `403-geo` yang lebih akurat.

## Tests

```
python -m unittest discover -s tools -p 'test_*.py'                        # 65 tests
node --test worker/test.mjs worker/test-playercompat.mjs \
              worker/integration.test.mjs                                  # 25 tests
```

Keduanya dijalankan oleh cron sebelum data dipublish.

## Player setup

| App | Playlist | How |
|---|---|---|
| OTT TV / OTT Navigator | `playlist-pipe.m3u` | Add playlist → URL |
| Televizo | `playlist-pipe.m3u` | Tambah playlist → URL |
| OTT Player / TiviMate / Smarters | `playlist-pipe.m3u` | Add M3U playlist → URL |
| VLC (Android/desktop) | `playlist.m3u` | Media → Open Network Stream |
| Kodi | `playlist.m3u` | PVR IPTV Simple Client → M3U URL |

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
