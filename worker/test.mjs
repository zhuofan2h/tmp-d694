// Unit tests for worker/src/index.js — run with: node --test worker/test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { filterChannels, buildM3U, parseLimit, jsonResponse } from './src/index.js';

const CH = [
  { name: 'ANTV HD', code: 'ID', country: 'Indonesia', premium: 'f', hls: 'http://a/1.m3u8', header_iptv: '{}' },
  { name: 'HBO', code: 'ID', country: 'Indonesia', premium: 't', hls: 'http://a/2.mpd', header_iptv: '{"User-Agent":"UA/1","Referer":"https://r"}' },
  { name: 'TVRI', code: 'RI', country: 'TVRI', premium: 'f', hls: 'https://b/3.m3u8', header_iptv: '{}', dead: true },
  { name: 'Broken\r\nName', code: 'US', country: 'USA', premium: 'f', hls: 'https://c/4.m3u8', header_iptv: '{}' },
];
const q = o => new URLSearchParams(o);

test('json() forwards status to the Response (bug #1)', () => {
  const r = jsonResponse({ error: 'not found' }, { status: 404 });
  assert.equal(r.status, 404);
  assert.equal(r.headers.get('status'), null);          // must NOT leak into headers
  assert.equal(r.headers.get('content-type'), 'application/json; charset=utf-8');
  assert.equal(r.headers.get('access-control-allow-origin'), '*');
  assert.equal(jsonResponse({ ok: 1 }).status, 200);
  assert.equal(jsonResponse({ ok: 1 }, {}, true).status, 200); // HEAD has no body
  assert.equal(jsonResponse({ ok: 1 }, {}, true).headers.get('content-type'), 'application/json; charset=utf-8');
});

test('parseLimit never yields NaN (bug #3)', () => {
  assert.equal(parseLimit('abc'), null);
  assert.equal(parseLimit(''), null);
  assert.equal(parseLimit(null), null);
  assert.equal(parseLimit('10'), 10);
  assert.equal(parseLimit('0'), 0);
  assert.equal(parseLimit('-3'), null);
  const arr = CH.filter(() => true);
  const lim = parseLimit('abc');
  const out = lim !== null ? arr.slice(0, lim) : arr;
  assert.equal(out.length, arr.length); // bad limit = no slicing, not empty
});

test('default filter hides premium and dead', () => {
  const names = filterChannels(CH, q({})).map(c => c.name);
  assert.deepEqual(names.sort(), ['ANTV HD', 'Broken\r\nName']);
});

test('premium=1 returns everything live (compat), premium=only returns only premium (bug #4)', () => {
  // dead channel stays hidden unless dead=1 — even with premium=1
  assert.equal(filterChannels(CH, q({ premium: '1' })).length, 3);
  assert.equal(filterChannels(CH, q({ free: '0' })).length, 3);
  assert.equal(filterChannels(CH, q({ premium: '1', dead: '1' })).length, 4);
  const only = filterChannels(CH, q({ premium: 'only' }));
  assert.deepEqual(only.map(c => c.name), ['HBO']);
});

test('search=hbo now finds the channel when premium included (bug #4)', () => {
  assert.equal(filterChannels(CH, q({ search: 'hbo' })).length, 0);          // premium hidden by default
  assert.deepEqual(
    filterChannels(CH, q({ search: 'hbo', premium: 'only' })).map(c => c.name),
    ['HBO']);
  assert.equal(filterChannels(CH, q({ search: 'hbo', premium: '1' })).length, 1);
});

test('dead channels excluded by default, opt-in with dead=1 (bug #6)', () => {
  assert.ok(!filterChannels(CH, q({})).some(c => c.dead));
  const withDead = filterChannels(CH, q({ dead: '1' }));          // premium still hidden
  assert.equal(withDead.length, 3);
  assert.equal(withDead.filter(c => c.dead).length, 1);
  assert.equal(filterChannels(CH, q({ dead: '1', premium: '1' })).length, 4);
  assert.ok(filterChannels(CH, q({ dead: '1' })).some(c => c.name === 'TVRI'));
});

test('country + group + search filters still work', () => {
  assert.deepEqual(filterChannels(CH, q({ country: 'id,ID', premium: '1' })).map(c => c.name).sort(),
    ['ANTV HD', 'HBO']);
  assert.deepEqual(filterChannels(CH, q({ country: 'id' })).map(c => c.name), ['ANTV HD']);
  assert.equal(filterChannels(CH, q({ group: 'tvri', dead: '1' })).length, 1);
  assert.equal(filterChannels(CH, q({ search: 'tv' })).length, 1);   // ANTV HD; TVRI is dead
  assert.equal(filterChannels(CH, q({ search: 'tv', dead: '1' })).length, 2);
  assert.equal(filterChannels(CH, q({ search: 'zzz' })).length, 0);
});

test('buildM3U: skips dead, strips CR/LF, keeps headers (bug #6, #7)', () => {
  const m3u = buildM3U(filterChannels(CH, q({ dead: '1', premium: '1' })));
  const lines = m3u.split('\n');
  assert.equal(lines[0], '#EXTM3U');
  assert.ok(!m3u.includes('TVRI'), 'dead channel must not be published');
  assert.ok(!/\r/.test(m3u), 'no CR allowed');
  const extinf = lines.filter(l => l.startsWith('#EXTINF'));
  assert.ok(extinf.every(l => !l.includes('\n')));
  assert.ok(extinf.some(l => l.includes('Broken  Name') || l.includes('Broken Name')));
  // header lines preserved
  assert.ok(m3u.includes('#EXTVLCOPT:http-user-agent=UA/1'));
  assert.ok(m3u.includes('#EXTVLCOPT:http-referrer=https://r'));
  assert.ok(m3u.includes('#KODIPROP:http-origin=https://r') === false); // ANTV has no origin
  // every published URL is absolute http(s)
  const urls = lines.filter(l => l.startsWith('http'));
  assert.ok(urls.length >= 3);
  assert.ok(urls.every(u => /^https?:\/\//.test(u)));
});

test('buildM3U emits Origin as KODIPROP when present', () => {
  const ch = [{ name: 'X', country: 'Y', premium: 'f', hls: 'https://z/x.m3u8',
                header_iptv: '{"Origin":"https://o.example"}' }];
  assert.ok(buildM3U(ch).includes('#KODIPROP:http-origin=https://o.example'));
});
