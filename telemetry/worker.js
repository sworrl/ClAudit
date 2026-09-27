// ClAudit census: an anonymous count of running nodes, by version, OS family, and install mode.
//
// A node POSTs {node, v, os, mode, event} to /beat every 10 minutes while it runs and once with
// event "stop" on a clean quit. `node` is a random id the client generated on first run; it is
// stored in KV with a short TTL and nothing else is kept: no IP, no hostname, no account, no
// content. Cloudflare sees the request IP the way it sees any request, but this script never
// reads it, never logs it, and KV holds only the fields below.
//
// A cron aggregates every 5 minutes into one "stats" record that GET /stats returns:
//   active   beat within ACTIVE_MIN and no stop event   (running right now)
//   stopped  said goodbye within 24h                    (clean quit)
//   quiet    seen within 24h but silent > ACTIVE_MIN    (crashed, suspended, offline)
//   seen_24h / seen_7d, versions{}, os{}, mode{} for active nodes, versions_24h{} for all seen.
const ACTIVE_MIN = 30;      // beats every 10 min; three missed beats = down
const RETAIN_DAYS = 8;      // a node that never beats again vanishes from KV after this

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,POST,OPTIONS",
  "access-control-allow-headers": "content-type",
};

function bump(o, k) { k = k || "unknown"; o[k] = (o[k] || 0) + 1; }

async function aggregate(env) {
  const now = Date.now();
  const nodes = [];
  let cursor;
  do {
    const r = await env.CENSUS.list({ prefix: "node:", cursor, limit: 1000 });
    for (const k of r.keys) if (k.metadata) nodes.push(k.metadata);
    cursor = r.list_complete ? undefined : r.cursor;
  } while (cursor);
  const activeMs = ACTIVE_MIN * 60000, dayMs = 86400000;
  const out = { generated: new Date(now).toISOString(), active_window_min: ACTIVE_MIN,
                retain_days: RETAIN_DAYS, active: 0, stopped: 0, quiet: 0, seen_24h: 0,
                seen_7d: nodes.length, versions: {}, os: {}, mode: {}, versions_24h: {} };
  for (const n of nodes) {
    const age = now - (n.last || 0);
    if (!n.stop && age <= activeMs) {
      out.active++; bump(out.versions, n.v); bump(out.os, n.os); bump(out.mode, n.mode);
    } else if (n.stop && age <= dayMs) {
      out.stopped++;
    } else if (age <= dayMs) {
      out.quiet++;
    }
    if (age <= dayMs) { out.seen_24h++; bump(out.versions_24h, n.v); }
  }
  await env.CENSUS.put("stats", JSON.stringify(out));
  return out;
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (url.pathname === "/beat" && req.method === "POST") {
      let b;
      try { b = await req.json(); } catch { return new Response("bad json", { status: 400, headers: CORS }); }
      const id = String(b.node || "").toLowerCase();
      if (!/^[0-9a-f]{32}$/.test(id)) return new Response("bad node", { status: 400, headers: CORS });
      const v = String(b.v || "").slice(0, 20).replace(/[^0-9a-zA-Z.\-]/g, "");
      const os = ["linux", "darwin", "windows"].includes(b.os) ? b.os : "other";
      const mode = ["git", "pip"].includes(b.mode) ? b.mode : "other";
      const stop = b.event === "stop";
      await env.CENSUS.put("node:" + id, "1", {
        expirationTtl: RETAIN_DAYS * 86400,
        metadata: { v, os, mode, last: Date.now(), stop },
      });
      return new Response(null, { status: 204, headers: CORS });
    }
    if (url.pathname === "/stats" || url.pathname === "/") {
      let s = url.searchParams.get("fresh") ? await aggregate(env) : await env.CENSUS.get("stats", "json");
      return new Response(JSON.stringify(s || { note: "no data yet" }, null, 1), {
        headers: { ...CORS, "content-type": "application/json", "cache-control": "public, max-age=60" },
      });
    }
    return new Response("ClAudit census. POST /beat, GET /stats.", { status: 404, headers: CORS });
  },
  async scheduled(_event, env, ctx) { ctx.waitUntil(aggregate(env)); },
};
