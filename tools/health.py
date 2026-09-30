#!/usr/bin/env python3
"""Health-check every channel, repair what can be repaired, then rewrite
playlist.m3u so that EVERY published entry is verified playable.

Run after collect.py:

    python tools/collect.py && python tools/health.py

What it does
------------
1. Probes each channel URL with its declared headers (User-Agent / Referer /
   Origin from `header_iptv`).
2. On failure it retries with repair variants that are known to help:
   longer timeout, VLC/ffmpeg User-Agent, no-Referer, http->https.
3. If a variant works, the channel's `hls` / `header_iptv` is updated to the
   working combination.
4. Channels are marked `dead: true` as soon as FAIL_THRESHOLD failed checks
   accumulate (default: this very run) and revived as soon as one check
   succeeds — so the published playlist only ever contains URLs that just
   answered with a real stream.
5. playlist.m3u is regenerated from the live free channels only, and
   data/health.json records the outcome. If a run would publish zero entries
   the old playlist is kept and the script exits non-zero (never wipe good
   data on a bad run).

Exit codes: 0 ok, 1 nothing playable / missing input.
"""
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402  (config + write_playlist)

OUT_DIR = os.environ.get('OUT_DIR', os.path.join(os.path.dirname(__file__), '..', 'data'))
TIMEOUT = float(os.environ.get('HEALTH_TIMEOUT', '12'))
THREADS = int(os.environ.get('HEALTH_THREADS', '20'))
# Default 1 = a channel that fails this run is excluded from the published
# playlist immediately (strict "everything published plays" guarantee).
# Raise HEALTH_FAILS to 2+ if you prefer tolerance for transient blips.
FAIL_THRESHOLD = int(os.environ.get('HEALTH_FAILS', '1'))
# 'keep' (default): ANY 403 must NOT kill the channel. A 403 only means
# 'not from the CHECKER's IP' — playability is decided by the VIEWER's IP,
# because the player fetches the stream URL from its own connection (e.g.
# API served by Cloudflare US, viewer on home WiFi in ID -> ID channels
# that 403 from the checker still play for the viewer). Such channels stay
# alive, stay in playlist.m3u and are flagged `geo_limited`.
# 'drop': strict mode — 403s are excluded like any other failure.
GEO_MODE = os.environ.get('HEALTH_GEO_MODE', 'keep').lower()


def location_dependent(status):
    """True when a failed status must NOT kill the channel.

    403-family answers depend on WHO asks: the viewer's IP decides, never
    the checker's. Under HEALTH_GEO_MODE=keep these are kept alive and
    flagged `geo_limited` instead of being marked dead.
    """
    return GEO_MODE == 'keep' and str(status).startswith('403')
# 'fragment': probe only and write HEALTH_FRAGMENT (used by CI matrix jobs)
# 'full'    : probe + publish (or merge HEALTH_FRAGMENTS if provided)
HEALTH_MODE = os.environ.get('HEALTH_MODE', 'full').lower()
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) '
              'Gecko/20100101 Firefox/153.0')

PLAYABLE_KINDS = ('hls', 'dash', 'flv', 'ts')


def classify(head: bytes) -> str:
    """What kind of payload did we get? 'hls'/'dash'/'flv'/'ts' are streams."""
    text_head = head[:512].decode('utf-8', errors='replace').lstrip()
    if text_head.startswith('#EXTM3U') or text_head.startswith('#EXT-X-'):
        return 'hls'
    if '<MPD' in text_head:
        return 'dash'
    if head[:3] == b'FLV':
        return 'flv'
    if head and head[0] == 0x47:          # MPEG-TS sync byte
        return 'ts'
    if text_head.startswith('{') or text_head.startswith('['):
        return 'json'                     # API reply, not a stream
    if text_head.startswith('<'):
        return 'html'
    return 'other'


def declared_headers(ch):
    try:
        hdr = json.loads(ch.get('header_iptv') or '{}')
    except Exception:
        hdr = {}
    return {k: v for k, v in hdr.items() if v and v != 'none'}


