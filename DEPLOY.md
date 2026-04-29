# Tideline Deploy Guide

End-to-end, ~30 minutes hands-on time. Free forever for the architecture as designed.

## What you're deploying

```
GitHub (public repo)
   ↓ Actions cron */5 min — runs Python workers
Cloudflare R2 ← latest.json + decision_log.csv + research_log.md (zero egress)
Cloudflare Pages ← serves web/ static files (unlimited bandwidth)
   ↓
your-domain.com (or pages.dev URL)
   ↓
pinned in your X bio
```

You'll do every step below once. After that the cron runs forever and you don't touch it.

## Step 1 — API keys (5 min)

Two free keys:

1. **FRED** (St. Louis Fed): https://fred.stlouisfed.org/docs/api/api_key.html
   - Sign up, click "Request API key", copy the 32-char string
2. **CoinGecko demo** (optional, recommended): https://www.coingecko.com/en/developers/dashboard
   - Sign up, demo plan, copy the key

Save both somewhere safe — you'll paste them into GitHub secrets in step 4.

## Step 2 — Cloudflare R2 bucket (5 min)

1. Sign up at https://dash.cloudflare.com — free account is fine.
2. R2 → "Create bucket" → name it `tideline` (or whatever you want, just remember it).
3. **Bucket → Settings → Public Access → "Allow Access"**. Note the public URL — looks like `https://pub-<long-hash>.r2.dev`.
4. R2 → **Manage R2 API Tokens** → Create API Token:
   - Permissions: **Object Read & Write**
   - Specify bucket: your `tideline` bucket only
   - TTL: forever
   - Copy: `Account ID`, `Access Key ID`, `Secret Access Key` — you'll need all three.

## Step 3 — GitHub repo (5 min)

1. Create a new public repository on GitHub (e.g. `tideline`).
2. Push the local code:
   ```bash
   cd /path/to/tideline
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR-USERNAME/tideline.git
   git push -u origin main
   ```
3. Confirm `.gitignore` excludes `workers/.env` and `workers/data_archive/hy_oas_*.csv` — verify nothing sensitive shows up in the GitHub UI.

**Important:** must be **public** repo. Public repos get unlimited GitHub Actions minutes; private repos cap at 2,000/month and our 5-min cron would burn through that.

## Step 4 — GitHub Actions secrets (3 min)

In your repo: **Settings → Secrets and variables → Actions → New repository secret**. Add all 6:

| Name | Value |
|---|---|
| `FRED_API_KEY` | from step 1 |
| `COINGECKO_DEMO_KEY` | from step 1 (optional) |
| `R2_ACCOUNT_ID` | from step 2 |
| `R2_ACCESS_KEY_ID` | from step 2 |
| `R2_SECRET_ACCESS_KEY` | from step 2 |
| `R2_BUCKET` | the bucket name (e.g. `tideline`) |

## Step 5 — Trigger first run (2 min)

In your repo: **Actions → "Refresh Tideline data" → Run workflow → Run workflow**.

Wait ~1 minute. If it goes green, click in and confirm the log shows `uploaded latest.json` and `uploaded decision_log.csv`. The cron will now also run automatically every 5 minutes from here on.

Verify in browser: visit `https://pub-<your-hash>.r2.dev/latest.json` — should return JSON.

## Step 6 — Cloudflare Pages (5 min)

1. Cloudflare dashboard → **Workers & Pages → Create → Pages → Connect to Git**.
2. Authorize the GitHub repo, select it.
3. Build settings:
   - Framework preset: **None**
   - Build command: leave **blank**
   - Build output directory: `web`
4. Click **Save and Deploy**.

When it finishes (≈30s), you'll get a URL like `https://tideline-xyz.pages.dev`. This is the live site.

## Step 7 — Point frontend at R2 (3 min)

The frontend currently fetches `./latest.json` from same origin. We want it to fetch from R2.

Edit `web/index.html`. Find this line near the bottom:

```js
const DATA_URL = window.TIDELINE_DATA_URL || './latest.json';
const LOG_URL  = window.TIDELINE_LOG_URL  || './decision_log.csv';
```

Change to your R2 public URL:

```js
const DATA_URL = 'https://pub-YOUR-HASH.r2.dev/latest.json';
const LOG_URL  = 'https://pub-YOUR-HASH.r2.dev/decision_log.csv';
```

In `web/methodology.html`, find:

```js
const r = await fetch('./research_log.md', { cache: 'no-cache' });
```

Change to:

```js
const r = await fetch('https://pub-YOUR-HASH.r2.dev/research_log.md', { cache: 'no-cache' });
```

Also replace the OG meta tag URLs (in both `index.html` and `methodology.html`):

```html
<!-- BEFORE -->
<meta property="og:url" content="https://tideline.example">
<meta property="og:image" content="https://tideline.example/og.png">

<!-- AFTER -->
<meta property="og:url" content="https://YOUR-DOMAIN-OR-PAGES-URL">
<meta property="og:image" content="https://YOUR-DOMAIN-OR-PAGES-URL/og.png">
```

(Twitter meta tags too — same pattern.)

Commit and push. Pages auto-redeploys.

## Step 8 — CORS (only if frontend can't reach R2)

If the dashboard loads but shows "Data unavailable" with a CORS error in the browser console:

1. Cloudflare R2 → your bucket → Settings → CORS Policy
2. Add:
   ```json
   [{"AllowedOrigins": ["https://YOUR-PAGES-URL", "https://YOUR-DOMAIN"], "AllowedMethods": ["GET"], "AllowedHeaders": ["*"]}]
   ```

## Step 9 — Custom domain (optional, 5 min)

Skip this if you're happy with `tideline-xyz.pages.dev`.

1. Buy a short domain (Cloudflare Registrar: $9–15/yr for `.live`/`.dev`).
2. Cloudflare Pages → your project → **Custom domains → Set up a domain**. Type `tideline.live` (or whatever). DNS records auto-add if domain is on Cloudflare.
3. Wait for SSL cert (≈5 min).

Update OG meta tags + `DATA_URL` to the new domain.

## Step 10 — X share preview check + pin (3 min)

1. Use https://cards-dev.twitter.com/validator to enter your URL and confirm the OG image renders.
2. If it does, post the link in a tweet. Tideline X card preview should show the dark ocean image with the wordmark.
3. Pin the tweet to your profile.

## Maintenance

**Day-to-day:** zero. The cron runs forever. The dashboard updates every 5 min.

**If something breaks:**
- GitHub Actions tab shows red X — click in to see the error.
- Most common: FRED rotated their API key format. Re-grab one and update the secret.
- Less common: Yahoo killed their endpoint. The `fetchers/yahoo.py` would fail. Less critical because Yahoo data isn't in the regime compute (only the dashboard's component rows).

**If you ever want to update the rule:**
- DON'T edit `rule/v1.py` in place. Per the integrity rules: changes reset the accuracy counter.
- Create `rule/v2.py`, copy with changes, update `compute/regime.py` import, document in `research_log.md`. The `rule_sha256` hash in every payload tracks which version produced which call — never silently change.

## Checklist before pinning on X

- [ ] All 6 GitHub secrets configured
- [ ] First Actions run completed green
- [ ] R2 public URL returns valid `latest.json`
- [ ] Cloudflare Pages deploy live
- [ ] `DATA_URL` and `LOG_URL` updated to R2 in `web/*.html`
- [ ] OG meta tags reference your real URL (not `tideline.example`)
- [ ] X card validator shows the OG image
- [ ] Mobile check on actual phone — Safari + Chrome
- [ ] Methodology page loads and research log renders
- [ ] Decision log CSV downloads
