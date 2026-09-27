# ClAudit census (Cloudflare Worker)

The anonymous half of the install census. `worker.js` receives one small JSON heartbeat from each
running ClAudit node every 10 minutes and answers `GET /stats` with counts. It keeps, per node, a
random id the client made up, the version string, the OS family, the install mode, the time of the
last beat, and whether the last beat was a clean stop. Nothing else: no IP, no hostname, no GitHub
account, no content. Entries expire after 8 days on their own.

What the counts mean (a beat every 10 minutes, `ACTIVE_MIN = 30`):

| field | meaning |
|---|---|
| `active` | beat within the last 30 minutes and no stop event: running right now |
| `stopped` | sent a stop event within 24 h: quit cleanly |
| `quiet` | seen within 24 h, silent for more than 30 minutes, no stop: crashed, suspended, offline |
| `seen_24h`, `seen_7d` | distinct nodes in those windows |
| `versions`, `os`, `mode` | breakdown of the active nodes |
| `versions_24h` | breakdown of everything seen in 24 h |

Deploy (needs `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in the environment):

```bash
cd telemetry
npx wrangler kv namespace create CENSUS      # once; put the id in wrangler.toml
npx wrangler deploy
```

The client side is `census_*` in `claudit_scan.py`; the URL it posts to is `CENSUS_URL`, overridable
with `census_url` in `~/.claude/claudit/config.json`. Turn the heartbeat off with `census_anon: false`.
