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
import sys
import urllib.request

from Crypto.Cipher import AES

OUT_DIR = os.environ.get('OUT_DIR', os.path.join(os.path.dirname(__file__), '..', 'data'))

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
            all_channels.append({
                'name': ch.get('name'),
                'tagline': ch.get('tagline'),
                'hls': ch.get('hls'),
                'is_live': ch.get('is_live'),
                'premium': ch.get('premium'),
                'jenis': ch.get('jenis'),
                'header_iptv': ch.get('header_iptv'),
                'url_license': ch.get('url_license'),
                'country': names.get(code, code),
                'code': code,
            })
        print(code, len(dd.get('info') or []))

    if not all_channels:
        print('FATAL: zero channels; keeping old data')
        sys.exit(1)

    seen = {}
    for ch in all_channels:
        seen.setdefault((ch['name'], ch['hls']), ch)
    uniq = list(seen.values())

    os.makedirs(OUT_DIR, exist_ok=True)
    updated = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    payload = {'updated': updated, 'count': len(uniq), 'channels': uniq}
    with open(os.path.join(OUT_DIR, 'channels.json'), 'w') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    def esc_m3u(s):
        return (s or '').replace('\n', ' ').strip()

    lines = ['#EXTM3U']
    for ch in sorted(uniq, key=lambda x: (x['country'] or '', x['name'] or '')):
        if ch['premium'] == 't':
            continue
        hls = ch['hls'] or ''
        if not hls.startswith('http'):
            continue
        gid = (ch['name'] or 'tv').lower().replace(' ', '')[:24]
        lines.append(f'#EXTINF:-1 tvg-id="{gid}" tvg-name="{esc_m3u(ch["name"])}" group-title="{esc_m3u(ch["country"])}",{esc_m3u(ch["name"])}')
        try:
            hdr = json.loads(ch['header_iptv'] or '{}')
        except Exception:
            hdr = {}
        ua = hdr.get('User-Agent')
        if ua and ua != 'none':
            lines.append(f'#EXTVLCOPT:http-user-agent={ua}')
            lines.append(f'#KODIPROP:http-user-agent={ua}')
        ref = hdr.get('Referer')
        if ref and ref != 'none':
            lines.append(f'#EXTVLCOPT:http-referrer={ref}')
        lines.append(hls)
    with open(os.path.join(OUT_DIR, 'playlist.m3u'), 'w') as f:
        f.write('\n'.join(lines) + '\n')

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
                   'failed': failed}, f, indent=1)
    print('unique:', len(uniq), 'failed:', failed)


if __name__ == '__main__':
    main()
