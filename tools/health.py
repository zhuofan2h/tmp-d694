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
        h = {'User-Agent': user_agent}
        if with_ref:
            if ref: h['Referer'] = ref
            if org: h['Origin'] = org
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
    for label, url, headers, timeout in variants(ch):
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

    def run_host(items):
        bad = []
        for e in items:
            hdr = dict(e['headers'])
            hdr.setdefault('User-Agent', BROWSER_UA)
            ok, kind, status, _ = probe(e['url'], hdr, TIMEOUT)
            if not ok:
                bad.append((e, status))
            time.sleep(gap)
        return bad

    failed = []
    with ThreadPoolExecutor(max_workers=threads or THREADS) as ex:
        for bad in ex.map(run_host, list(by_host.values())):
            failed.extend(bad)
    return len(entries), failed


def main():
    src = os.path.join(OUT_DIR, 'channels.json')
    if not os.path.exists(src):
        print('FATAL: no channels.json — run collect.py first')
        return 1
    data = json.load(open(src, encoding='utf-8'))
    channels = data['channels']

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

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    repaired = []
    unplayable = []
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
    max_rounds = int(os.environ.get('HEALTH_VERIFY_ROUNDS', '3'))
    verify_fail = []
    while rounds < max_rounds:
        rounds += 1
        total, failed = verify_published(OUT_DIR)
        if not failed:
            verify_ok = True
            print(f'phase C: playlist verified {total}/{total} entries '
                  f'(round {rounds})', flush=True)
            break
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
            if c.get('hls') in bad_urls and not c.get('dead'):
                c['dead'] = True
                c['dead_reason'] = 'verify-failed'
                c['fail_count'] = FAIL_THRESHOLD
                changed = True
        if not changed:
            break                      # failures we cannot attribute -> give up
        entries = collect.write_playlist(channels, OUT_DIR)

    dead_count = sum(1 for c in channels if c.get('dead'))
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
                      'dead': dead_count, 'playlist_entries': entries,
                      'playlist_verified': verify_ok})
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=1)

    print(f'playable: {playable}/{len(channels)} | dead: {dead_count} | '
          f'repaired: {len(repaired)} | playlist entries: {entries} | '
          f'verified: {verify_ok} ({rounds} round(s))')
    if not verify_ok:
        print('FATAL: playlist could not be fully verified; '
              'keeping previous published data')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
