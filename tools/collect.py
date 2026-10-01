#!/usr/bin/env python3
"""Collect channel data from upstream sources into data/.

Reads config (base URL, markers, keys) from tools/config.bin (base64 blob).
Exits non-zero if zero channels decrypt (never wipe good data on a bad run).
"""
import base64
import binascii
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

from Crypto.Cipher import AES

OUT_DIR = os.environ.get('OUT_DIR', os.path.join(os.path.dirname(__file__), '..', 'data'))
EXTRA_FILE = os.environ.get(
    'EXTRA_CHANNELS', os.path.join(os.path.dirname(__file__), 'extra_channels.json'))

_cfg = base64.b64decode(open(os.path.join(os.path.dirname(__file__), 'config.bin')).read().strip()).decode()
CFG = json.loads(_cfg)
BASE = base64.b64decode(CFG['base']).decode()
RC_URL = base64.b64decode(CFG['rc']).decode()
APP_ID = base64.b64decode(CFG['app']).decode()
APK_LEN = int(CFG['len'])
CRC = int(os.environ.get('CRC', CFG['crc']))
COUNTRIES = CFG['cc']
D2 = lambda b: binascii.unhexlify(b)


def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='replace')


def c_(x):
    return x[::-1]


def b64p(x):
    return base64.b64decode(x + '=' * (-len(x) % 4)).decode('utf-8', errors='replace')


def build_key(cert_der):
    return hashlib.sha256((str(APK_LEN) + hashlib.sha256(cert_der).hexdigest().upper()).encode()).digest()[:16]


def build_marker(patch_key):
    seed = f'{APK_LEN}{CRC}{patch_key}'
    return hashlib.sha1(hashlib.md5(seed.encode()).hexdigest().encode()).hexdigest()


def decrypt_blob(blob, key):
    s = c_(b64p(c_(blob)))
    dec = base64.b64decode(s + '=' * (-len(s) % 4))
    pt = AES.new(key, AES.MODE_CBC, dec[:16]).decrypt(dec[16:])
    txt = pt.decode('utf-8', errors='replace')
    i = txt.find('{"')
    if i < 0:
        return None
    try:
        return json.loads(txt[i:txt.rfind('}') + 1])
    except json.JSONDecodeError:
        return None


def read_cert():
    p = os.path.join(os.path.dirname(__file__), 'config.bin')
    # cert der embedded in config blob
    return D2(CFG['cert'])


def load_extra_channels():
    """Channel tambahan yang lolos verifikasi tapi tak ada di upstream.

    Upstream hanya menyediakan stream DRM / API ber-token untuk sebagian
    channel Indonesia (SCTV, RCTI, dst.) sehingga tak bisa diputar. File ini
    berisi pengganti yang sudah diuji `ffmpeg` + dicek visual identitasnya.
    """
    if not os.path.exists(EXTRA_FILE):
        return []
    with open(EXTRA_FILE) as f:
        rows = json.load(f)
    out = []
    for r in rows:
        if not (r.get('hls') or '').startswith('http'):
            continue
        out.append({
            'name': r.get('name'),
            'tagline': r.get('tagline'),
            'hls': r.get('hls'),
            'is_live': r.get('is_live', True),
            'premium': r.get('premium', 'f'),
            'jenis': r.get('jenis', 'hls'),
            'header_iptv': json.dumps(normalize_headers(r.get('header_iptv')),
                                      ensure_ascii=False),
            'url_license': r.get('url_license', 'none'),
            'country': r.get('country', 'Indonesia'),
            'code': r.get('code', 'ID'),
            'group': r.get('group', 'Indonesia'),
            'source': 'extra',
        })
    return out


def esc_m3u(s):
    """Channel names/taglines must never contain CR/LF (breaks strict players).

    A CRLF run collapses to a single space so names stay readable.
    """
    import re
    return re.sub(r'[\r\n]+', ' ', s or '').strip()


# --------------------------------------------------------------------------
# Header handling
# --------------------------------------------------------------------------
# Upstream ships `header_iptv` as a JSON string, but a few records are
# malformed: the User-Agent appears as a KEY with no value, which is not
# valid JSON at all ({"Referer":"...","Origin":"...","Mozilla/5.0 ..."}).
# Those records silently parsed as {} -> the stream was requested without
# any header and failed. Repair them instead of dropping them.
_UA_CANDIDATE = re.compile(
    r'(?<=[,{])\s*"((?:Mozilla|okhttp|Dalvik|Lavf/|VLC/|ExoPlayer|curl/|'
    r'okhttp/|python-requests|Wget)[^"]*)"\s*(?=[,}])')