def variants(ch):
    """(label, url, headers, timeout) attempts, cheapest success first."""
    url = ch.get('hls') or ''
    hdr = declared_headers(ch)
    ua = hdr.get('User-Agent') or BROWSER_UA
    ref, org = hdr.get('Referer'), hdr.get('Origin')

    def mk(user_agent, with_ref=True):
        # forward EVERY declared header (Cookie, Authorization, custom tokens...)
        # — dropping them is what made e.g. Trans7/TransTV answer 403
        h = {k: v for k, v in hdr.items() if k != 'User-Agent'}
        h['User-Agent'] = user_agent
        if not with_ref:
            h.pop('Referer', None)
            h.pop('Origin', None)
        return h

    yield 'declared', url, mk(ua), TIMEOUT
    yield 'slow', url, mk(ua), max(TIMEOUT, 25)
    yield 'vlc', url, mk('VLC/3.0.20 LibVLC/3.0.20'), TIMEOUT
    yield 'ffmpeg', url, mk('Lavf/60.16.100'), TIMEOUT
    yield 'no-ref', url, mk(ua, with_ref=False), TIMEOUT
    if url.startswith('http://'):
        yield 'https', 'https://' + url[7:], mk(ua), TIMEOUT


# Several channels live behind the SAME CDN host; firing 20 parallel probes at
# one host makes it rate-limit us and produces fake "dead" verdicts. Cap
# concurrent requests per host (a real player opens one stream at a time).
_SEMAPHORES = {}
_SEMAPHORES_LOCK = threading.Lock()
MAX_PER_HOST = int(os.environ.get('HEALTH_PER_HOST', '2'))


def _host_sem(url):
    host = urlparse(url).netloc.lower()
    with _SEMAPHORES_LOCK:
        sem = _SEMAPHORES.get(host)
        if sem is None:
            sem = threading.BoundedSemaphore(MAX_PER_HOST)
            _SEMAPHORES[host] = sem
        return sem


def probe_limited(url, headers, timeout):
    with _host_sem(url):
        return probe(url, headers, timeout)


GEO_MARKERS = (
    'block access from your country',
    'access from your country',
    'not available in your country',
    'unavailable in your country',
    'banned your access based on your country',
    'geo-restricted',
)


def geo_reason(url, headers, timeout=8):
    """Return '403-geo' when the 403 body explicitly says it is geo-based."""
    try:
        req = urllib.request.Request(url, headers=headers or {}, method='GET')
        body = urllib.request.urlopen(req, timeout=timeout).read(4000)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(4000)
        except Exception:
            return None
    except Exception:
        return None
    text = body.decode('utf-8', errors='replace').lower()
    return '403-geo' if any(m in text for m in GEO_MARKERS) else None


def probe(url, headers, timeout):
    """Return (ok, kind, status, final_url). Follows redirects."""
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        head = r.read(512)
        kind = classify(head)
        return kind in PLAYABLE_KINDS, kind, r.status, r.geturl()
    except urllib.error.HTTPError as e:
        return False, 'httperror', e.code, url
    except socket.timeout:
        return False, 'timeout', 'timeout', url
    except Exception as e:
        return False, 'conn_error', str(e)[:70], url


def check_channel(ch):
    """Probe with repairs. Returns (channel, status_label, variant_used)."""
    last_status = None
    last_headers = None
    last_url = None
    for label, url, headers, timeout in variants(ch):
        last_headers, last_url = headers, url
        ok, kind, status, final = probe_limited(url, headers, timeout)
        last_status = f'{status}' if label == 'declared' else f'{status}({label})'
        if not ok:
            continue
        # success — persist whatever combination worked
        out = dict(ch)
        if url != ch.get('hls'):
            out['hls'] = url
        elif final and final != url and label == 'declared':
            # only bake in a redirect target when it is a plain same-kind URL
            if final.split('?')[0] == url.split('?')[0]:
                out['hls'] = final
        if label != 'declared':
            try:
                hdr = json.loads(ch.get('header_iptv') or '{}')
            except Exception:
                hdr = {}
            hdr['User-Agent'] = headers.get('User-Agent', hdr.get('User-Agent'))
            if label == 'no-ref':
                hdr.pop('Referer', None)
                hdr.pop('Origin', None)
            out['header_iptv'] = json.dumps(hdr, ensure_ascii=False)
        return out, f'ok:{kind}', label
    if last_status and str(last_status).startswith('403'):
        # distinguish a hard geo-block from a plain 403 (token/auth)
        if geo_reason(last_url, last_headers):
            last_status = '403-geo' + str(last_status)[3:]
    return ch, last_status or 'unknown', None


