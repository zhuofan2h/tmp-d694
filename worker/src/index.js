// IPTV playlist/API Worker — serves data from GitHub raw (free tier, no KV needed)
const DATA_BASE = (globalThis.DATA_BASE_OVERRIDE) || 'https://raw.githubusercontent.com/zhuofan2h/quiet-summit-8ck88/main/data/';
const TTL = 900; // 15 min edge cache

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

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const q = url.searchParams;
    const path = url.pathname.replace(/\/+$/, '') || '/';

    try {
      if (path === '/health') {
        return json({ ok: true, ts: new Date().toISOString() });
      }

      if (path === '/playlist.m3u' || path === '/m3u' || path === '/playlist') {
        const raw = await fetchText('playlist.m3u', ctx);
        if (!q.toString()) return text(raw, M3U_HEADERS);
        // filtered playlist: re-derive from channels.json
        const data = await fetchJSON('channels.json', ctx);
        const chans = filterChannels(data.channels, q);
        return text(buildM3U(chans), M3U_HEADERS);
      }

      if (path === '/api/channels') {
        const data = await fetchJSON('channels.json', ctx);
        let chans = filterChannels(data.channels, q);
        if (q.get('limit')) chans = chans.slice(0, +q.get('limit'));
        return json({ updated: data.updated, count: chans.length, channels: chans });
      }

      if (path === '/api/countries') {
        const data = await fetchJSON('countries.json', ctx);
        return json(data);
      }

      if (path === '/api/stats') {
        const [stats, chans] = await Promise.all([
          fetchJSON('stats.json', ctx),
          fetchJSON('countries.json', ctx),
        ]);
        return json({ ...stats, counts: chans.counts, per_country_total: Object.keys(chans.counts || {}).length });
      }

      return json({ error: 'not found', endpoints: ['/playlist.m3u', '/m3u?country=ID&free=1', '/api/channels', '/api/countries', '/api/stats', '/health'] }, { ...JSON_HEADERS, status: 404 });
    } catch (e) {
      return json({ error: String(e) }, { ...JSON_HEADERS, status: 502 });
    }
  },
};

function json(obj, extra = {}) {
  return new Response(JSON.stringify(obj), { headers: { ...JSON_HEADERS, ...extra } });
}
function text(s, hdrs) {
  return new Response(s, { headers: hdrs });
}

async function fetchJSON(name, ctx) {
  const data = await cached(name, ctx);
  return data.json();
}
async function fetchText(name, ctx) {
  const r = await cached(name, ctx);
  return r.text();
}
async function cached(name, ctx) {
  const cache = caches.default;
  const key = new Request(DATA_BASE + name);
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
  if (premium !== '1' && free !== '0') {
    out = out.filter(c => c.premium !== 't');
  }
  return out;
}

function buildM3U(chans) {
  const esc = s => (s || '').replace(/\n/g, ' ').trim();
  const lines = ['#EXTM3U'];
  for (const ch of chans) {
    if (!ch.hls || !ch.hls.startsWith('http')) continue;
    const gid = (ch.name || 'tv').toLowerCase().replace(/\s+/g, '').slice(0, 24);
    lines.push(`#EXTINF:-1 tvg-id="${gid}" tvg-name="${esc(ch.name)}" group-title="${esc(ch.country)}",${esc(ch.name)}`);
    let hdr = {};
    try { hdr = JSON.parse(ch.header_iptv || '{}'); } catch {}
    const ua = hdr['User-Agent'];
    if (ua && ua !== 'none') {
      lines.push(`#EXTVLCOPT:http-user-agent=${ua}`);
      lines.push(`#KODIPROP:http-user-agent=${ua}`);
    }
    const ref = hdr.Referer;
    if (ref && ref !== 'none') lines.push(`#EXTVLCOPT:http-referrer=${ref}`);
    lines.push(ch.hls);
  }
  return lines.join('\n') + '\n';
}
