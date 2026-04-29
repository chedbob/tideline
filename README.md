# Tideline

Cross-asset regime tracker. Public, free-tier-only stack, viral-safe.

Full plan: [`../.claude/plans/yo-i-got-you-ticklish-bengio.md`](../.claude/plans/yo-i-got-you-ticklish-bengio.md)

## Architecture

```
GitHub Actions (cron */5 min, public repo = unlimited minutes)
   └── Python workers (fetch + compute)
         └── Cloudflare R2 (latest.json — zero egress, 10M reads/mo free)
               └── Cloudflare Pages (static Next.js — unlimited bandwidth free)
```

## Phase 1 status (this repo)

Plumbing only. No frontend yet.

- `workers/fetchers/fred.py` — Fed liquidity, credit spreads, rates (FRED API)
- `workers/fetchers/yahoo.py` — VIX term, sector ETFs, FX carry
- `workers/fetchers/coingecko.py` — BTC.D, ETH.D, stablecoin supply
- `workers/fetchers/perps.py` — OI-weighted aggregate funding (Binance/Bybit/OKX/Hyperliquid)
- `workers/publish.py` — assembles `latest.json`, uploads to R2
- `.github/workflows/refresh.yml` — 5-min cron

## Local dev

```bash
cd workers
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export FRED_API_KEY=your_key_here          # https://fred.stlouisfed.org/docs/api/api_key.html
export COINGECKO_DEMO_KEY=your_key         # optional, https://www.coingecko.com/en/developers/dashboard

python publish.py
# → writes workers/out/latest.json
```

## Deploy (user-action items)

1. **Create FRED API key** (free): https://fred.stlouisfed.org/docs/api/api_key.html
2. **Create CoinGecko demo key** (free, optional but recommended): https://www.coingecko.com/en/developers/dashboard
3. **Create Cloudflare R2 bucket**:
   - Sign in to Cloudflare → R2 → Create bucket named `tideline`
   - Enable public access (or use custom domain later)
   - Generate an API token with Object Read & Write for this bucket
   - Note the `accountId`, `accessKeyId`, `secretAccessKey`
4. **Push this repo to GitHub as public** (required for unlimited Actions minutes).
5. **Add GitHub secrets** (Settings → Secrets → Actions):
   - `FRED_API_KEY`
   - `COINGECKO_DEMO_KEY`
   - `R2_ACCOUNT_ID`
   - `R2_ACCESS_KEY_ID`
   - `R2_SECRET_ACCESS_KEY`
   - `R2_BUCKET` (e.g. `tideline`)
6. **Trigger the workflow manually** (Actions tab → Refresh Tideline data → Run workflow) to verify. Once green, it runs every 5 min.
7. `latest.json` will be at: `https://pub-<hash>.r2.dev/latest.json` (or your custom domain).

## Roadmap

See the plan file for Weeks 2–6: Next.js port of the HTML prototype, HMM regime classifier (filtered probs only — never Viterbi), historical analog finder, daily share-card PNG generator, launch.

## Integrity

- No regime label displayed from Viterbi-smoothed HMM states (they repaint using future data)
- All computed metrics show uncertainty / base rates, never point predictions
- Methodology page will disclose what free data cannot provide (intraday GEX, live swap spreads, etc.)