def _looks_like_ua(s):
    return bool(s) and bool(re.match(
        r'(Mozilla/|okhttp|Dalvik|Lavf/|VLC/|ExoPlayer|curl/|python-requests|Wget)',
        s.strip(), re.I))


def repair_header_json(raw):
    """Return a parseable JSON string for header_iptv (best effort)."""
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    s = (raw or '').strip()
    if not s:
        return '{}'
    try:
        json.loads(s)
        return s
    except Exception:
        pass
    fixed = _UA_CANDIDATE.sub(lambda m: '"User-Agent":"%s"' % m.group(1), s)
    try:
        json.loads(fixed)
        return fixed
    except Exception:
        return s


def normalize_headers(raw):
    """header_iptv -> clean {Header-Name: value}; drops 'none'/empty/dupes."""
    if isinstance(raw, dict):
        obj = raw
    else:
        try:
            obj = json.loads(repair_header_json(raw))
        except Exception:
            obj = {}
    out = {}
    for k, v in (obj or {}).items():
        k = str(k).strip()
        if not k or v is None or isinstance(v, (dict, list)):
            continue
        v = str(v).strip()
        if _looks_like_ua(k):
            # a UA used as the key: keep it as User-Agent (value wins when
            # the value is itself a UA)
            out.setdefault('User-Agent', v if _looks_like_ua(v) else k)
            continue
        if not v or v.lower() == 'none':
            continue
        out[k] = v
    return out


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------
# Upstream labels Indonesian content with pseudo-country codes so the same
# nation ends up scattered over three group-titles ("Indonesia", "TVRI",
# "TV Lokal"). Players show one folder per group-title -> the Indonesian
# folder looked incomplete. Everything Indonesian lands in one group.
GROUP_ALIASES = {'TVRI': 'Indonesia', 'TV Lokal': 'Indonesia'}
CODE_GROUPS = {'ID': 'Indonesia', 'RI': 'Indonesia', 'LO': 'Indonesia'}


def classify_entry(ch, names, file_code):
    """(code, country, group-title) for one upstream record.

    Upstream splits the catalogue across bucket files (C0..C9), so the folder
    code lives on the record itself (`alpha_2_code`), never on the file name —
    grouping by file would scatter Indonesia into "C0"/"C5"/... folders.
    `names` is the file's `country_list` (alpha_2_code -> label).
    """
    entry_code = (ch.get('alpha_2_code') or '').strip() or file_code
    country = (names.get(entry_code) or ch.get('country_name')
               or names.get(file_code, file_code))
    return entry_code, country, CODE_GROUPS.get(
        entry_code, GROUP_ALIASES.get(country, country))


def group_of(ch):
    """group-title for a channel (fast, stable, no per-entry surprises).

    A geo-locked channel still 403s from the checker's vantage point — it
    may well open from the network it is meant for, so it is NOT dropped;
    it goes to a separate `<group> (geo)` folder instead, keeping the main
    folder 100% verified-playable."""
    g = ch.get('group')
    if not g:
        code = ch.get('code')
        if code in CODE_GROUPS:
            g = CODE_GROUPS[code]
        else:
            country = ch.get('country') or ''
            g = GROUP_ALIASES.get(country, country)
    if ch.get('geo_limited'):
        return f'{g} (geo)'
    return g


def apply_extra_overrides(uniq, extras):
    """Curated extras beat raw upstream rows shown in the same folder.

    Without this a live upstream "SCTV" (and "JTV", whose upstream record
    carries code LO yet groups to Indonesia) publishes the channel twice.
    Returns (rows, dropped_count); every extra row is guaranteed present —
    the (name, hls) dedupe above may have kept the upstream row for a pair
    the extra shares, which dropping would then delete entirely."""
    extra_keys = {(e.get('name'), group_of(e)) for e in extras
                  if e.get('source') == 'extra'}
    if not extra_keys:
        return uniq, 0
    kept = [c for c in uniq
            if c.get('source') == 'extra'
            or (c.get('name'), group_of(c)) not in extra_keys]
    dropped = len(uniq) - len(kept)
    have = {(c.get('name'), c.get('hls')) for c in kept}
    kept += [e for e in extras
             if (e.get('name'), e.get('hls')) not in have]
    return kept, dropped


# --------------------------------------------------------------------------
# Playlist writing
# --------------------------------------------------------------------------
# Two styles, because players disagree about how headers travel:
#   'vlc'  -> #EXTVLCOPT/#KODIPROP tags + plain URL  (VLC, Kodi, ... )
#   'pipe' -> URL|User-Agent=..&Referer=..           (OTT TV, Televizo,
#             TiviMate, OTT Player, Smarters and friends on Android)
# A header one style cannot carry (Cookie) only excludes the entry from
# THAT style's file — never from the other one.
PLAYLIST_FILES = {'vlc': 'playlist.m3u', 'pipe': 'playlist-pipe.m3u'}
VLC_HEADER_KEYS = ('user-agent', 'referer', 'origin')