def confirm(ch, timeout=None):
    """Second, independent probe of a channel that already passed once.

    Flaky/rate-limited hosts answer 200 on one connection and reset the next;
    only channels that answer BOTH times are published.
    Returns (ok, status).
    """
    url = ch.get('hls') or ''
    hdr = declared_headers(ch)
    hdr.setdefault('User-Agent', BROWSER_UA)
    ok, kind, status, _ = probe_limited(url, hdr, timeout or TIMEOUT)
    return ok, status


# M3U can only carry these (via #EXTVLCOPT / #KODIPROP). A channel that
# NEEDS anything else (Cookie, Authorization, custom tokens) plays fine with
# the API/headers but can never play from a plain playlist entry.
PLAYLIST_HEADER_KEYS = {'user-agent', 'referer', 'origin'}


def needs_extra_headers(ch):
    """Headers the playlist cannot express; [] means it is playlist-safe."""
    try:
        hdr = json.loads(ch.get('header_iptv') or '{}')
    except Exception:
        return []
    return [k for k, v in hdr.items()
            if k.lower() not in PLAYLIST_HEADER_KEYS and v not in ('none', None, '')]


def parse_playlist(path):
    """Parse playlist.m3u into [{'name','url','headers'}]."""
    entries, cur = [], None
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            if line.startswith('#EXTINF'):
                m = re.search(r',(.*)$', line)
                cur = {'name': (m.group(1) if m else '').strip(),
                       'headers': {}, 'url': None}
            elif cur is not None and line.startswith('#EXTVLCOPT:http-user-agent='):
                cur['headers']['User-Agent'] = line.split('=', 1)[1]
            elif cur is not None and line.startswith('#EXTVLCOPT:http-referrer='):
                cur['headers']['Referer'] = line.split('=', 1)[1]
            elif line.startswith('#'):
                continue
            elif line.startswith('http') and cur is not None:
                cur['url'] = line
                entries.append(cur)
                cur = None
    return entries


def verify_published(out_dir, threads=None, gap=0.4):
    """Phase C: probe EVERY entry of the just-written playlist, one host at a
    time per slot with a small gap (a real player is gentle too). Returns
    (total, [(entry, status)] failures)."""
    entries = parse_playlist(os.path.join(out_dir, 'playlist.m3u'))
    by_host = {}
    for e in entries:
        by_host.setdefault(urlparse(e['url']).netloc.lower(), []).append(e)

    # url -> headers the playlist CANNOT carry (Cookie etc). A 403 on such
    # an entry is a playlist limitation, NOT a geo problem: it must be
    # dropped from the playlist (but stays alive for API consumers).
    extra_by_url = {}
    src = os.path.join(out_dir, 'channels.json')
    if os.path.exists(src):
        try:
            for c in json.load(open(src, encoding='utf-8')).get('channels', []):
                extra = needs_extra_headers(c)
                if extra:
                    extra_by_url[c.get('hls')] = extra
        except Exception:
            extra_by_url = {}

    def run_host(items):
        bad, geo = [], []
        for e in items:
            hdr = dict(e['headers'])
            hdr.setdefault('User-Agent', BROWSER_UA)
            ok, kind, status, _ = probe(e['url'], hdr, TIMEOUT)
            if not ok:
                # calm retry: phase A+B just hammered this host, throttling
                # right now does not mean the channel is dead
                time.sleep(2.0)
                ok, kind, status, _ = probe(e['url'], hdr, TIMEOUT)
            if not ok:
                playlist_limited = extra_by_url.get(e['url'])
                if location_dependent(status) and not playlist_limited:
                    # 403 from the checker's IP: viewer's IP decides, so
                    # keep it published (flagged) instead of dropping it
                    label = ('403-geo' if geo_reason(e['url'], hdr)
                             else '403-blocked')
                    geo.append((e, label))
                else:
                    bad.append((e, status))
            time.sleep(gap)
        return bad, geo

    failed, geo_ok = [], []
    with ThreadPoolExecutor(max_workers=threads or THREADS) as ex:
        for bad, geo in ex.map(run_host, list(by_host.values())):
            failed.extend(bad)
            geo_ok.extend(geo)
    return len(entries), failed, geo_ok


