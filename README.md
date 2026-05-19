# Meta Ads AI Analytics

An AI-powered Meta Ads analytics dashboard built against the requirements in `ads analysis dashboard req.md`. Uses **Claude Sonnet 4.6** for analysis, recommendations, and chat. Persistent SQLite memory so the AI never analyzes a report in isolation.

---

## Run it (pick one)

### Option A — Docker (recommended)

```bash
cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY=sk-ant-...
docker compose up -d
```

Open http://localhost:8000 — SQLite memory persists in a named Docker volume (`ads-data`).

### Option B — One-command script

```bash
# macOS / Linux
./run.sh

# Windows
run.bat
```

Creates a venv, installs deps, copies `.env.example` → `.env` on first run, then starts uvicorn.

### Option C — Manual

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add ANTHROPIC_API_KEY
uvicorn backend.main:app --reload --port 8000
```

---

## What's implemented (against the spec)

### Core platform (§1–3)
- Multi-file **CSV / XLSX / XLS** upload (Meta exports `.xls` by default)
- Auto-detects whether each file is the **Campaigns / Ad Sets / Ads** export from filename + columns
- When all three levels are uploaded for the same period, each rollup uses its own level's file — no double-counting
- Detects per-day vs aggregate exports; warns the user in the UI if charts will collapse to a single point

### Persistent context & memory (§5 — the MOST IMPORTANT requirement)
- Every Claude call routes through `ai.context_block()`, which prepends a `<historical_context>` payload with:
  - Upload history (filename, level, period, daily/aggregate)
  - All prior analyses + summaries
  - Standing recommendations + their auto-judged outcomes
  - Long-term memory entries (insights, trends, risks, scaling events)
  - User notes / overrides
- The system prompt makes reading this block **inviolable**
- Every `analyze()` writes new recommendations + memory back to the DB
- **Outcome auto-scoring**: before each new analysis runs, `score_previous_recommendations()` compares the entity's post-recommendation window against the pre-recommendation window and writes `improved / worsened / unchanged` to the DB. The AI then sees its own track record on the next call.
- **Context export / import**: download `/api/context/export` for a full JSON snapshot of memory; POST to `/api/context/import` to restore. Lets you migrate machines or branch your AI memory.

### Analytics dashboard (§6)
- Campaign / Ad Set / Ad level breakdowns
- Metrics: daily CAC + daily CAC change, daily cost/conversation, daily conversations, daily spend, results, plus ROAS, CTR, CPC, CPM, conversion rate
- Filters: date range, campaign, ad set, ad, region, granularity (daily/weekly/monthly)
- USD → INR auto-conversion (override rate via `USD_TO_INR` env)

### AI chat & analysis (§4, §7)
- Right-hand chat panel grounded in full history + current data snapshot
- "Run AI Analysis" → executive summary, narrative commentary, structured recommendations across **Working / Not Working / At Risk / Needs Scaling**
- **Creative fatigue detector** (§7): scans for ads where frequency rose while CTR fell or cost-per-conversation rose; surfaces as auto-flagged at_risk recommendations
- **Period-vs-period comparison** (§4, §8): "what changed since last week" with headline KPI deltas + per-campaign deltas

### Commentary & reporting (§8)
- Executive summaries, narrative commentary, risk and scaling analysis on every analysis run
- `/api/summary?period=daily|weekly|monthly` — designed to be hit by a cron / scheduled task to produce a fresh briefing automatically

### Export (§9)
- CSV summary (filtered)
- PDF report (per analysis)
- JSON context backup (full AI memory)

### UI (§10)
- Functional Meta-Ads-Manager-style layout: sidebar (uploads, past analyses, memory), main (KPIs, charts, tabs for Campaigns / Ad Sets / Ads / Recommendations / Compare / Fatigue / History), right-side AI chat
- Data-first, not aesthetic-first

---

## API reference

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/upload` | Multi-file upload (CSV/XLSX/XLS) |
| GET | `/api/uploads` | Upload history |
| GET | `/api/entities` | Distinct campaigns / adsets / ads / regions |
| GET | `/api/metrics` | KPIs + time series + breakdowns (with filters) |
| GET | `/api/compare` | Period vs previous period (auto-derived or explicit) |
| GET | `/api/fatigue` | Creative-fatigue scan over the date range |
| POST | `/api/analyze` | Full AI analysis (scores past recs, runs comparison + fatigue, writes new recs) |
| POST | `/api/chat` | Chat with the AI analyst |
| GET | `/api/chat/history` | Chat history |
| POST | `/api/summary` | Generate executive summary for `daily / weekly / monthly` |
| GET | `/api/recommendations` | All recommendations (across analyses) |
| GET | `/api/analyses` / `/api/analyses/{id}` | Historical analysis list / detail |
| GET | `/api/memory` | Long-term memory entries |
| POST | `/api/notes` / GET `/api/notes` | User notes / overrides |
| POST | `/api/score-outcomes` | Re-judge all pending recommendation outcomes |
| GET | `/api/context/export` | Download full AI memory as JSON |
| POST | `/api/context/import` | Restore from a context JSON |
| GET | `/api/export/csv` / `/api/export/pdf` | Reports |
| GET | `/api/health` | Health + AI status |