def _uncarryable_headers(ch, allowed_keys):
    """Headers whose NAME is outside `allowed_keys` (per style)."""
    return [k for k in normalize_headers(ch.get('header_iptv'))
            if k.lower() not in allowed_keys]


def pipe_unusable_headers(ch):
    """Values a `|Header=value&...` suffix cannot express (delimiters)."""
    return [k for k, v in normalize_headers(ch.get('header_iptv')).items()
            if '|' in v or '\r' in v or '\n' in v]


def pipe_url(url, hdr):
    """`https://x/index.m3u8|User-Agent=..&Referer=..` — the header syntax
    understood by Android/IPTV players (VLC ignores it, hence two files).

    Values are left raw (players decode inconsistently) except for the two
    characters that would break parsing: `|` and `&` plus any newline.
    """
    order = ('User-Agent', 'Referer', 'Origin', 'Cookie', 'Authorization')
    parts = []
    for k in order:
        v = hdr.get(k)
        if v:
            parts.append('%s=%s' % (k, v.replace('%', '%25').replace('|', '%7C')
                                    .replace('&', '%26').replace('\r', '')
                                    .replace('\n', '')))
    for k, v in hdr.items():
        if k in order:
            continue
        if not v:
            continue
        parts.append('%s=%s' % (k, v.replace('%', '%25').replace('|', '%7C')
                                .replace('&', '%26').replace('\r', '')
                                .replace('\n', '')))
    return url + '|' + '&'.join(parts) if parts else url


