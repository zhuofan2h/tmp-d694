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
https://iptv-api.zhuofan2h.workers.dev/m3u?search=hbo
```

JSON:

- `GET /api/channels?country=ID&search=…&limit=…`
- `GET /api/countries`
- `GET /api/stats`
- `GET /health`

Web: `https://iptv-web.zhuofan2h.workers.dev/`

## Player setup

| App | How |
|---|---|
| TiviMate | Settings → Playlists → Add → URL |
| IPTV Smarters | Add Playlist → M3U URL |
| VLC | Media → Open Network Stream |
| Kodi | PVR IPTV Simple Client → M3U URL |

## Notes

- Some streams need specific headers — already embedded as `#EXTVLCOPT`/`#KODIPROP`.
- Some streams are geo-restricted.
- `.mpd`/`.flv` formats need a capable player (Kodi, TiviMate, ExoPlayer apps). Browsers: `.m3u8` only.
- Data refreshed automatically every 30 minutes.

## Layout

```
data/     playlist + JSON (generated)
tools/    collector
worker/   API
web/      web UI
```
