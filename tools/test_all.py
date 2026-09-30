#!/usr/bin/env python3
"""Tests for collect.py / health.py — run with:
    python -m unittest discover -s tools -p 'test_*.py'
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import collect          # noqa: E402
import health           # noqa: E402
from Crypto.Cipher import AES   # noqa: E402


def ch(**kw):
    base = {'name': 'Ch', 'country': 'X', 'premium': 'f',
            'hls': 'http://a/x.m3u8', 'header_iptv': '{}'}
    base.update(kw)
    return base


class TestConfig(unittest.TestCase):
    def test_config_decodes(self):
        for k in ('base', 'rc', 'app', 'pkey', 'len', 'crc', 'cert', 'cc'):
            self.assertIn(k, collect.CFG)

    def test_marker_is_stable(self):
        # regression: marker derivation must not change silently, it has to
        # keep matching what upstream publishes
        pkey = __import__('base64').b64decode(collect.CFG['pkey']).decode()
        self.assertEqual(collect.build_marker(pkey),
                         'fd5ca84d0866c105416ea5b3911f761e07c3031e')

    def test_decrypt_roundtrip(self):
        """Encrypt a payload the same way upstream does, then decrypt it."""
        cert = collect.D2(collect.CFG['cert'])
        key = collect.build_key(cert)
        payload = json.dumps({'info': [{'name': 'X', 'hls': 'http://y/z.m3u8'}],
                              'country': 'ZZ'}).encode()
        iv = b'0123456789abcdef'
        ct = AES.new(key, AES.MODE_CBC, iv).encrypt(
            payload + b' ' * (-len(payload) % 16))
        # wrap it exactly the way upstream ships it: reverse(base64(reverse(base64(iv+ct))))
        import base64 as b64
        dec = b64.b64encode(iv + ct).decode()
        shipped = b64.b64encode(dec[::-1].encode()).decode()[::-1]
        out = collect.decrypt_blob(shipped, key)
        self.assertIsNotNone(out, 'decrypt_blob must recover the payload')
        self.assertEqual(out['info'][0]['name'], 'X')


class TestPlaylist(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def read(self):
        with open(os.path.join(self.dir, 'playlist.m3u')) as f:
            return f.read()

    def test_counts_and_skips(self):
        chs = [ch(name='A'), ch(name='Dead', dead=True), ch(name='P', premium='t')]
        self.assertEqual(collect.write_playlist(chs, self.dir), 1)
        txt = self.read()
        self.assertIn('A', txt)
        self.assertNotIn('Dead', txt)
        self.assertNotIn(',P', txt)

    def test_crlf_stripped_everywhere(self):
        chs = [ch(name='Bad\r\nName')]
        self.assertEqual(collect.write_playlist(chs, self.dir), 1)
        txt = self.read()
        self.assertNotIn('\r', txt)
        # exactly one line per EXTINF block: no name may leak a newline
        for line in txt.split('\n'):
            if line.startswith('#EXTINF'):
                self.assertNotIn('\r', line)

    def test_tvg_id_sanitized(self):
        collect.write_playlist([ch(name='A\r\nB')], self.dir)
        line = [l for l in self.read().split('\n') if l.startswith('#EXTINF')][0]
        self.assertNotIn('\n', line)
        self.assertIn('tvg-id="ab"', line)

    def test_headers_emitted(self):
        h = json.dumps({'User-Agent': 'UA/1', 'Referer': 'https://r', 'Origin': 'https://o'})
        collect.write_playlist([ch(header_iptv=h)], self.dir)
        txt = self.read()
        self.assertIn('#EXTVLCOPT:http-user-agent=UA/1', txt)
        self.assertIn('#KODIPROP:http-user-agent=UA/1', txt)
        self.assertIn('#EXTVLCOPT:http-referrer=https://r', txt)
        self.assertIn('#KODIPROP:http-origin=https://o', txt)

    def test_none_headers_ignored(self):
        h = json.dumps({'User-Agent': 'none', 'Referer': 'none'})
        collect.write_playlist([ch(header_iptv=h)], self.dir)
        self.assertNotIn('EXTVLCOPT', self.read())

    def test_include_premium(self):
        chs = [ch(name='P', premium='t')]
        self.assertEqual(collect.write_playlist(chs, self.dir, include_premium=True), 1)
        self.assertEqual(collect.write_playlist(chs, self.dir), 0)

    def test_bad_header_json_does_not_crash(self):
        self.assertEqual(collect.write_playlist([ch(header_iptv='{oops')], self.dir), 1)

    def test_escaping(self):
        self.assertEqual(collect.esc_m3u('a\r\nb'), 'a b')
        self.assertEqual(collect.esc_m3u(None), '')


class TestHealthClassify(unittest.TestCase):
    def test_hls(self):
        self.assertEqual(health.classify(b'#EXTM3U\n#EXT-X-VERSION:3'), 'hls')

    def test_dash(self):
        self.assertEqual(health.classify(b'<?xml version="1.0"?><MPD xmlns="x">'), 'dash')

    def test_flv(self):
        self.assertEqual(health.classify(b'FLV\x01\x05\x00\x00\x00\x09'), 'flv')

    def test_ts(self):
        self.assertEqual(health.classify(b'\x47\x40\x00\x10\x00'), 'ts')

    def test_json_and_html(self):
        self.assertEqual(health.classify(b'{"message":"No API key"}'), 'json')
        self.assertEqual(health.classify(b'<!DOCTYPE html><html>'), 'html')

    def test_other(self):
        self.assertEqual(health.classify(b'\x00\x01\x02binary'), 'other')


class TestHealthVariants(unittest.TestCase):
    def test_variant_order_and_https_upgrade(self):
        labels = [v[0] for v in health.variants(ch(hls='http://a/x.m3u8'))]
        self.assertEqual(labels[0], 'declared')
        self.assertIn('slow', labels)
        self.assertIn('vlc', labels)
        self.assertIn('ffmpeg', labels)
        self.assertIn('no-ref', labels)
        self.assertIn('https', labels)

    def test_no_https_variant_for_https_url(self):
        labels = [v[0] for v in health.variants(ch(hls='https://a/x.m3u8'))]
        self.assertNotIn('https', labels)

    def test_declared_headers(self):
        h = json.dumps({'User-Agent': 'UA/9', 'Referer': 'none', 'host': 'h.example'})
        out = health.declared_headers({'header_iptv': h})
        self.assertEqual(out['User-Agent'], 'UA/9')
        self.assertNotIn('Referer', out)     # 'none' dropped
        self.assertIn('host', out)

    def test_check_channel_marks_failure(self):
        # no network: force probe to fail
        orig = health.probe
        health.probe = lambda *a, **k: (False, 'httperror', 404, 'x')
        try:
            _, status, variant = health.check_channel(ch())
        finally:
            health.probe = orig
        self.assertIsNone(variant)
        self.assertIn('404', status)

    def test_check_channel_repairs_with_variant(self):
        def fake_probe(url, headers, timeout):
            if headers.get('User-Agent', '').startswith('VLC'):
                return True, 'hls', 200, url
            return False, 'httperror', 403, 'x'
        orig = health.probe
        health.probe = fake_probe
        try:
            out, status, variant = health.check_channel(ch())
        finally:
            health.probe = orig
        self.assertEqual(variant, 'vlc')
        self.assertTrue(status.startswith('ok:'))
        hdr = json.loads(out['header_iptv'])
        self.assertTrue(hdr['User-Agent'].startswith('VLC'))  # repair persisted


class TestHealthConfirm(unittest.TestCase):
    def test_confirm_uses_persisted_headers(self):
        seen = {}

        def fake(url, headers, timeout):
            seen.update(headers)
            return True, 'hls', 200, url

        orig = health.probe
        health.probe = fake
        try:
            ok, st = health.confirm(
                ch(header_iptv=json.dumps({'User-Agent': 'UA/7', 'Referer': 'https://r'})))
        finally:
            health.probe = orig
        self.assertTrue(ok)
        self.assertEqual(st, 200)
        self.assertEqual(seen['User-Agent'], 'UA/7')
        self.assertEqual(seen['Referer'], 'https://r')

    def test_confirm_failure_reports_status(self):
        orig = health.probe
        health.probe = lambda *a, **k: (False, 'httperror', 403, 'x')
        try:
            ok, st = health.confirm(ch())
        finally:
            health.probe = orig
        self.assertFalse(ok)
        self.assertEqual(st, 403)

    def test_confirm_falls_back_to_browser_ua(self):
        seen = {}

        def fake(url, headers, timeout):
            seen.update(headers)
            return True, 'hls', 200, url

        orig = health.probe
        health.probe = fake
        try:
            health.confirm(ch(header_iptv='{}'))
        finally:
            health.probe = orig
        self.assertIn('User-Agent', seen)
        self.assertIn('Mozilla', seen['User-Agent'])


if __name__ == '__main__':
    unittest.main()