def write_playlist(channels, out_dir, include_premium=False, skip_dead=True,
                   style='vlc'):
    """Write the playlist for one style; every emitted entry carries its
    playback headers.

    Shared by collect.py (initial write) and health.py (rewrite after probing).
    Returns the number of entries written.
    """
    filename = PLAYLIST_FILES[style]
    lines = ['#EXTM3U']
    entries = 0
    for ch in sorted(channels,
                     key=lambda x: (group_of(x), x.get('name') or '')):
        if skip_dead and (ch.get('dead') or ch.get('no_playlist')
                          or ch.get('drm')):
            # no_playlist  = header tak bisa dibawa gaya mana pun
            # drm          = manifest terenkripsi -> player biasa error
            continue
        if style == 'vlc' and (ch.get('pipe_only')
                               or _uncarryable_headers(ch, VLC_HEADER_KEYS)):
            # Cookie dsb. hanya bisa dibawa oleh gaya pipe
            continue
        if style == 'pipe' and pipe_unusable_headers(ch):
            continue
        if not include_premium and ch.get('premium') == 't':
            continue
        hls = ch.get('hls') or ''
        if not hls.startswith('http'):
            continue
        # tvg-id must be sanitized too — a CRLF in the name used to leak into it
        gid = esc_m3u(ch.get('name') or 'tv').lower().replace(' ', '')[:24]
        entries += 1
        lines.append(f'#EXTINF:-1 tvg-id="{gid}" tvg-name="{esc_m3u(ch.get("name"))}" '
                     f'group-title="{esc_m3u(group_of(ch))}",{esc_m3u(ch.get("name"))}')
        hdr = normalize_headers(ch.get('header_iptv'))
        ua = hdr.get('User-Agent')
        if ua:
            lines.append(f'#EXTVLCOPT:http-user-agent={ua}')
            lines.append(f'#KODIPROP:http-user-agent={ua}')
        ref = hdr.get('Referer')
        if ref:
            lines.append(f'#EXTVLCOPT:http-referrer={ref}')
        origin = hdr.get('Origin')
        if origin:
            lines.append(f'#KODIPROP:http-origin={origin}')
        if style == 'pipe' and hdr:
            lines.append(pipe_url(hls, hdr))
        else:
            lines.append(hls)
    with open(os.path.join(out_dir, filename), 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return entries


def write_all_playlists(channels, out_dir, include_premium=False,
                        skip_dead=True):
    """Write BOTH styles. Returns {'vlc': n, 'pipe': n}."""
    return {style: write_playlist(channels, out_dir, include_premium,
                                  skip_dead, style)
            for style in PLAYLIST_FILES}


def main():
    base = BASE
    patch_key = base64.b64decode(CFG.get('pkey', '')).decode()
    marker = build_marker(patch_key)
    cert = read_cert()
    key = build_key(cert)

    # optional: try remote config for fresh base (auth via optional env)
    token = os.environ.get('RC_TOKEN', '')
    if token:
        try:
            body = json.dumps({'app_id': APP_ID, 'app_instance_id': 'c1f0b630-88f6-4d1e-a3b2-9a8c7d6e5f40'})
            req = urllib.request.Request(RC_URL + '?key=' + token, data=body.encode(),
                                         headers={'Content-Type': 'application/json'})
            rc = json.loads(urllib.request.urlopen(req, timeout=20).read())
            e = rc.get('entries', {})
            if e.get('path_url_216'):
                base = e['path_url_216']
                print('rc ok, base refreshed')
        except Exception as ex:
            print('rc fetch failed, using static base:', str(ex)[:60])

    all_channels = []
    per_country = {}
    failed = []
    bases = [base, BASE]
    if bases[0] == bases[1]:
        bases = [base]
    for code in COUNTRIES:
        dd = None
        for b in bases:
            try:
                body = http_get(b + code + '.json').strip()
                idx = body.find(marker)
                blob = body[idx + 40:] if idx >= 0 else body
                dd = decrypt_blob(blob, key)
                if dd:
                    base = b
                    bases = [b]
                    break
            except Exception:
                continue
        if not dd:
            failed.append(code)
            print(code, 'ERR all bases failed')
            continue
        names = {c['alpha_2_code']: c['country_name'] for c in (dd.get('country_list') or [])}
        # normalize upstream group labels
        names = {k: (v.replace('Bioskop BitTV', 'Movies').replace('BitTV', 'TV')) for k, v in names.items()}
        per_country[code] = names.get(code, code)
        for ch in dd.get('info') or []:
            # Upstream memecah channel ke banyak file "bucket" (C0..C9), jadi
            # kode folder yang benar ada di tiap entri (alpha_2_code), bukan di
            # nama file. Tanpa ini channel Indonesia terpecah jadi grup "C0".
            entry_code, country, group = classify_entry(ch, names, code)
            all_channels.append({
                'name': ch.get('name'),
                'tagline': ch.get('tagline'),
                'hls': ch.get('hls'),
                'is_live': ch.get('is_live'),
                'premium': ch.get('premium'),
                'jenis': ch.get('jenis'),
                # normalised on ingest: malformed upstream JSON (UA used as
                # the key) used to parse as {} and lose every header
                'header_iptv': json.dumps(normalize_headers(ch.get('header_iptv')),
                                          ensure_ascii=False),
                'url_license': ch.get('url_license'),
                'country': country,
                'code': entry_code,
                # group-title shown by players (Indonesia keeps TVRI/TV Lokal)
                'group': group,
            })
        print(code, len(dd.get('info') or []))

    extras = load_extra_channels()
    if extras:
        all_channels.extend(extras)
        print('extra:', len(extras))

    if not all_channels:
        print('FATAL: zero channels; keeping old data')
        sys.exit(1)

    seen = {}
    for ch in all_channels:
        # (name, hls): the same event feed is republished under every country
        # code with an identical URL — dedupe on the URL, not on the country.
        # Same name with a DIFFERENT URL (e.g. Animax HD ID vs JP) is kept.
        seen.setdefault((ch.get('name'), ch.get('hls')), ch)
    uniq = list(seen.values())
    uniq, dropped = apply_extra_overrides(uniq, extras)
    if dropped:
        print('extra-override:', dropped, 'upstream dup dropped')

    os.makedirs(OUT_DIR, exist_ok=True)
    updated = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    payload = {'updated': updated, 'count': len(uniq), 'channels': uniq}
    with open(os.path.join(OUT_DIR, 'channels.json'), 'w') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    playlist_counts = write_all_playlists(uniq, OUT_DIR)
    n_entries = playlist_counts['vlc']

    with open(os.path.join(OUT_DIR, 'countries.json'), 'w') as f:
        counts = {}
        for ch in uniq:
            counts[ch['country']] = counts.get(ch['country'], 0) + 1
        json.dump({'updated': updated,
                   'countries': [{'code': k, 'name': v} for k, v in per_country.items()],
                   'counts': counts}, f, ensure_ascii=False, indent=1)

    with open(os.path.join(OUT_DIR, 'stats.json'), 'w') as f:
        json.dump({'updated': updated, 'unique': len(uniq),
                   'free': sum(1 for c in uniq if c['premium'] != 't'),
                   'premium': sum(1 for c in uniq if c['premium'] == 't'),
                   'playlist_entries': n_entries,
                   'playlist_pipe_entries': playlist_counts['pipe'],
                   'failed': failed}, f, indent=1)
    print(f'unique: {len(uniq)} playlist-entries: {n_entries} failed: {failed}')


if __name__ == '__main__':
    main()
