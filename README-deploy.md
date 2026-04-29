# Tideline — fast deploy path (TL;DR)

Two scripts, one browser flow, ~20 minutes. Full step-by-step in [DEPLOY.md](./DEPLOY.md).

## What you do

```
1. CLOUDFLARE BROWSER (10 min)
   - Sign up at dash.cloudflare.com
   - R2 → Create bucket: tideline
   - Enable public access; copy the pub-XXX.r2.dev URL
   - Manage R2 API Tokens → Create → copy: account_id, access_key_id, secret
   - (don't do Pages yet — comes after step 3)

2. INSTALL gh CLI (one time, 2 min)
   - https://cli.github.com  →  download installer
   - Open Command Prompt:  gh auth login  →  follow prompts (GitHub.com, HTTPS, browser)

3. RUN TWO SCRIPTS (5 min)
   cd C:\Users\natet\OneDrive\Desktop\Claude\tideline
   python configure.py        # interactive — paste R2 values
   bash deploy.sh             # auto-creates repo, sets secrets, triggers run

4. CLOUDFLARE PAGES (3 min, browser)
   - Workers & Pages → Create → Pages → Connect to Git → tideline repo
   - Build command: blank
   - Build output: web
   - Save and Deploy
   - You get a https://tideline-XXX.pages.dev URL

5. RE-RUN configure.py (1 min)
   python configure.py
   - Enter the Pages URL when prompted for SITE_URL
   - This updates OG meta tags so X previews work
   - Run: bash deploy.sh   (re-pushes the OG fix)

6. VALIDATE + PIN (2 min)
   - https://cards-dev.twitter.com/validator → paste your Pages URL → confirm OG card shows
   - Tweet your link, pin it
```

## What configure.py needs from you

When you run it, it asks for these values one at a time. Have them in a notepad ready:

| Prompt | Where you got it |
|---|---|
| R2_ACCOUNT_ID | Cloudflare R2 → Manage API Tokens page |
| R2_ACCESS_KEY_ID | created with the token |
| R2_SECRET_ACCESS_KEY | shown ONCE when token is created — save it |
| R2_BUCKET | the bucket name you typed (`tideline`) |
| R2_PUBLIC_URL | `https://pub-XXX.r2.dev` (no trailing slash) |
| SITE_URL | `https://tideline-XXX.pages.dev` (after step 4) — placeholder OK first pass |

FRED_API_KEY and COINGECKO_DEMO_KEY are already in your `.env` from the previous step; configure.py won't re-prompt unless they're missing.

## What deploy.sh does for you

- Creates the GitHub repo (public) if it doesn't exist
- Pushes everything
- Sets all 6 GitHub Actions secrets from your local `.env` (none of these are committed; `.env` is gitignored)
- Triggers the first cron run and watches it
- Curls your R2 URL to confirm `latest.json` actually appeared

## If something breaks

- **`gh: command not found`** → install GitHub CLI, then `gh auth login`
- **`gh auth status` fails** → run `gh auth login` again
- **`workers/.env not found`** → run `python configure.py` first
- **Workflow run fails** → click into Actions tab on GitHub, read the log, usually a typo'd secret
- **R2 URL returns 403 or 404** → bucket public access not enabled in Cloudflare dashboard (R2 → bucket → Settings → Public Access → Allow)
- **Page loads but data says "unavailable"** → CORS issue. The deploy.sh tail prints the exact CORS policy to paste

## What I (Claude) handled vs what you handled

| Task | Me | You |
|---|---|---|
| Write `configure.py` + `deploy.sh` | ✓ | |
| Create Cloudflare account + R2 bucket | | ✓ (browser) |
| Run `python configure.py` (you paste the values) | | ✓ |
| Run `bash deploy.sh` | | ✓ |
| Connect Pages to Git | | ✓ (browser) |
| Tweet + pin | | ✓ |

You are the only person who can click the browser flows and own the Cloudflare/X accounts. Everything in between is automated.
