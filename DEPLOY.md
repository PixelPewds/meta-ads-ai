# Deploying to `dev.forcescholar.com`

Hostinger shared hosting can't run Python apps. The clean path is:
**Railway runs the app → Hostinger DNS points your subdomain at Railway.**

Total time: ~15 minutes. Cost: ₹0 to start (Railway free tier covers light usage).

---

## Step 1 — Get an Anthropic API key (the "AI" part)

1. Go to https://console.anthropic.com/
2. Sign up with email or Google
3. Click **Settings → Billing** and add a payment method. Add at least $5 in credit (you can set a monthly cap so you never overspend — $5 lasts a long time for a single-user analytics app).
4. Click **API Keys → Create Key**. Name it `forcescholar-dashboard`. Copy the `sk-ant-...` string immediately (you can't view it again).
5. Save it somewhere safe — you'll paste it into Railway in Step 4.

That's it for Anthropic.

---

## Step 2 — Push the project to GitHub

Railway deploys from a Git repo. If you don't have one yet:

```bash
cd meta-ads-ai
git init
git add .
git commit -m "initial: meta ads ai dashboard"

# Create a new repo at https://github.com/new (private is fine), then:
git remote add origin git@github.com:YOUR_USER/meta-ads-ai.git
git branch -M main
git push -u origin main
```

The included `.gitignore` keeps your `.env` and `data/` out of the repo.

---

## Step 3 — Deploy on Railway

1. Go to https://railway.app and sign up with GitHub.
2. Click **New Project → Deploy from GitHub repo → meta-ads-ai**.
3. Railway sees the `Dockerfile` and starts building. Wait ~3 minutes.
4. Click the deployed service → **Variables** → add:
   - `ANTHROPIC_API_KEY` = the `sk-ant-...` from Step 1
   - `CLAUDE_MODEL` = `claude-sonnet-4-6`  (optional — defaults to this)
   - `USD_TO_INR` = `83.0`  (optional)
5. Click **Settings → Networking → Generate Domain**. Railway gives you a URL like `meta-ads-ai-production.up.railway.app`. Open it — your dashboard should be live.
6. Add a **volume** so the SQLite memory survives redeploys: **Settings → Volumes → New Volume**, mount path `/app/data`, size 1 GB.

Quick sanity check: open the Railway URL, the status pill in the sidebar should say "Claude online · claude-sonnet-4-6" (green dot). If it shows an error, hover the pill to see what Anthropic returned.

---

## Step 4 — Point `dev.forcescholar.com` at Railway

In Railway:

1. **Settings → Networking → Custom Domain → Add Domain**
2. Type `dev.forcescholar.com`
3. Railway shows you a CNAME target like `something.up.railway.app`. Copy it.

In Hostinger hPanel:

1. **Domains → forcescholar.com → DNS / Name Servers**
2. **Add new record**:
   - Type: `CNAME`
   - Name: `dev`
   - Target: paste the Railway CNAME target
   - TTL: 14400 (default)
3. Save.

DNS propagation usually takes 5–30 minutes (sometimes longer). When it's ready, Railway auto-issues a Let's Encrypt SSL cert, and `https://dev.forcescholar.com` will serve the dashboard.

---

## Updating the app after deploy

Any push to `main` on GitHub auto-redeploys. To make a change:

```bash
# edit files locally
git add .
git commit -m "tweak"
git push
```

Railway rebuilds in ~2 minutes. SQLite data persists across deploys because of the volume from Step 3.6.

---

## Backing up the AI memory

Hit `https://dev.forcescholar.com/api/context/export` (or click ⬇ Context in the dashboard) → downloads a JSON of all uploads, analyses, recommendations, and long-term memory. Save it to Google Drive once a week.

---

## Cost expectations

- **Railway**: free tier covers ~500 hours/mo of execution. The app sleeps when idle; first request after sleep wakes it (~5s). For ~₹500/mo you can keep it always-on.
- **Anthropic**: each "Run AI Analysis" call costs roughly ₹2–₹8 depending on how much history Claude reads. A chat reply: ₹0.50–₹2. $5 in credit = ~₹400 worth of usage, lasts weeks for normal use.
- **Hostinger**: no extra cost — just using your existing DNS.

---

## Alternatives if Railway doesn't suit you

Same deploy pattern works on:
- **Render** (`render.com`) — similar UX, persistent disks for SQLite
- **Fly.io** — slightly more technical, great free tier
- **Hostinger VPS** (KVM 1, ~₹450/mo) — `ssh` in, `docker compose up -d`, point dev.forcescholar.com A-record at the VPS IP, add Nginx + certbot. Most control, most setup.

Tell me which and I'll write specific steps.
