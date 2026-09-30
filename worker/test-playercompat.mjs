
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildM3U, filterChannels } from './src/index.js';

// --- player-compat: two styles, Indonesian group, DRM -----------------------
test('buildM3U merges Indonesian pseudo-groups into one group-title', () => {
  const chans = [
    { name: 'MetroTV', code: 'ID', country: 'Indonesia', premium: 'f', hls: 'https://a/1.m3u8', header_iptv: '{}' },
    { name: 'TVRI Aceh', code: 'RI', country: 'TVRI', premium: 'f', hls: 'https://a/2.m3u8', header_iptv: '{}' },
    { name: 'JakTV', code: 'LO', country: 'TV Lokal', premium: 'f', hls: 'https://a/3.m3u8', header_iptv: '{}' },
    { name: 'Globo', code: 'BR', country: 'Brazil', premium: 'f', hls: 'https://a/4.m3u8', header_iptv: '{}' },
  ];
  const m3u = buildM3U(chans);
  assert.equal((m3u.match(/group-title="Indonesia"/g) || []).length, 3);
  assert.ok(m3u.includes('group-title="Brazil"'));
  // an explicit group field always wins
  assert.ok(buildM3U([{ name: 'K', group: 'Kids', country: 'X', hls: 'https://a/5.m3u8' }])
    .includes('group-title="Kids"'));
});

test('buildM3U pipe style carries every header as URL|Name=value', () => {
  const withCookie = { name: 'Trans7', code: 'ID', country: 'Indonesia', premium: 'f',
                       hls: 'https://a/t.m3u8',
                       header_iptv: '{"User-Agent":"UA/9","Referer":"https://r","Cookie":"k=v"}' };
  const plain = { name: 'MetroTV', code: 'ID', country: 'Indonesia', premium: 'f',
                  hls: 'https://a/m.m3u8',
                  header_iptv: '{"User-Agent":"UA/9","Referer":"https://r"}' };
  const chans = [withCookie, plain];
  const pipe = buildM3U(chans, 'pipe');
  const cookieUrl = pipe.split('\n').find(l => l.includes('Cookie='));
  assert.equal(cookieUrl,
    'https://a/t.m3u8|User-Agent=UA/9&Referer=https://r&Cookie=k=v');
  // VLC/Kodi style must NOT get the pipe suffix (VLC cannot parse it)
  const vlc = buildM3U(chans, 'vlc');
  const vlcUrls = vlc.split('\n').filter(l => l.startsWith('http'));
  assert.deepEqual(vlcUrls, ['https://a/m.m3u8'],
    'plain URL only, and the Cookie channel is not in this file');
  assert.ok(vlc.includes('#EXTVLCOPT:http-user-agent=UA/9'));
  assert.ok(!vlc.includes(',Trans7'));
  assert.ok(pipe.includes(',Trans7'));
});

test('buildM3U: drm / pipe_only / no_playlist rules per style', () => {
  const chans = [
    { name: 'Secret', country: 'X', premium: 'f', hls: 'https://a/1.mpd', header_iptv: '{}', drm: true },
    { name: 'CookieOnly', country: 'X', premium: 'f', hls: 'https://a/2.m3u8',
      header_iptv: '{"Cookie":"a=1"}', pipe_only: true },
    { name: 'Never', country: 'X', premium: 'f', hls: 'https://a/3.m3u8', header_iptv: '{}', no_playlist: true },
    { name: 'Ok', country: 'X', premium: 'f', hls: 'https://a/4.m3u8', header_iptv: '{}' },
  ];
  const vlc = buildM3U(chans, 'vlc');
  const pipe = buildM3U(chans, 'pipe');
  for (const m3u of [vlc, pipe]) {
    assert.ok(!m3u.includes(',Secret'), 'DRM must never be published');
    assert.ok(!m3u.includes(',Never'), 'no_playlist must never be published');
    assert.ok(m3u.includes(',Ok'));
  }
  assert.ok(!vlc.includes(',CookieOnly'), 'pipe_only stays out of the vlc file');
  assert.ok(pipe.includes(',CookieOnly'));
  assert.ok(pipe.includes('https://a/2.m3u8|Cookie=a=1'));
});

test('filterChannels: country=ID covers every Indonesian channel, not only code ID', () => {
  const chans = [
    { name: 'A', code: 'ID', group: 'Indonesia', country: 'Indonesia', premium: 'f' },
    { name: 'B', code: 'RI', group: 'Indonesia', country: 'TVRI', premium: 'f' },
    { name: 'C', code: 'LO', group: 'Indonesia', country: 'TV Lokal', premium: 'f' },
    { name: 'D', code: 'MY', country: 'Malaysia', premium: 'f' },
  ];
  const out = filterChannels(chans, new URLSearchParams('country=ID'));
  assert.deepEqual(out.map(c => c.name), ['A', 'B', 'C']);
  const my = filterChannels(chans, new URLSearchParams('country=MY'));
  assert.deepEqual(my.map(c => c.name), ['D']);
});

test('filterChannels: group=Indonesia finds the merged folder', () => {
  const chans = [
    { name: 'A', code: 'ID', group: 'Indonesia', country: 'Indonesia', premium: 'f' },
    { name: 'D', code: 'MY', country: 'Malaysia', premium: 'f' },
  ];
  const out = filterChannels(chans, new URLSearchParams('group=Indonesia'));
  assert.deepEqual(out.map(c => c.name), ['A']);
});

test('geo-locked channel lands in its own "<group> (geo)" folder', () => {
  const geo = buildM3U([{ name: 'X', hls: 'https://a/x.m3u8', group: 'Indonesia',
    header_iptv: '{}', geo_limited: true }], 'pipe');
  assert.ok(geo.includes('group-title="Indonesia (geo)"'), geo);
  const plain = buildM3U([{ name: 'Y', hls: 'https://a/y.m3u8', group: 'Indonesia',
    header_iptv: '{}' }], 'pipe');
  assert.ok(plain.includes('group-title="Indonesia"'), plain);
  assert.ok(!plain.includes('(geo)'), plain);
});

test('buildM3U repairs a malformed header_iptv (UA used as key)', () => {
  const raw = '{"Referer":"https://www.visionplus.id/","Origin":"https://www.visionplus.id/",'
            + '"Mozilla/5.0 (Linux; Android 13) Chrome/112.0.0.0"}';
  const m3u = buildM3U([{ name: 'Cineedge', country: 'X', premium: 'f',
                          hls: 'https://a/c.mpd', header_iptv: raw }], 'pipe');
  assert.ok(m3u.includes('#EXTVLCOPT:http-user-agent=Mozilla/5.0 (Linux; Android 13) Chrome/112.0.0.0'));
  assert.ok(m3u.includes('User-Agent=Mozilla/5.0'));
  // well-formed JSON is left exactly as it is
  const ok = buildM3U([{ name: 'A', country: 'X', premium: 'f', hls: 'https://a/a.m3u8',
                         header_iptv: '{"User-Agent":"UA/1"}' }], 'pipe');
  assert.ok(ok.includes('https://a/a.m3u8|User-Agent=UA/1'));
});
