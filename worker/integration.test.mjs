// Integration test: runs the actual Worker fetch handler with mocked
// `caches` + `fetch` against the fixtures in testdata/.
// Run with: node --test worker/integration.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_BASE = 'https://fixture.test/data/';

// --- mocks -------------------------------------------------------------
// NOTE: a real Cloudflare cache returns a FRESH Response on every match();
// storing one Response object would let its body be consumed only once.
const store = new Map();
globalThis.caches = {
  default: {
    async match(req) {
      const e = store.get(req.url);
      if (!e) return undefined;
      return new Response(e.body, { status: e.status, headers: e.headers });
    },
    async put(req, res) {
      const body = await res.clone().text();
      store.set(req.url, { body, status: res.status, headers: Object.fromEntries(res.headers) });
    },
  },
};
globalThis.fetch = async (input) => {
  const url = typeof input === 'string' ? input : input.url;
  if (!url.startsWith(FIXTURE_BASE)) throw new Error('unexpected fetch ' + url);
  const name = url.slice(FIXTURE_BASE.length);
  const body = readFileSync(join(__dirname, 'testdata', name), 'utf8');
  const type = name.endsWith('.m3u') ? 'audio/x-mpegurl' : 'application/json';
  return new Response(body, { status: 200, headers: { 'content-type': type } });
};

const { default: worker } = await import('./src/index.js');
const env = { DATA_BASE: FIXTURE_BASE };
const ctx = { waitUntil() {} };
const call = (path, init) => worker.fetch(new Request('https://iptv.test' + path, init), env, ctx);

// --- tests -------------------------------------------------------------
test('unknown path returns real HTTP 404 (bug #1 regression)', async () => {
  const r = await call('/tidakada');
  assert.equal(r.status, 404);
  assert.equal(r.headers.get('status'), null);
  const body = await r.json();
  assert.equal(body.error, 'not found');
  assert.ok(Array.isArray(body.endpoints));
});

test('POST is rejected with 405 (bug #5)', async () => {
  const r = await call('/api/stats', { method: 'POST' });
  assert.equal(r.status, 405);
  assert.equal(r.headers.get('allow'), 'GET, HEAD');
});

test('HEAD returns headers without a body', async () => {
  const r = await call('/health', { method: 'HEAD' });
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('content-type'), 'application/json; charset=utf-8');
  assert.equal(await r.text(), '');
});

test('health endpoint works', async () => {
  const r = await call('/health');
  assert.equal(r.status, 200);
  assert.equal((await r.json()).ok, true);
});

test('api/channels default = free only, dead excluded', async () => {
  const r = await call('/api/channels');
  const b = await r.json();
  assert.equal(r.status, 200);
  assert.deepEqual(b.channels.map(c => c.name), ['Free One', 'Free Two']);
});

test('limit=abc does not empty the result (bug #3)', async () => {
  const b = await (await call('/api/channels?limit=abc')).json();
  assert.equal(b.count, 2);
  const b2 = await (await call('/api/channels?limit=1')).json();
  assert.equal(b2.count, 1);
});

test('premium=only + search finds premium channels (bug #4)', async () => {
  const b = await (await call('/api/channels?search=hbo&premium=only')).json();
  assert.deepEqual(b.channels.map(c => c.name), ['HBO', 'HBO Hits']);
  const all = await (await call('/api/channels?premium=1')).json();
  assert.equal(all.count, 4);                  // live channels only
  const withDead = await (await call('/api/channels?premium=1&dead=1')).json();
  assert.equal(withDead.count, 5);             // dead opt-in (bug #6)
});

test('country filter', async () => {
  const b = await (await call('/api/channels?country=MY')).json();
  assert.deepEqual(b.channels.map(c => c.name), ['Free Two']);
});

test('raw playlist.m3u served verbatim when unfiltered', async () => {
  const r = await call('/playlist.m3u');
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('content-type'), 'audio/x-mpegurl; charset=utf-8');
  const txt = await r.text();
  assert.ok(txt.startsWith('#EXTM3U'));
  assert.ok(txt.includes('https://cdn.test/1.m3u8'));
});

test('filtered playlist never contains dead or premium entries', async () => {
  const txt = await (await call('/m3u?country=ID')).text();
  assert.ok(txt.startsWith('#EXTM3U'));
  assert.ok(txt.includes('Free One'));
  assert.ok(!txt.includes('dead.m3u8'), 'dead channel leaked into playlist');
  assert.ok(!txt.includes('hbo.mpd'), 'premium leaked into default playlist');
  const lines = txt.trim().split('\n');
  const urls = lines.filter(l => l.startsWith('http'));
  assert.equal(urls.length, 1);
});

test('502 when upstream fixture is missing', async () => {
  const r = await worker.fetch(
    new Request('https://iptv.test/api/stats'),
    { DATA_BASE: 'https://missing.test/' }, ctx);
  assert.equal(r.status, 502);
});
