// IPTV playlist/API Worker — serves data from GitHub raw (free tier, no KV needed)
//
// Fixes vs original:
//  1. json() now forwards `status` to the Response (it used to leak into headers,
//     so every error answered with HTTP 200 + a bogus `status: 404` header).
//  2. DATA_BASE pointed at a deleted repo (quiet-summit-8ck88) -> 404/502.
//     Now points at this repo and can be overridden via env DATA_BASE.
//  3. `?limit=abc` used to return count 0 (NaN slice) -> limit is parsed safely.
//  4. `premium=1` used to mean "include premium" only; there was no way to ask
//     for premium-only, so `?search=hbo` returned 0. Added `premium=only` and
//     `free=only`, kept `premium=1`/`free=0` semantics for compatibility.
//  5. No method check -> POST was accepted. Now only GET/HEAD (405 otherwise).
//  6. Channels marked `dead` (failing health check) are excluded by default;
//     `?dead=1` opts back in.
//  7. \r and \n stripped from playlist fields (broken rows in strict players).

const DEFAULT_DATA_BASE = 'https://raw.githubusercontent.com/zhuofan2h/tmp-d694/main/data/';
const TTL = 900; // 15 min edge cache

// Upstream labels Indonesian content with pseudo-country codes; players show
// one folder per group-title, so keep every Indonesian channel together.
const GROUP_ALIASES = { TVRI: 'Indonesia', 'TV Lokal': 'Indonesia' };
const CODE_GROUPS = { ID: 'Indonesia', RI: 'Indonesia', LO: 'Indonesia' };
const VLC_HEADER_KEYS = new Set(['user-agent', 'referer', 'origin']);

const JSON_HEADERS = {
  'content-type': 'application/json; charset=utf-8',
  'access-control-allow-origin': '*',
  'cache-control': `public, max-age=${TTL}`,
};
const M3U_HEADERS = {
  'content-type': 'audio/x-mpegurl; charset=utf-8',
  'access-control-allow-origin': '*',
  'cache-control': `public, max-age=${TTL}`,
};

function dataBaseOf(env) {
  return (env && env.DATA_BASE) || globalThis.DATA_BASE_OVERRIDE || DEFAULT_DATA_BASE;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const q = url.searchParams;
    const path = url.pathname.replace(/\/+$/, '') || '/';
    const method = (request.method || 'GET').toUpperCase();
    const head = method === 'HEAD';

    if (method !== 'GET' && method !== 'HEAD') {
      return json({ error: 'method not allowed', allow: 'GET, HEAD' }, { status: 405, allow: 'GET, HEAD' });
    }

    const base = dataBaseOf(env);
    try {
      if (path === '/health') {
        return json({ ok: true, ts: new Date().toISOString() }, {}, head);
      }

      if (path === '/playlist.m3u' || path === '/m3u' || path === '/playlist'
          || path === '/playlist-pipe.m3u') {
        const pipe = path === '/playlist-pipe.m3u' || q.get('style') === 'pipe';
        if (!q.toString() || (pipe && q.toString() === 'style=pipe')) {
          // canonical files (pre-verified by the health pipeline)
          const raw = await fetchText(pipe ? 'playlist-pipe.m3u' : 'playlist.m3u',
                                      ctx, base);
          return text(raw, M3U_HEADERS, head);
        }
        // filtered playlist: re-derive from channels.json
        const data = await fetchJSON('channels.json', ctx, base);
        const chans = filterChannels(data.channels, q);
        return text(buildM3U(chans, pipe ? 'pipe' : 'vlc'), M3U_HEADERS, head);
      }

      if (path === '/api/channels') {
        const data = await fetchJSON('channels.json', ctx, base);
        let chans = filterChannels(data.channels, q);
        const lim = parseLimit(q.get('limit'));
        if (lim !== null) chans = chans.slice(0, lim);
        return json({ updated: data.updated, count: chans.length, channels: chans }, {}, head);
      }

      if (path === '/api/countries') {
        return json(await fetchJSON('countries.json', ctx, base), {}, head);
      }

      if (path === '/api/stats') {
        const [stats, countries] = await Promise.all([
          fetchJSON('stats.json', ctx, base),
          fetchJSON('countries.json', ctx, base),
        ]);
        return json({
          ...stats,
          counts: countries.counts,
          per_country_total: Object.keys(countries.counts || {}).length,
        }, {}, head);
      }

      return json({
        error: 'not found',
        endpoints: [
          '/playlist.m3u',
          '/playlist-pipe.m3u',
          '/m3u?country=ID&search=tv',
          '/m3u?country=ID&style=pipe',
          '/m3u?premium=only',
          '/api/channels?country=ID&limit=100',
          '/api/countries',
          '/api/stats',
          '/health',
        ],
      }, { status: 404 });
    } catch (e) {
      return json({ error: String(e) }, { status: 502 });
    }
  },
};

