#!/usr/bin/env python3
"""Tests for collect.py / health.py — run with:
    python -m unittest discover -s tools -p 'test_*.py'
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error

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


class TestParsePlaylist(unittest.TestCase):
    def test_parse_fixture(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'worker', 'testdata', 'playlist.m3u')
        entries = health.parse_playlist(path)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e['name'], 'Free One')
        self.assertEqual(e['url'], 'https://cdn.test/1.m3u8')
        self.assertEqual(e['headers'].get('User-Agent'), 'UA/1')
        self.assertEqual(e['headers'].get('Referer'), 'https://r')

    def test_parse_ignores_junk_lines(self):
        import tempfile
        txt = ('#EXTM3U\n#EXTINF:-1 tvg-id="a",A\n'
               '#EXTVLCOPT:http-user-agent=UA/1\n#KODIPROP:x=y\n'
               '#EXTINF:-1,B\nhttps://x/b.m3u8\n')
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'p.m3u')
        with open(p, 'w') as f:
            f.write(txt)
        entries = health.parse_playlist(p)
        self.assertEqual([e['name'] for e in entries], ['B'])
        self.assertEqual(entries[0]['url'], 'https://x/b.m3u8')


class TestHeaderForwarding(unittest.TestCase):
    def test_variants_forward_every_declared_header(self):
        h = json.dumps({'User-Agent': 'UA/5', 'Cookie': 'a=1',
                        'Referer': 'https://r', 'Origin': 'https://o'})
        declared = [v for v in health.variants(ch(header_iptv=h))
                    if v[0] == 'declared'][0][2]
        self.assertEqual(declared.get('Cookie'), 'a=1')     # the Trans7 bug
        self.assertEqual(declared.get('Referer'), 'https://r')
        self.assertEqual(declared.get('Origin'), 'https://o')
        self.assertEqual(declared['User-Agent'], 'UA/5')

    def test_no_ref_variant_keeps_cookie_drops_referrer(self):
        h = json.dumps({'User-Agent': 'UA/5', 'Cookie': 'a=1',
                        'Referer': 'https://r'})
        nr = [v for v in health.variants(ch(header_iptv=h))
              if v[0] == 'no-ref'][0][2]
        self.assertNotIn('Referer', nr)
        self.assertEqual(nr.get('Cookie'), 'a=1')

    def test_vlc_variant_still_keeps_cookie(self):
        h = json.dumps({'User-Agent': 'UA/5', 'Cookie': 'a=1'})
        vlc = [v for v in health.variants(ch(header_iptv=h))
               if v[0] == 'vlc'][0][2]
        self.assertTrue(vlc['User-Agent'].startswith('VLC'))
        self.assertEqual(vlc.get('Cookie'), 'a=1')


class TestGeoReason(unittest.TestCase):
    GEO = b'<H1>403 ERROR</H1> The Amazon CloudFront distribution is configured to ' \
          b'block access from your country.'

    def _patch_urlopen(self, body=None, exc=None):
        orig = health.urllib.request.urlopen

        def fake(*a, **k):
            if exc:
                raise exc
            class R:
                def read(self, n): return body
            return R()
        health.urllib.request.urlopen = fake
        return orig

    def test_geo_body_detected(self):
        import io
        err = urllib.error.HTTPError('http://x', 403, 'Forbidden', {}, io.BytesIO(self.GEO))
        orig = self._patch_urlopen(exc=err)
        try:
            self.assertEqual(health.geo_reason('http://x', {}), '403-geo')
        finally:
            health.urllib.request.urlopen = orig

    def test_plain_403_not_marked_geo(self):
        import io
        err = urllib.error.HTTPError('http://x', 403, 'Forbidden', {},
                                     io.BytesIO(b'<h1>403 Forbidden</h1><h1>nginx</h1>'))
        orig = self._patch_urlopen(exc=err)
        try:
            self.assertIsNone(health.geo_reason('http://x', {}))
        finally:
            health.urllib.request.urlopen = orig

    def test_check_channel_labels_403_geo(self):
        import io
        err = urllib.error.HTTPError('http://x', 403, 'Forbidden', {}, io.BytesIO(self.GEO))
        orig_probe, orig_url = health.probe, None
        health.probe = lambda *a, **k: (False, 'httperror', 403, 'x')
        orig_url = self._patch_urlopen(exc=err)
        try:
            _, status, variant = health.check_channel(ch())
        finally:
            health.probe = orig_probe
            health.urllib.request.urlopen = orig_url
        self.assertTrue(status.startswith('403-geo'), status)


class TestNoPlaylist(unittest.TestCase):
    def test_needs_extra_headers_detects_cookie(self):
        self.assertEqual(
            health.needs_extra_headers(
                ch(header_iptv=json.dumps({'User-Agent': 'UA', 'Cookie': 'a=1'}))),
            ['Cookie'])
        self.assertEqual(
            health.needs_extra_headers(
                ch(header_iptv=json.dumps({'User-Agent': 'UA',
                                           'Referer': 'https://r',
                                           'Origin': 'https://o'}))), [])
        self.assertEqual(
            health.needs_extra_headers(
                ch(header_iptv=json.dumps({'Cookie': 'none'}))), [])

    def test_needs_extra_headers_survives_bad_json(self):
        self.assertEqual(health.needs_extra_headers(ch(header_iptv='{oops')), [])

    def test_write_playlist_skips_no_playlist(self):
        d = tempfile.mkdtemp()
        chs = [ch(name='Alive'), ch(name='Cookie', no_playlist=True),
               ch(name='Dead', dead=True)]
        self.assertEqual(collect.write_playlist(chs, d), 1)
        txt = open(os.path.join(d, 'playlist.m3u')).read()
        self.assertIn('Alive', txt)
        self.assertNotIn('Cookie', txt)
        self.assertNotIn('Dead', txt)


class TestMultiVantage(unittest.TestCase):
    """Fragment / merge verdicts across checker locations."""

    @staticmethod
    def _frag(tmp, name, vantage, rows):
        path = os.path.join(tmp, name)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'vantage': vantage, 'results': rows}, f)
        return path

    def test_alive_if_any_vantage_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._frag(tmp, 'a.json', 'sg', [
                {'name': 'RTM', 'code': '1', 'ok': False, 'status': '403-geo:blocked'},
                {'name': 'Gone', 'code': '2', 'ok': False, 'status': '404'}])
            b = self._frag(tmp, 'b.json', 'my', [
                {'name': 'RTM', 'code': '1', 'ok': True, 'status': 'ok:declared'},
                {'name': 'Gone', 'code': '2', 'ok': False, 'status': '404'}])
            m = health.merge_fragments([a, b])
            self.assertTrue(m[('RTM', '1')]['ok'], 'one vantage passing must keep it alive')
            self.assertIn('my', m[('RTM', '1')]['vantages_ok'])
            self.assertFalse(m[('Gone', '2')]['ok'])
            self.assertEqual(m[('Gone', '2')]['status'], '404')

    def test_geo_label_survives_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._frag(tmp, 'a.json', 'sg', [
                {'name': 'X', 'code': '7', 'ok': False, 'status': '404'}])
            b = self._frag(tmp, 'b.json', 'id', [
                {'name': 'X', 'code': '7', 'ok': False, 'status': '403-geo:blocked'}])
            m = health.merge_fragments([a, b])
            self.assertTrue(m[('X', '7')]['status'].startswith('403-geo'),
                            'explicit geo verdict must upgrade the label')

    def test_fragment_paths_glob_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = self._frag(tmp, 'f-1.json', 'a', [])
            p2 = self._frag(tmp, 'f-2.json', 'b', [])
            self.assertEqual(health.fragment_paths(os.path.join(tmp, 'f-*.json')),
                             sorted([p1, p2]))
            self.assertEqual(health.fragment_paths(f'{p1}:{p2}'), sorted([p1, p2]))

    def test_write_fragment_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'nested', 'deep', 'frag.json')
            health.write_fragment(out, 'v', [({'name': 'n'}, 'ok:x', None)])
            self.assertTrue(os.path.exists(out))

    def test_write_fragment_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'frag.json')
            ch = {'name': 'A', 'code': 'x', 'hls': 'https://e/x.m3u8'}
            health.write_fragment(out, 'ubuntu-latest',
                                  [(ch, 'ok:declared', 'declared'),
                                   (ch, '403-geo:CF', None)])
            # write_fragment stores one row per result; rebuild via json
            health.write_fragment(out, 'ubuntu-latest', [(ch, 'ok:declared', 'declared')])
            fr = json.load(open(out, encoding='utf-8'))
            self.assertEqual(fr['vantage'], 'ubuntu-latest')
            self.assertTrue(fr['results'][0]['ok'])
            self.assertEqual(fr['results'][0]['name'], 'A')


class TestGeoMode(unittest.TestCase):
    """GEO_MODE=keep: geo-restricted entries stay publishable."""

    def test_verify_counts_geo_as_pass_when_keep(self):
        ch = {'name': 'G', 'code': 'g', 'premium': 'f',
              'hls': 'https://geo.example/x.m3u8', 'url': 'https://geo.example/x.m3u8',
              'header_iptv': '{}', 'group': 'XX'}
        with tempfile.TemporaryDirectory() as tmp:
            collect.write_playlist([ch], tmp)
            orig_probe, orig_geo, orig_sleep = (
                health.probe, health.geo_reason, health.time.sleep)
            health.time.sleep = lambda *_: None
            health.probe = lambda *a, **k: (False, 'httperror', 403, '')
            health.geo_reason = lambda *a, **k: '403-geo:country'
            health.GEO_MODE = 'keep'
            try:
                total, failed, geo_ok = health.verify_published(tmp)
                self.assertEqual(total, 1)
                self.assertEqual(failed, [], 'geo entry must not fail verify')
                self.assertEqual(len(geo_ok), 1)
            finally:
                health.probe, health.geo_reason, health.time.sleep = (
                    orig_probe, orig_geo, orig_sleep)
                health.GEO_MODE = os.environ.get('HEALTH_GEO_MODE', 'keep').lower()

    def test_verify_drops_geo_when_strict(self):
        ch = {'name': 'G', 'code': 'g', 'premium': 'f',
              'hls': 'https://geo.example/x.m3u8', 'url': 'https://geo.example/x.m3u8',
              'header_iptv': '{}', 'group': 'XX'}
        with tempfile.TemporaryDirectory() as tmp:
            collect.write_playlist([ch], tmp)
            orig_probe, orig_geo, orig_sleep = (
                health.probe, health.geo_reason, health.time.sleep)
            health.time.sleep = lambda *_: None
            health.probe = lambda *a, **k: (False, 'httperror', 403, '')
            health.geo_reason = lambda *a, **k: '403-geo:country'
            health.GEO_MODE = 'drop'
            try:
                total, failed, geo_ok = health.verify_published(tmp)
                self.assertEqual(len(failed), 1, 'strict mode must fail the entry')
                self.assertEqual(geo_ok, [])
            finally:
                health.probe, health.geo_reason, health.time.sleep = (
                    orig_probe, orig_geo, orig_sleep)
                health.GEO_MODE = os.environ.get('HEALTH_GEO_MODE', 'keep').lower()


class TestFragmentAndMerge(unittest.TestCase):
    """HEALTH_MODE=fragment and HEALTH_FRAGMENTS orchestration."""

    def test_merged_results_marks_geo_kept(self):
        ch = {'name': 'X', 'code': '7'}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'f.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'vantage': 'sg', 'results': [
                    {'name': 'X', 'code': '7', 'ok': False,
                     'status': '403-geo:CF'}]}, f)
            old = health.GEO_MODE
            health.GEO_MODE = 'keep'
            try:
                res = health.merged_results([ch], os.path.join(tmp, '*.json'))
                self.assertEqual(res[0][1], 'ok:geo-kept',
                                 'geo-fail must stay alive when mode=keep')
            finally:
                health.GEO_MODE = old

    def test_merged_results_dead_when_all_vantages_fail(self):
        ch = {'name': 'Y', 'code': '8'}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'f.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'vantage': 'sg', 'results': [
                    {'name': 'Y', 'code': '8', 'ok': False, 'status': '404'}]}, f)
            res = health.merged_results([ch], os.path.join(tmp, '*.json'))
            self.assertEqual(res[0][1], '404')

    def test_fragment_mode_writes_fragment_without_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'data')
            os.makedirs(out)
            ch = {'name': 'A', 'code': 'a', 'hls': 'https://e/a.m3u8'}
            with open(os.path.join(out, 'channels.json'), 'w') as f:
                json.dump({'channels': [ch]}, f)
            frag = os.path.join(tmp, 'frag', 'f.json')

            saved = (health.OUT_DIR, health.HEALTH_MODE,
                     os.environ.get('HEALTH_FRAGMENT'),
                     os.environ.get('HEALTH_VANTAGE'),
                     os.environ.get('HEALTH_FRAGMENTS'))
            saved_probe = health.probe_all
            health.OUT_DIR = out
            health.HEALTH_MODE = 'fragment'
            health.probe_all = lambda channels: [(c, 'ok:declared', None)
                                                 for c in channels]
            os.environ['HEALTH_FRAGMENT'] = frag
            os.environ['HEALTH_VANTAGE'] = 'unit'
            os.environ.pop('HEALTH_FRAGMENTS', None)
            try:
                rc = health.main()
                self.assertEqual(rc, 0, 'fragment mode must succeed')
                self.assertTrue(os.path.exists(frag), 'fragment file written')
                fr = json.load(open(frag, encoding='utf-8'))
                self.assertEqual(fr['vantage'], 'unit')
                self.assertTrue(fr['results'][0]['ok'])
                # never publishes: no playlist, no dead flags in data/
                self.assertFalse(os.path.exists(os.path.join(out, 'playlist.m3u')))
                reloaded = json.load(open(os.path.join(out, 'channels.json'),
                                          encoding='utf-8'))
                self.assertNotIn('dead', reloaded['channels'][0])
            finally:
                health.probe_all = saved_probe
                health.OUT_DIR, health.HEALTH_MODE = saved[0], saved[1]
                for key, val in (('HEALTH_FRAGMENT', saved[2]),
                                 ('HEALTH_VANTAGE', saved[3]),
                                 ('HEALTH_FRAGMENTS', saved[4])):
                    if val is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = val


class TestLocationDependent403(unittest.TestCase):
    """403 = decided by the VIEWER's IP, never killed by the checker's."""

    def test_403_family_never_kills_in_keep_mode(self):
        old = health.GEO_MODE
        health.GEO_MODE = 'keep'
        try:
            for st in (403, '403', '403-geo:CF', '403(vlc)', '403(httperror)'):
                self.assertTrue(health.location_dependent(st), f'{st!r} must be kept')
            for st in ('404', '400', '401', 'timeout', 'flaky:403', 'ok:hls'):
                self.assertFalse(health.location_dependent(st), f'{st!r} is not IP-dependent')
        finally:
            health.GEO_MODE = old

    def test_strict_drop_mode_kills_403(self):
        old = health.GEO_MODE
        health.GEO_MODE = 'drop'
        try:
            self.assertFalse(health.location_dependent(403))
        finally:
            health.GEO_MODE = old

    def test_verify_keeps_generic_403_when_keep(self):
        """Even a 403 with NO geo text in the body must stay published."""
        ch = {'name': 'G2', 'code': 'g2', 'premium': 'f',
              'hls': 'https://gen403.example/x.m3u8',
              'url': 'https://gen403.example/x.m3u8',
              'header_iptv': '{}', 'group': 'XX'}
        with tempfile.TemporaryDirectory() as tmp:
            collect.write_playlist([ch], tmp)
            orig = (health.probe, health.geo_reason, health.time.sleep,
                    health.GEO_MODE)
            health.time.sleep = lambda *_: None
            health.probe = lambda *a, **k: (False, 'httperror', 403, '')
            health.geo_reason = lambda *a, **k: None   # no geo text at all
            health.GEO_MODE = 'keep'
            try:
                total, failed, geo_ok = health.verify_published(tmp)
                self.assertEqual(failed, [], 'generic 403 must not drop the entry')
                self.assertEqual(len(geo_ok), 1)
                self.assertEqual(geo_ok[0][1], '403-blocked')
            finally:
                (health.probe, health.geo_reason, health.time.sleep,
                 health.GEO_MODE) = orig

    def test_cookie_channel_is_pipe_only_not_lost(self):
        """Cookie cannot ride #EXTVLCOPT, but the pipe style carries it:
        the channel leaves playlist.m3u and appears in playlist-pipe.m3u."""
        chs = [ch(name='CK', header_iptv=json.dumps({'User-Agent': 'UA/1',
                                                     'Cookie': 'a=1'}))]
        with tempfile.TemporaryDirectory() as tmp:
            counts = collect.write_all_playlists(chs, tmp)
            self.assertEqual(counts['vlc'], 0, 'vlc style must skip Cookie')
            self.assertEqual(counts['pipe'], 1, 'pipe style must keep it')
            vlc = open(os.path.join(tmp, 'playlist.m3u')).read()
            pipe = open(os.path.join(tmp, 'playlist-pipe.m3u')).read()
            self.assertNotIn(',CK', vlc)
            self.assertIn(',CK', pipe)
            self.assertIn('http://a/x.m3u8|User-Agent=UA/1&Cookie=a=1', pipe)

    def test_verify_attributes_failure_to_its_style(self):
        """A legacy vlc file containing a header-limited entry must be
        reported as failed (so the pipeline can move it to the pipe file)."""
        c = {'name': 'CK', 'code': 'ck', 'premium': 'f',
             'hls': 'https://cookie.example/x.m3u8',
             'url': 'https://cookie.example/x.m3u8',
             'header_iptv': json.dumps({'Cookie': 'a=1'}), 'group': 'XX'}
        with tempfile.TemporaryDirectory() as tmp:
            # legacy behaviour: entry present in the VLC file
            with open(os.path.join(tmp, 'playlist.m3u'), 'w',
                      encoding='utf-8') as f:
                f.write('#EXTM3U\n#EXTINF:-1,CK\n'
                        'https://cookie.example/x.m3u8\n')
            with open(os.path.join(tmp, 'channels.json'), 'w',
                      encoding='utf-8') as f:
                json.dump({'channels': [c]}, f)
            orig = (health.probe, health.probe_deep, health.time.sleep,
                    health.GEO_MODE)
            health.time.sleep = lambda *_: None
            health.probe = lambda *a, **k: (False, 'httperror', 403, '')
            health.probe_deep = health.probe
            health.GEO_MODE = 'keep'
            try:
                total, failed, geo_ok = health.verify_published(tmp)
                self.assertEqual(total, 1)
                self.assertEqual(len(failed), 1,
                                 'header-limited 403 must be dropped')
                self.assertEqual(failed[0][0]['style'], 'vlc')
                self.assertEqual(geo_ok, [])
            finally:
                (health.probe, health.probe_deep, health.time.sleep,
                 health.GEO_MODE) = orig

    def test_merged_results_keeps_plain_403(self):
        ch = {'name': 'P', 'code': 'p'}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'f.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'vantage': 'us', 'results': [
                    {'name': 'P', 'code': 'p', 'ok': False, 'status': '403'}]},
                    f)
            old = health.GEO_MODE
            health.GEO_MODE = 'keep'
            try:
                res = health.merged_results([ch], os.path.join(tmp, '*.json'))
                self.assertEqual(res[0][1], 'ok:geo-kept',
                                 'plain 403 must survive the merge as alive')
            finally:
                health.GEO_MODE = old


class TestGeoGrouping(unittest.TestCase):
    """geo-locked -> folder `<group> (geo)`, folder utama tetap bersih."""

    def test_geo_channel_gets_separate_folder(self):
        self.assertEqual(
            collect.group_of({'group': 'Indonesia', 'geo_limited': True}),
            'Indonesia (geo)')

    def test_normal_channel_keeps_its_group(self):
        self.assertEqual(collect.group_of({'group': 'Indonesia'}),
                         'Indonesia')
        self.assertEqual(collect.group_of({'code': 'ID'}), 'Indonesia')
        self.assertEqual(collect.group_of({'code': 'RI'}), 'Indonesia')

    def test_write_playlist_puts_geo_in_its_own_folder(self):
        chans = [{'name': 'A', 'hls': 'http://x/a.m3u8', 'group': 'Indonesia',
                  'header_iptv': '{}', 'code': 'ID', 'premium': 'f'},
                 {'name': 'B', 'hls': 'http://x/b.m3u8', 'group': 'Indonesia',
                  'header_iptv': '{}', 'code': 'ID', 'premium': 'f',
                  'geo_limited': True}]
        entries = collect.write_playlist(chans, self.tmp.name)
        self.assertEqual(entries, 2, 'both channels must be written')
        path = os.path.join(self.tmp.name,
                            collect.PLAYLIST_FILES['vlc'])
        body = open(path, encoding='utf-8').read()
        self.assertIn('group-title="Indonesia"', body)
        self.assertIn('group-title="Indonesia (geo)"', body)

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class TestCanonicalize(unittest.TestCase):
    """check_channel returns a repaired COPY — flags must reach the file."""

    def test_repaired_copy_is_folded_back(self):
        channels = [{'name': 'A', 'code': 'x', 'hls': 'http://old/a.m3u8',
                     'header_iptv': '{}'},
                    {'name': 'B', 'code': 'y', 'hls': 'http://old/b.m3u8',
                     'header_iptv': '{}'}]
        repaired = dict(channels[0], hls='http://new/a.m3u8')
        results = [(repaired, 'ok:declared', 'vlc'), (channels[1], '404', None)]
        out = health.canonicalize(results, channels)
        self.assertIs(out[0][0], channels[0], 'must return the canonical object')
        self.assertEqual(channels[0]['hls'], 'http://new/a.m3u8')
        # flags set on the returned object must land on the canonical record
        out[0][0]['drm'] = True
        out[0][0]['pipe_only'] = True
        self.assertTrue(channels[0]['drm'])
        self.assertTrue(channels[0]['pipe_only'])
        self.assertIs(out[1][0], channels[1])

    def test_length_mismatch_is_left_alone(self):
        results = [('a', 'ok', None)]
        self.assertEqual(health.canonicalize(results, []), results)


class TestPlayerCompat(unittest.TestCase):
    """Two playlist styles + the guarantees players rely on."""

    def test_malformed_header_json_is_repaired(self):
        raw = ('{"Referer":"https://www.visionplus.id/",'
               '"Origin":"https://www.visionplus.id/",'
               '"Mozilla/5.0 (Linux; Android 13) Chrome/112.0.0.0"}')
        hdr = collect.normalize_headers(raw)
        self.assertEqual(hdr.get('Origin'), 'https://www.visionplus.id/')
        self.assertTrue(hdr.get('User-Agent', '').startswith('Mozilla/5.0'))
        # a WELL-formed header must not be rewritten
        ok = json.dumps({'User-Agent': 'Mozilla/5.0 X', 'Referer': 'https://r'})
        self.assertEqual(collect.normalize_headers(ok),
                         {'User-Agent': 'Mozilla/5.0 X', 'Referer': 'https://r'})

    def test_repair_header_json_keeps_valid_json_untouched(self):
        raw = '{"User-Agent":"Mozilla/5.0","Referer":"https://r"}'
        self.assertEqual(collect.repair_header_json(raw), raw)

    def test_none_and_empty_headers_dropped(self):
        self.assertEqual(collect.normalize_headers(
            json.dumps({'User-Agent': 'none', 'Referer': '', 'X': '1'})),
            {'X': '1'})

    def test_indonesian_groups_are_merged(self):
        for code, country in (('ID', 'Indonesia'), ('RI', 'TVRI'),
                              ('LO', 'TV Lokal')):
            c = {'code': code, 'country': country, 'name': 'N',
                 'hls': 'http://a/x.m3u8', 'premium': 'f'}
            self.assertEqual(collect.group_of(c), 'Indonesia',
                             f'{code}/{country} must land in Indonesia')
        other = {'code': 'BR', 'country': 'Brazil', 'name': 'N'}
        self.assertEqual(collect.group_of(other), 'Brazil')
        # a stored group always wins
        self.assertEqual(collect.group_of({'group': 'Kids', 'country': 'X'}),
                         'Kids')

    def test_group_title_written_into_playlist(self):
        chs = [ch(name='TVRI Aceh', country='TVRI', code='RI'),
               ch(name='Bandung TV', country='TV Lokal', code='LO'),
               ch(name='MetroTV', country='Indonesia', code='ID')]
        with tempfile.TemporaryDirectory() as tmp:
            collect.write_all_playlists(chs, tmp)
            txt = open(os.path.join(tmp, 'playlist.m3u')).read()
            self.assertEqual(txt.count('group-title="Indonesia"'), 3)

    def test_pipe_url_format_and_escaping(self):
        hdr = {'User-Agent': 'UA/1', 'Referer': 'https://r',
               'Cookie': 'a=1; b=2'}
        self.assertEqual(
            collect.pipe_url('https://x/a.m3u8', hdr),
            'https://x/a.m3u8|User-Agent=UA/1&Referer=https://r&Cookie=a=1; b=2')
        # a `|` or `&` inside a value must not break the grammar
        out = collect.pipe_url('https://x/a.m3u8', {'User-Agent': 'a|b&c'})
        self.assertEqual(out.count('|'), 1)
        self.assertIn('a%7Cb%26c', out)

    def test_pipe_playlist_carries_every_header(self):
        with_cookie = ch(name='CK', header_iptv=json.dumps(
            {'User-Agent': 'UA/2', 'Referer': 'https://r',
             'Origin': 'https://o', 'Cookie': 'k=v'}))
        plain = ch(name='PL', header_iptv=json.dumps(
            {'User-Agent': 'UA/2', 'Referer': 'https://r',
             'Origin': 'https://o'}))
        with tempfile.TemporaryDirectory() as tmp:
            counts = collect.write_all_playlists([with_cookie, plain], tmp)
            self.assertEqual(counts['pipe'], 2)
            self.assertEqual(counts['vlc'], 1, 'Cookie entry leaves the vlc file')
            pipe = open(os.path.join(tmp, 'playlist-pipe.m3u')).read()
            line = [l for l in pipe.split('\n')
                    if l.startswith('http') and 'x.m3u8' in l]
            suffix = next(l.split('|', 1)[1] for l in line if 'Cookie' in l)
            self.assertEqual(suffix.split('&'),
                             ['User-Agent=UA/2', 'Referer=https://r',
                              'Origin=https://o', 'Cookie=k=v'])
            # VLC-style file still uses tags + plain URL
            vlc = open(os.path.join(tmp, 'playlist.m3u')).read()
            self.assertIn('#EXTVLCOPT:http-user-agent=UA/2', vlc)
            self.assertNotIn(',CK', vlc)
            self.assertNotIn('|', [l for l in vlc.split('\n')
                                   if l.startswith('http')][0])

    def test_parse_pipe_playlist_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            collect.write_all_playlists(
                [ch(name='T', header_iptv=json.dumps(
                    {'User-Agent': 'UA/3', 'Cookie': 'a=1'}))], tmp)
            entries = health.parse_playlist(
                os.path.join(tmp, 'playlist-pipe.m3u'))
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['url'], 'http://a/x.m3u8')
            self.assertEqual(entries[0]['headers']['User-Agent'], 'UA/3')
            self.assertEqual(entries[0]['headers']['Cookie'], 'a=1')

    def test_drm_channels_are_never_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            counts = collect.write_all_playlists(
                [ch(name='Secret', drm=True), ch(name='Open')], tmp)
            self.assertEqual(counts['vlc'], 1)
            self.assertEqual(counts['pipe'], 1)
            for f in ('playlist.m3u', 'playlist-pipe.m3u'):
                txt = open(os.path.join(tmp, f)).read()
                self.assertNotIn(',Secret', txt)
                self.assertIn(',Open', txt)

    def test_needs_extra_headers_vs_no_playlist(self):
        cookie = ch(header_iptv=json.dumps({'User-Agent': 'UA',
                                            'Cookie': 'a=1'}))
        self.assertEqual(health.needs_extra_headers(cookie), ['Cookie'])
        self.assertEqual(health.needs_no_playlist(cookie), [])
        broken = ch(header_iptv=json.dumps({'User-Agent': 'a|b'}))
        self.assertEqual(health.needs_no_playlist(broken), ['User-Agent'])

    def test_probe_deep_follows_to_segment(self):
        pages = {
            'https://h/master.m3u8': b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvar.m3u8\n',
            'https://h/var.m3u8': b'#EXTM3U\n#EXT-X-TARGETDURATION:4\n'
                                  b'#EXTINF:4.0,\nseg1.ts\n',
            'https://h/seg1.ts': b'\x47\x40\x00\x10payload',
        }
        orig_body, orig_probe = health.fetch_body, health.probe
        health.probe = lambda url, hdr, t: (True, 'hls', 200, url)
        health.fetch_body = lambda url, hdr, t, limit=65536: (
            200, pages[url], url) if url in pages else (404, b'', url)
        try:
            ok, kind, status, _ = health.probe_deep(
                'https://h/master.m3u8', {}, 5)
            self.assertTrue(ok, 'manifest + segment must both be followed')
            self.assertEqual(kind, 'hls+media')
        finally:
            health.fetch_body, health.probe = orig_body, orig_probe

    def test_probe_deep_rejects_html_segment(self):
        pages = {
            'https://h2/m.m3u8': b'#EXTM3U\n#EXTINF:4,\nseg.ts\n',
            'https://h2/seg.ts': b'<!DOCTYPE html><html>blocked</html>',
        }
        orig_body, orig_probe = health.fetch_body, health.probe
        health.probe = lambda url, hdr, t: (True, 'hls', 200, url)
        health.fetch_body = lambda url, hdr, t, limit=65536: (
            200, pages[url], url) if url in pages else (404, b'', url)
        try:
            ok, kind, status, _ = health.probe_deep('https://h2/m.m3u8', {}, 5)
            self.assertFalse(ok, 'an HTML segment is a playback error')
            self.assertEqual(kind, 'sub-html')
        finally:
            health.fetch_body, health.probe = orig_body, orig_probe

    def test_drm_check_reads_content_protection(self):
        orig = health.fetch_body
        health.fetch_body = lambda url, hdr, t, limit=65536: (
            200, b'<MPD><ContentProtection/></MPD>', url)
        try:
            self.assertTrue(health.drm_check(ch(hls='https://d/x.mpd')))
            health.fetch_body = lambda url, hdr, t, limit=65536: (
                200, b'<MPD><BaseURL/></MPD>', url)
            self.assertFalse(health.drm_check(ch(hls='https://d/x.mpd')))
            health.fetch_body = lambda url, hdr, t, limit=65536: (403, b'', url)
            self.assertIsNone(health.drm_check(ch(hls='https://d/x.mpd')))
        finally:
            health.fetch_body = orig


if __name__ == '__main__':
    unittest.main()