def write_fragment(path, vantage, results):
    """Persist one vantage point's verdicts (used by CI matrix jobs)."""
    frag = {
        'vantage': vantage,
        'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'results': [
            {'name': ch.get('name'), 'code': ch.get('code'),
             'hls': ch.get('hls'), 'ok': status.startswith('ok:'),
             'status': status}
            for ch, status, _variant in results
        ],
    }
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(frag, f, ensure_ascii=False)
    ok = sum(1 for r in frag['results'] if r['ok'])
    print(f'fragment written: {path} ({vantage}: {ok}/{len(frag["results"])} ok)')
    return path


def merge_fragments(paths):
    """Combine verdicts from several vantage points.

    A channel is ALIVE if ANY vantage can play it (geo-block in one country
    must not hide a channel from the country that CAN watch it). Failures are
    kept per-vantage for reporting; the label prefers '403-geo' when that is
    what every vantage saw.

    Returns {(name, code): {'ok': bool, 'status': str,
                            'vantages_ok': [...], 'vantages_fail': {...}}}
    """
    merged = {}
    loaded = 0
    for path in paths:
        try:
            fr = json.load(open(path, encoding='utf-8'))
        except Exception as ex:
            print(f'  skip fragment {path}: {ex}')
            continue
        loaded += 1
        vantage = fr.get('vantage') or path
        for r in fr.get('results', []):
            key = (r.get('name'), r.get('code'))
            m = merged.setdefault(key, {'ok': False, 'status': None,
                                        'vantages_ok': [], 'vantages_fail': {}})
            if r.get('ok'):
                m['ok'] = True
                m['status'] = 'ok:merged'
                m['vantages_ok'].append(vantage)
            else:
                m['vantages_fail'][vantage] = str(r.get('status'))
                if not m['ok']:
                    st = str(r.get('status'))
                    cur = str(m['status'] or '')
                    # first failure is the default label; an explicit geo
                    # verdict from ANY vantage upgrades the label
                    if m['status'] is None \
                            or (st.startswith('403-geo') and not cur.startswith('403-geo')):
                        m['status'] = st
    print(f'merged {loaded} fragment(s) -> {len(merged)} channels')
    return merged


def fragment_paths(spec):
    """'frags/*.json' or 'a.json:b.json' -> sorted list of files."""
    import glob as _glob
    paths = []
    for part in str(spec).replace(';', ':').split(':'):
        part = part.strip()
        if not part:
            continue
        hits = _glob.glob(part)
        paths.extend(hits if hits else [part])
    return sorted(dict.fromkeys(paths))


def merged_results(channels, frag_spec):
    """Turn merged vantage fragments into [(channel, status, variant), ...].

    A channel is alive when ANY vantage reported it playable; a 403-geo seen
    anywhere becomes 'ok:geo-kept' when HEALTH_GEO_MODE=keep.
    """
    merged = merge_fragments(fragment_paths(frag_spec))
    results = []
    missing = 0
    geo_kept = 0
    for ch in channels:
        m = merged.get((ch.get('name'), ch.get('code')))
        if m is None:
            status = 'not-checked'              # absent from every fragment
            missing += 1
        elif m['ok']:
            status = str(m['status'])           # 'ok:merged'
        else:
            status = str(m['status'])
            if location_dependent(status):
                # 403 from a checker IP is not a dead stream: the viewer
                # decides (playlist phase C refines header-limited ones)
                status = 'ok:geo-kept'
                geo_kept += 1
        results.append((ch, status, None))
    alive = sum(1 for _, s, _ in results if s.startswith('ok'))
    print(f'merge mode: {alive} alive of {len(results)} '
          f'({geo_kept} geo-kept, {missing} not in any fragment)', flush=True)
    return results