// status goes to the Response, everything else becomes a header
function json(obj, extra = {}, head = false) {
  const { status, ...headers } = extra || {};
  return new Response(head ? null : JSON.stringify(obj), {
    status: status || 200,
    headers: { ...JSON_HEADERS, ...headers },
  });
}
function text(s, hdrs, head = false) {
  return new Response(head ? null : s, { headers: hdrs });
}

function parseLimit(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number.parseInt(v, 10);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

async function fetchJSON(name, ctx, base) {
  const data = await cached(name, ctx, base);
  return data.json();
}
async function fetchText(name, ctx, base) {
  const r = await cached(name, ctx, base);
  return r.text();
}
async function cached(name, ctx, base) {
  const cache = caches.default;
  const key = new Request(base + name);
  let hit = await cache.match(key);
  if (hit) return hit;
  const r = await fetch(key, { cf: { cacheTtl: TTL, cacheEverything: true } });
  if (!r.ok) throw new Error(`upstream ${r.status} for ${name}`);
  const store = r.clone();
  ctx.waitUntil(cache.put(key, store));
  return r;
}

function filterChannels(chans, q) {
  let out = chans;
  const wantDead = q.get('dead') === '1' || q.get('include_dead') === '1';
  if (!wantDead) out = out.filter(c => !c.dead);

  const country = q.get('country');
  if (country) {
    const set = new Set(country.split(',').map(s => s.trim().toLowerCase()));
    out = out.filter(c => set.has((c.code || '').toLowerCase()));
  }
  const group = q.get('group');
  if (group) {
    const g = group.toLowerCase();
    out = out.filter(c => (c.country || '').toLowerCase().includes(g));
  }
  const search = q.get('search');
  if (search) {
    const s = search.toLowerCase();
    out = out.filter(c => (c.name || '').toLowerCase().includes(s));
  }

  const premium = q.get('premium');
  const free = q.get('free');
  if (premium === 'only') {
    out = out.filter(c => c.premium === 't');      // hanya premium
  } else if (premium === '1' || free === '0') {
    // semua (free + premium) — perilaku lama dipertahankan
  } else if (free === 'only') {
    out = out.filter(c => c.premium !== 't');
  } else {
    out = out.filter(c => c.premium !== 't');      // default: free saja
  }
  return out;
}

// header_iptv -> clean object; also repairs upstream's malformed variant where
// the User-Agent is a KEY with no value (invalid JSON that used to parse as {}).
function normalizeHeaders(raw) {
  let obj = null;
  if (raw && typeof raw === 'object') obj = raw;
  else {
    const s = (raw || '').trim();
    if (!s) return {};
    try {
      obj = JSON.parse(s);
    } catch {
      try {
        obj = JSON.parse(s.replace(
          /([,{])\s*"((?:Mozilla|okhttp|Dalvik|Lavf\/|VLC\/|ExoPlayer|curl\/)[^"]*)"\s*(?=[,}])/g,
          '$1"User-Agent":"$2"'));
      } catch {
        return {};
      }
    }
  }
  const uaLike = (s) => /^(Mozilla\/|okhttp|Dalvik|Lavf\/|VLC\/|ExoPlayer|curl\/)/i.test(String(s || ''));
  const out = {};
  for (const [k0, v0] of Object.entries(obj || {})) {
    const k = String(k0).trim();
    if (!k || v0 === null || v0 === undefined || typeof v0 === 'object') continue;
    const v = String(v0).trim();
    if (uaLike(k)) {
      if (!out['User-Agent']) out['User-Agent'] = uaLike(v) ? v : k;
      continue;
    }
    if (!v || v.toLowerCase() === 'none') continue;
    out[k] = v;
  }
  return out;
}