---

## Exporting from Meta Ads Manager (so the dashboard sees daily data)

Meta exports one `.xls` per level (Campaigns / Ad Sets / Ads). Upload all three.

For best results:

1. In Ads Manager switch to the **Campaigns / Ad Sets / Ads** tab as needed.
2. Click **Reports → Export → Export table data (.xlsx)**.
3. Set **Breakdowns → Time → Day** before exporting if you want daily CAC trends. Without this you'll get one aggregate row per entity.
4. Recommended columns: `Campaign Name` / `Ad Set Name` / `Ad Name`, `Amount Spent`, `Impressions`, `Reach`, `Frequency`, `Link Clicks`, `CTR`, `Messaging Conversations Started`, `Cost per Messaging Conversation Started`, `Purchases`, `Purchase Conversion Value`.

If a file doesn't import cleanly, run:

```bash
python inspect_export.py path/to/your-file.xls
```

It prints the raw columns and exactly what the parser extracted.

---

## Scheduling automatic summaries

The `/api/summary` endpoint takes a `period` and returns a fresh analysis. Wire it to whatever scheduler you prefer:

```bash
# every Monday at 9am — weekly digest
0 9 * * 1 curl -s -X POST http://localhost:8000/api/summary \
  -H "Content-Type: application/json" -d '{"period":"weekly"}'
```

Or use Cowork's built-in scheduled tasks to run it inside Claude and forward the summary to Slack / email / WhatsApp.

---

## Project layout

```
meta-ads-ai/
├── backend/
│   ├── main.py          # FastAPI app + endpoints
│   ├── database.py      # SQLite schema + persistent memory + migrations
│   ├── parser.py        # CSV/XLSX/XLS → normalized rows, level detection
│   ├── analytics.py     # Metrics, time series, breakdowns, currency conversion
│   ├── comparison.py    # Period-vs-period, fatigue detection, outcome scoring
│   └── ai.py            # Claude Sonnet 4.6 integration + historical context loader
├── frontend/
│   └── index.html       # Single-page dashboard (vanilla JS + Chart.js)
├── sample_data/         # Two weeks of demo Meta Ads data
├── inspect_export.py    # CLI: probe how the parser will read a given file
├── Dockerfile
├── docker-compose.yml
├── run.sh / run.bat     # One-command bootstrap
├── requirements.txt
├── .env.example
└── README.md
```

---

## What's deferred (and roughly how to add it)

- **Direct Meta Marketing API integration** — currently file-upload only. Would plug into `parser.py` as a new ingest source; the rest of the pipeline doesn't care where rows come from.
- **Predictive forecasting** — add a `/api/forecast` endpoint using Prophet or a Claude-driven projection.
- **Slack / WhatsApp alerts** — point your scheduler at `/api/summary` and pipe the response into a webhook.
- **Multi-currency beyond USD→INR** — extend `analytics.USD_TO_INR` to a fuller FX table or call an FX API.
- **Auth** — single-user / admin-only per the spec. Add SSO when multi-user is needed.

---

## Cost / latency notes

Sonnet 4.6 is the right default for the analysis + chat workload. Optimizations once history grows large:

- Route quick chat replies to `claude-haiku-4-5-20251001`; keep `analyze()` on Sonnet.
- Cap `context_block()` parameters once memory grows; or summarize older memory entries with Haiku before re-loading.
