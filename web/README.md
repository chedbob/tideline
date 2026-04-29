# Tideline web/

Vanilla static site. No build step. Single HTML files + Tailwind CDN. Deploy as-is to Cloudflare Pages.

## Files

- `index.html` — main dashboard (Zone 0 Faber + Zone 1 regime + Zone 2 components)
- `methodology.html` — research integrity, caveats, decision-log download
- `_redirects` — Cloudflare Pages routing

## Pre-deploy checklist

- [ ] Replace `tideline.example` in OG/Twitter meta tags with your real domain (both HTML files)
- [ ] Generate and upload an `og.png` (1200×630, dark ocean palette, big "Tideline" logo + tagline) — without one, X share previews render text-only
- [ ] Set `DATA_URL` and `LOG_URL` to your R2 public URLs (or leave defaults if same-origin)
- [ ] Confirm `latest.json` and `decision_log.csv` are reachable at those URLs from a fresh browser (no CORS errors)

## Configure data source

The frontend fetches `latest.json` and `decision_log.csv` from the same origin by default. To point at a Cloudflare R2 public URL instead, set `window.TIDELINE_DATA_URL` and `window.TIDELINE_LOG_URL` before the page's main script runs.

Easiest: edit the constants at the top of `index.html`:

```js
const DATA_URL = window.TIDELINE_DATA_URL || './latest.json';
const LOG_URL  = window.TIDELINE_LOG_URL  || './decision_log.csv';
```

Replace `./latest.json` with `https://pub-<your-r2-hash>.r2.dev/latest.json` once you've created the R2 bucket.

## Local dev

```bash
# from repo root
cp tideline/workers/out/latest.json     tideline/web/latest.json
cp tideline/workers/out/decision_log.csv tideline/web/decision_log.csv
cd tideline/web
python -m http.server 8000
# open http://localhost:8000
```

## Deploy to Cloudflare Pages

1. Push the repo to GitHub (public).
2. Cloudflare → Pages → Create project → Connect Git → select repo.
3. Build settings:
   - Framework preset: **None**
   - Build command: *(leave blank)*
   - Build output directory: `tideline/web`
4. Deploy. Custom domain optional.

Frontend will then poll your R2 `latest.json` URL every 5 minutes.

## Auto-refresh cadence

Page refreshes data every 5 minutes via `setInterval`. R2 cache is set to `s-maxage=300` so edge serves the same JSON for at most 5 minutes — matches publish cadence.

## What does NOT need a backend

- Voting (cut from v1)
- Comments (none)
- User accounts (none)
- Search (none)

If you want voting later, that's the only feature that requires Cloudflare Workers + D1. Until then this is pure static.