def probe_all(channels):
    """Phase A + B: probe every channel from THIS vantage point.
    Returns [(channel, status, repaired_variant), ...]; mutates the
    channel dicts in memory (repairs) but writes nothing to disk.
    """
    print(f'probing {len(channels)} channels with {THREADS} threads '
          f'(timeout {TIMEOUT}s, dead after {FAIL_THRESHOLD} bad runs)...')

    results = []
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        for i, (ch, status, variant) in enumerate(ex.map(check_channel, channels), 1):
            results.append((ch, status, variant))
            if i % 50 == 0:
                print(f'  {i}/{len(channels)}', flush=True)

    # Phase B: every first-pass success must answer AGAIN on a fresh
    # connection, otherwise it is flaky/rate-limited and gets dropped.
    if os.environ.get('HEALTH_CONFIRM', '1') == '1':
        print('phase B: confirming first-pass successes...', flush=True)

        def _confirm(item):
            idx, (ch, status, variant) = item
            if not status.startswith('ok:'):
                return None
            ok, st = confirm(ch)
            return None if ok else (idx, f'flaky:{st}')

        with ThreadPoolExecutor(max_workers=THREADS) as ex2:
            for res in ex2.map(_confirm, list(enumerate(results))):
                if res:
                    idx, st = res
                    ch, _, _variant = results[idx]
                    results[idx] = (ch, st, None)
                    print(f'  flaky: {ch.get("name")} -> {st}', flush=True)

    return results


