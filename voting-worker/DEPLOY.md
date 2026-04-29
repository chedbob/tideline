# Deploy the voting Worker — Cloudflare Workers + D1

Adds the poll, vote, scoreboard endpoints to your existing Tideline deploy.

## Prerequisites

- Cloudflare account (you have it)
- Node.js + npm (Wrangler is a JS CLI). Quick test: `npm --version` should return something. If not: https://nodejs.org → installer.

## Steps

### 1. Install Wrangler (Cloudflare's CLI)

```
npm install -g wrangler
```

### 2. Log in (browser opens)

```
wrangler login
```

Click Allow.

### 3. Create the D1 database

```
cd C:\Users\natet\OneDrive\Desktop\Claude\tideline\voting-worker
wrangler d1 create tideline-voting
```

Output looks like:

```
[[d1_databases]]
binding = "DB"
database_name = "tideline-voting"
database_id = "abcd1234-ef56-7890-abcd-ef1234567890"
```

**Copy that `database_id`** and paste it into `wrangler.toml` (replace `REPLACE_WITH_D1_ID_AFTER_CREATE`).

### 4. Apply the schema

```
wrangler d1 execute tideline-voting --remote --file=schema.sql
```

You'll see "Executed N queries". Schema applied.

### 5. Deploy the Worker

```
wrangler deploy
```

Output ends with the live URL — looks like:

```
https://tideline-voting.YOURUSER.workers.dev
```

**Copy that URL.**

### 6. Test endpoints

```
curl https://tideline-voting.YOURUSER.workers.dev/health
```

Should print `ok`.

```
curl -X POST https://tideline-voting.YOURUSER.workers.dev/_admin/create
```

Manually creates this week's poll. Should return JSON with the poll details.

### 7. Wire frontend to the Worker

Edit `web/index.html`. Find this line:

```js
const VOTE_API = window.TIDELINE_VOTE_API || '';
```

Change to your Worker URL:

```js
const VOTE_API = 'https://tideline-voting.YOURUSER.workers.dev';
```

Save → commit → push:

```
cd C:\Users\natet\OneDrive\Desktop\Claude\tideline
git add web/index.html voting-worker/
git commit -m "wire voting worker"
git push
```

Cloudflare Pages auto-redeploys in ~30s.

### 8. Verify

Open your Pages URL. You should see:

- **Tide Score** hero with the big number
- **This Week's Poll** card with Tideline's call + 3 vote buttons + live tally
- **Scoreboard** with two circular rings (Tideline + Crowd)

Vote once to test. The button should highlight and the tally should update.

## Cron schedule (already configured in wrangler.toml)

- **Mon 13:35 UTC** (9:35 ET) — Worker creates a fresh poll for the week
- **Fri 21:00 UTC** (5:00 ET, after market close) — Worker resolves the previous week's poll and updates the scoreboard

You don't trigger these manually after launch. The first poll was created via `/_admin/create` in step 6, so the dashboard works immediately.

## Manual admin endpoints (for testing)

- `POST /_admin/create` — force-create this week's poll if missing
- `POST /_admin/resolve` — force-resolve any open polls past their week_end

These are not protected. If abuse becomes a concern later, add a shared-secret header check.

## Cost

Free tier:
- D1: 5 GB storage, 5M reads/day, 100k writes/day
- Workers: 100k requests/day free
- Cron: free

You'll hit none of those limits for this product.