function groupOf(ch) {
  let g = ch.group;
  if (!g) {
    if (CODE_GROUPS[ch.code]) {
      g = CODE_GROUPS[ch.code];
    } else {
      const c = ch.country || '';
      g = GROUP_ALIASES[c] || c;
    }
  }
  // geo-locked: 403 dari vantage pengece (mungkin hidup dari jaringan yang
  // ditujunya) -> folder terpisah, folder utama tetap 100% terverifikasi
  if (ch.geo_limited) return `${g} (geo)`;
  return g;
}

// `URL|User-Agent=..&Referer=..` — header syntax for Android/IPTV players
function pipeUrl(url, hdr) {
  const order = ['User-Agent', 'Referer', 'Origin', 'Cookie', 'Authorization'];
  const esc = (v) => String(v).replace(/%/g, '%25').replace(/\|/g, '%7C')
    .replace(/&/g, '%26').replace(/[\r\n]/g, '');
  const parts = [];
  for (const k of order) if (hdr[k]) parts.push(`${k}=${esc(hdr[k])}`);
  for (const [k, v] of Object.entries(hdr)) {
    if (!order.includes(k) && v) parts.push(`${k}=${esc(v)}`);
  }
  return parts.length ? `${url}|${parts.join('&')}` : url;
}

function buildM3U(chans, style = 'vlc') {
  const esc = s => (s || '').replace(/[\r\n]+/g, ' ').trim();
  const lines = ['#EXTM3U'];
  for (const ch of chans) {
    // dead = stream mati; no_playlist = tak bisa dibawa gaya mana pun;
    // drm = manifest terenkripsi -> player biasa (VLC/Televizo) menolak
    if (ch.dead || ch.no_playlist || ch.drm) continue;
    const hdr = normalizeHeaders(ch.header_iptv);
    if (style === 'vlc') {
      // Cookie dsb. hanya bisa dibawa oleh gaya pipe
      if (ch.pipe_only) continue;
      if (Object.keys(hdr).some(k => !VLC_HEADER_KEYS.has(k.toLowerCase()))) continue;
    }
    if (!ch.hls || !ch.hls.startsWith('http')) continue;
    const gid = (ch.name || 'tv').toLowerCase().replace(/\s+/g, '').slice(0, 24);
    lines.push(`#EXTINF:-1 tvg-id="${gid}" tvg-name="${esc(ch.name)}" group-title="${esc(groupOf(ch))}",${esc(ch.name)}`);
    const ua = hdr['User-Agent'];
    if (ua) {
      lines.push(`#EXTVLCOPT:http-user-agent=${ua}`);
      lines.push(`#KODIPROP:http-user-agent=${ua}`);
    }
    if (hdr.Referer) lines.push(`#EXTVLCOPT:http-referrer=${hdr.Referer}`);
    if (hdr.Origin) lines.push(`#KODIPROP:http-origin=${hdr.Origin}`);
    lines.push(style === 'pipe' && Object.keys(hdr).length
      ? pipeUrl(ch.hls, hdr) : ch.hls);
  }
  return lines.join('\n') + '\n';
}

// named exports for unit tests (Node) — harmless in the Workers runtime
export { filterChannels, buildM3U, parseLimit, json as jsonResponse };