def main():
    src = os.path.join(OUT_DIR, 'channels.json')
    if not os.path.exists(src):
        print('FATAL: no channels.json — run collect.py first')
        return 1
    data = json.load(open(src, encoding='utf-8'))
    channels = data['channels']

    frag_spec = os.environ.get('HEALTH_FRAGMENTS', '')
    if frag_spec:
        # merge mode: verdicts already gathered from several vantage points
        # -> no local probing at all
        results = merged_results(channels, frag_spec)
    else:
        results = probe_all(channels)

        # --- fragment mode: report only, never touch data/ ---------------
        if HEALTH_MODE == 'fragment':
            frag_out = os.environ.get('HEALTH_FRAGMENT', 'health-fragment.json')
            vantage = os.environ.get('HEALTH_VANTAGE', 'local')
            write_fragment(frag_out, vantage, results)
            return 0

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    repaired = []
    unplayable = []
    geo_kept = []
    playable = 0
    for ch, status, variant in results:
        was_dead = bool(ch.get('dead'))
        if status.startswith('ok:'):
            ch.pop('dead', None)
            ch.pop('dead_reason', None)
            ch.pop('fail_count', None)
            playable += 1
            if variant and variant != 'declared':
                repaired.append({'name': ch.get('name'), 'code': ch.get('code'),
                                 'variant': variant, 'url': ch.get('hls')})
            if was_dead:
                repaired.append({'name': ch.get('name'), 'code': ch.get('code'),
                                 'variant': 'revived', 'url': ch.get('hls')})
        elif location_dependent(status):
            # 403 from the CHECKER's IP — the viewer's IP is what counts.
            # Keep it alive, flag it, publish it; never kill it.
            ch.pop('dead', None)
            ch.pop('dead_reason', None)
            ch.pop('fail_count', None)
            ch['geo_limited'] = True
            ch['geo_status'] = str(status)
            playable += 1
            geo_kept.append({'name': ch.get('name'), 'code': ch.get('code'),
                             'status': str(status), 'url': ch.get('hls')})
        else:
            fails = int(ch.get('fail_count') or 0) + 1
            ch['fail_count'] = fails
            if fails >= FAIL_THRESHOLD:
                ch['dead'] = True
                ch['dead_reason'] = status
            unplayable.append({'name': ch.get('name'), 'code': ch.get('code'),
                               'premium': ch.get('premium'), 'status': status,
                               'dead': bool(ch.get('dead')), 'url': ch.get('hls')})

    free_live = [c for c in channels if c.get('premium') != 't' and not c.get('dead')
                 and (c.get('hls') or '').startswith('http')]
    if not free_live:
        print('FATAL: health check produced zero playable free channels; '
              'keeping previous playlist')
        return 1

    entries = collect.write_playlist(channels, OUT_DIR)   # free + alive only

    # --- Phase C: the published artifact must prove itself ---------------
    # Probe every entry of the playlist we just wrote; anything that fails is
    # marked dead and the playlist is rewritten. Repeat until clean, so the
    # file we commit is 100% verified — not just the channel list.
    verify_ok = False
    rounds = 0
    drops = 0
    max_drops = int(os.environ.get('HEALTH_VERIFY_ROUNDS', '5'))
    verify_fail = []
    geo_flagged = 0
    while True:                       # the LAST action is always a verify pass
        rounds += 1
        total, failed, geo_ok = verify_published(OUT_DIR)
        if not failed:
            verify_ok = True
            # flag (never drop) the geo-restricted ones we just saw
            if GEO_MODE == 'keep' and geo_ok:
                geo_labels = {e['url']: st for e, st in geo_ok}
                for c in channels:
                    st = geo_labels.get(c.get('hls'))
                    if st:
                        c['geo_limited'] = True
                        c['geo_status'] = f'{st}(verify)'
                        geo_flagged += 1
            print(f'phase C: playlist verified {total}/{total} entries '
                  f'(round {rounds}; {len(geo_ok)} geo-limited)', flush=True)
            break
        if drops >= max_drops:
            print(f'phase C: giving up after {drops} drops, '
                  f'{len(failed)} entries still failing', flush=True)
            break
        drops += 1
        print(f'phase C round {rounds}: {len(failed)}/{total} entries failed '
              f'verification -> dropping', flush=True)
        bad_urls = set()
        for e, st in failed:
            bad_urls.add(e['url'])
            verify_fail.append({'name': e['name'], 'url': e['url'],
                                'status': str(st)})
            print(f'  verify-fail {st} {e["name"][:40]}', flush=True)
        changed = False
        for c in channels:
            if c.get('hls') not in bad_urls or c.get('dead') or c.get('no_playlist'):
                continue
            extra = needs_extra_headers(c)
            if extra:
                # it played during phase A/B with its full headers — the
                # playlist simply cannot carry them (e.g. Cookie): keep the
                # channel alive for API consumers, keep it OUT of playlist.m3u
                c['no_playlist'] = True
                c['no_playlist_reason'] = f'needs headers: {",".join(extra)}'
                print(f'  no-playlist {c.get("name")} needs {extra}', flush=True)
            else:
                c['dead'] = True
                c['dead_reason'] = 'verify-failed'
                c['fail_count'] = FAIL_THRESHOLD
            changed = True
        if not changed:
            break                      # failures we cannot attribute -> give up
        entries = collect.write_playlist(channels, OUT_DIR)

    dead_count = sum(1 for c in channels if c.get('dead'))
    no_playlist_count = sum(1 for c in channels if c.get('no_playlist'))
    geo_count = sum(1 for c in channels if c.get('geo_limited'))
    data['channels'] = channels
    data['health'] = {'checked': now, 'playable': playable,
                      'total': len(channels), 'dead': dead_count,
                      'playlist_entries': entries, 'verified': verify_ok}
    with open(src, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    health = {
        'updated': now,
        'checked': len(channels),
        'playable': playable,
        'dead': dead_count,
        'no_playlist': no_playlist_count,
        'geo_limited': geo_count,
        'geo_mode': GEO_MODE,
        'repaired': len(repaired),
        'playlist_entries': entries,
        'playlist_verified': verify_ok,
        'verify_rounds': rounds,
        'verify_failures': verify_fail,
        'repairs': repaired,
        'unplayable': unplayable,
    }
    with open(os.path.join(OUT_DIR, 'health.json'), 'w', encoding='utf-8') as f:
        json.dump(health, f, ensure_ascii=False, indent=1)

    stats_path = os.path.join(OUT_DIR, 'stats.json')
    if os.path.exists(stats_path):
        stats = json.load(open(stats_path, encoding='utf-8'))
        stats.update({'health_checked': now, 'playable': playable,
                      'dead': dead_count, 'no_playlist': no_playlist_count,
                      'geo_limited': geo_count, 'geo_mode': GEO_MODE,
                      'playlist_entries': entries,
                      'playlist_verified': verify_ok})
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=1)

    print(f'playable: {playable}/{len(channels)} | dead: {dead_count} | '
          f'no-playlist: {no_playlist_count} | geo-limited: {geo_count} | '
          f'repaired: {len(repaired)} | playlist entries: {entries} | '
          f'verified: {verify_ok} ({rounds} round(s), {drops} drop(s))')
    if not verify_ok:
        print('FATAL: playlist could not be fully verified; '
              'keeping previous published data')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())