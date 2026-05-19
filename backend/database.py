"""
SQLite persistent storage for Meta Ads AI Analytics.

This is the heart of the "AI never forgets" requirement.
Every upload, every analysis, every recommendation, every user note
is persisted here so the AI can load full historical context on every call.
"""

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

DB_PATH = Path(__file__).parent.parent / "data" / "ads.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


SCHEMA = """
-- File uploads: every CSV/XLSX/XLS that comes in
CREATE TABLE IF NOT EXISTS uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    uploaded_at     TEXT NOT NULL,
    row_count       INTEGER,
    date_min        TEXT,
    date_max        TEXT,
    raw_columns     TEXT,           -- JSON list of original column names
    report_level    TEXT,           -- campaign | adset | ad | mixed
    is_daily        INTEGER DEFAULT 0,
    period_start    TEXT,
    period_end      TEXT
);

-- Normalized fact table: one row per (upload, campaign, adset, ad, date)
CREATE TABLE IF NOT EXISTS ad_rows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL,
    report_level    TEXT,           -- campaign | adset | ad | mixed
    date            TEXT,
    campaign        TEXT,
    adset           TEXT,
    ad              TEXT,
    spend           REAL DEFAULT 0,
    impressions     REAL DEFAULT 0,
    reach           REAL DEFAULT 0,
    frequency       REAL DEFAULT 0,
    clicks          REAL DEFAULT 0,
    link_clicks     REAL DEFAULT 0,
    results         REAL DEFAULT 0,
    conversations   REAL DEFAULT 0,
    cost_per_conversation REAL DEFAULT 0,
    purchases       REAL DEFAULT 0,
    revenue         REAL DEFAULT 0,
    currency        TEXT DEFAULT 'USD',
    region          TEXT,
    delivery        TEXT,
    objective       TEXT,
    budget          REAL,
    raw_json        TEXT,  -- full original row, for reference
    FOREIGN KEY (upload_id) REFERENCES uploads(id)
);

CREATE INDEX IF NOT EXISTS idx_ad_rows_date     ON ad_rows(date);
CREATE INDEX IF NOT EXISTS idx_ad_rows_campaign ON ad_rows(campaign);
CREATE INDEX IF NOT EXISTS idx_ad_rows_adset    ON ad_rows(adset);
CREATE INDEX IF NOT EXISTS idx_ad_rows_ad       ON ad_rows(ad);
CREATE INDEX IF NOT EXISTS idx_ad_rows_level    ON ad_rows(report_level);

-- Each AI analysis run: contains commentary + structured outputs
CREATE TABLE IF NOT EXISTS analyses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    upload_ids      TEXT,           -- JSON list of upload IDs involved
    date_range      TEXT,           -- "2026-04-01..2026-04-30"
    summary         TEXT,           -- executive summary from Claude
    commentary      TEXT,           -- longer narrative
    metrics_json    TEXT,           -- snapshot of key metrics at time of run
    model           TEXT
);

-- Structured recommendations from the AI
CREATE TABLE IF NOT EXISTS recommendations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id     INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    category        TEXT NOT NULL,   -- working | not_working | at_risk | needs_scaling
    entity_level    TEXT,            -- campaign | adset | ad
    entity_name     TEXT,
    headline        TEXT,
    rationale       TEXT,
    suggested_action TEXT,
    outcome         TEXT,            -- filled later: improved | worsened | unchanged | pending
    FOREIGN KEY (analysis_id) REFERENCES analyses(id)
);

-- Long-term memory entries Claude can recall (compact, durable facts)
CREATE TABLE IF NOT EXISTS memory_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- insight | trend | risk | scaling | user_note | event
    entity_level    TEXT,
    entity_name     TEXT,
    content         TEXT NOT NULL,
    source_analysis_id INTEGER,
    FOREIGN KEY (source_analysis_id) REFERENCES analyses(id)
);

CREATE INDEX IF NOT EXISTS idx_memory_entity ON memory_entries(entity_name);
CREATE INDEX IF NOT EXISTS idx_memory_kind   ON memory_entries(kind);

-- Free-form chat history
CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    role            TEXT NOT NULL,   -- user | assistant
    content         TEXT NOT NULL
);

-- User notes / overrides (e.g., "we paused campaign X for budget reasons")
CREATE TABLE IF NOT EXISTS user_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    entity_level    TEXT,
    entity_name     TEXT,
    note            TEXT NOT NULL
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        # Lightweight migration for users on an older DB: add columns if missing.
        _add_col_if_missing(c, "uploads", "report_level", "TEXT")
        _add_col_if_missing(c, "uploads", "is_daily", "INTEGER DEFAULT 0")
        _add_col_if_missing(c, "uploads", "period_start", "TEXT")
        _add_col_if_missing(c, "uploads", "period_end", "TEXT")
        _add_col_if_missing(c, "ad_rows", "report_level", "TEXT")
        _add_col_if_missing(c, "ad_rows", "reach", "REAL DEFAULT 0")
        _add_col_if_missing(c, "ad_rows", "frequency", "REAL DEFAULT 0")
        _add_col_if_missing(c, "ad_rows", "link_clicks", "REAL DEFAULT 0")
        _add_col_if_missing(c, "ad_rows", "cost_per_conversation", "REAL DEFAULT 0")
        _add_col_if_missing(c, "ad_rows", "delivery", "TEXT")
        _add_col_if_missing(c, "ad_rows", "objective", "TEXT")
        _add_col_if_missing(c, "ad_rows", "budget", "REAL")


def _add_col_if_missing(c, table: str, col: str, decl: str):
    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# ---------- uploads ----------

def insert_upload(filename: str, row_count: int, date_min: Optional[str],
                  date_max: Optional[str], columns: list[str],
                  report_level: Optional[str] = None,
                  is_daily: bool = False,
                  period_start: Optional[str] = None,
                  period_end: Optional[str] = None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO uploads (filename, uploaded_at, row_count, date_min, date_max, "
            "raw_columns, report_level, is_daily, period_start, period_end) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (filename, now(), row_count, date_min, date_max, json.dumps(columns),
             report_level, 1 if is_daily else 0, period_start, period_end),
        )
        return cur.lastrowid


def list_uploads() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT id, filename, uploaded_at, row_count, date_min, date_max, "
            "report_level, is_daily, period_start, period_end "
            "FROM uploads ORDER BY uploaded_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- ad_rows ----------

def insert_ad_rows(upload_id: int, rows: list[dict]):
    if not rows:
        return
    with conn() as c:
        c.executemany(
            """INSERT INTO ad_rows
               (upload_id, report_level, date, campaign, adset, ad,
                spend, impressions, reach, frequency, clicks, link_clicks,
                results, conversations, cost_per_conversation,
                purchases, revenue, currency, region, delivery, objective, budget, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    upload_id,
                    r.get("report_level"),
                    r.get("date"),
                    r.get("campaign"),
                    r.get("adset"),
                    r.get("ad"),
                    float(r.get("spend") or 0),
                    float(r.get("impressions") or 0),
                    float(r.get("reach") or 0),
                    float(r.get("frequency") or 0),
                    float(r.get("clicks") or 0),
                    float(r.get("link_clicks") or 0),
                    float(r.get("results") or 0),
                    float(r.get("conversations") or 0),
                    float(r.get("cost_per_conversation") or 0),
                    float(r.get("purchases") or 0),
                    float(r.get("revenue") or 0),
                    r.get("currency") or "USD",
                    r.get("region"),
                    r.get("delivery"),
                    r.get("objective"),
                    float(r["budget"]) if r.get("budget") is not None else None,
                    json.dumps(r.get("raw") or {}, default=str),
                )
                for r in rows
            ],
        )


def fetch_ad_rows(date_from: Optional[str] = None, date_to: Optional[str] = None,
                  campaign: Optional[str] = None, adset: Optional[str] = None,
                  ad: Optional[str] = None,
                  region: Optional[str] = None,
                  report_level: Optional[str] = None) -> list[dict]:
    q = "SELECT * FROM ad_rows WHERE 1=1"
    params: list[Any] = []
    if date_from:
        q += " AND date >= ?"
        params.append(date_from)
    if date_to:
        q += " AND date <= ?"
        params.append(date_to)
    if campaign:
        q += " AND campaign = ?"
        params.append(campaign)
    if adset:
        q += " AND adset = ?"
        params.append(adset)
    if ad:
        q += " AND ad = ?"
        params.append(ad)
    if region:
        q += " AND region = ?"
        params.append(region)
    if report_level:
        q += " AND report_level = ?"
        params.append(report_level)
    q += " ORDER BY date ASC"
    with conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def fetch_ad_rows_for_level(level: str, **filters) -> list[dict]:
    """
    Returns the most specific rows we have for the requested aggregation level.

    Preference order:
      ad     -> rows from Ads exports
      adset  -> rows from Ad Sets exports, else Ads exports
      campaign -> Campaigns exports, else Ad Sets, else Ads
    This avoids double-counting if the user uploads multiple levels for the same period.
    """
    preference = {
        "ad":      ["ad", "mixed"],
        "adset":   ["adset", "ad", "mixed"],
        "campaign":["campaign", "adset", "ad", "mixed"],
    }.get(level, ["campaign", "adset", "ad", "mixed"])
    for lvl in preference:
        rows = fetch_ad_rows(report_level=lvl, **filters)
        if rows:
            return rows
    return []


def distinct_entities() -> dict:
    with conn() as c:
        campaigns = [r["campaign"] for r in c.execute(
            "SELECT DISTINCT campaign FROM ad_rows WHERE campaign IS NOT NULL ORDER BY campaign"
        ).fetchall()]
        adsets = [r["adset"] for r in c.execute(
            "SELECT DISTINCT adset FROM ad_rows WHERE adset IS NOT NULL ORDER BY adset"
        ).fetchall()]
        ads = [r["ad"] for r in c.execute(
            "SELECT DISTINCT ad FROM ad_rows WHERE ad IS NOT NULL ORDER BY ad"
        ).fetchall()]
        regions = [r["region"] for r in c.execute(
            "SELECT DISTINCT region FROM ad_rows WHERE region IS NOT NULL AND region != '' ORDER BY region"
        ).fetchall()]
    return {"campaigns": campaigns, "adsets": adsets, "ads": ads, "regions": regions}


# ---------- analyses & recommendations ----------

def insert_analysis(upload_ids: list[int], date_range: str, summary: str,
                    commentary: str, metrics: dict, model: str) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO analyses (created_at, upload_ids, date_range, summary, commentary, metrics_json, model) "
            "VALUES (?,?,?,?,?,?,?)",
            (now(), json.dumps(upload_ids), date_range, summary, commentary,
             json.dumps(metrics), model),
        )
        return cur.lastrowid


def insert_recommendations(analysis_id: int, recs: list[dict]):
    if not recs:
        return
    with conn() as c:
        c.executemany(
            """INSERT INTO recommendations
               (analysis_id, created_at, category, entity_level, entity_name,
                headline, rationale, suggested_action, outcome)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (
                    analysis_id, now(),
                    r.get("category", "working"),
                    r.get("entity_level"),
                    r.get("entity_name"),
                    r.get("headline"),
                    r.get("rationale"),
                    r.get("suggested_action"),
                    r.get("outcome", "pending"),
                )
                for r in recs
            ],
        )


def list_analyses(limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT id, created_at, date_range, summary, model FROM analyses "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_analysis(analysis_id: int) -> Optional[dict]:
    with conn() as c:
        a = c.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
        if not a:
            return None
        d = dict(a)
        d["recommendations"] = [dict(r) for r in c.execute(
            "SELECT * FROM recommendations WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchall()]
        return d


def latest_recommendations(limit: int = 30) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM recommendations ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- memory ----------

def insert_memory(kind: str, content: str, entity_level: Optional[str] = None,
                  entity_name: Optional[str] = None,
                  source_analysis_id: Optional[int] = None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO memory_entries (created_at, kind, entity_level, entity_name, content, source_analysis_id) "
            "VALUES (?,?,?,?,?,?)",
            (now(), kind, entity_level, entity_name, content, source_analysis_id),
        )
        return cur.lastrowid


def fetch_memory(limit: int = 200) -> list[dict]:
    """Most recent memory entries, newest first. Claude loads these as context."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM memory_entries ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- chat ----------

def insert_chat(role: str, content: str):
    with conn() as c:
        c.execute(
            "INSERT INTO chat_messages (created_at, role, content) VALUES (?,?,?)",
            (now(), role, content),
        )


def fetch_chat(limit: int = 40) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT role, content, created_at FROM chat_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        # return oldest first for replay
        return list(reversed([dict(r) for r in rows]))


# ---------- user notes ----------

def insert_note(note: str, entity_level: Optional[str] = None,
                entity_name: Optional[str] = None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO user_notes (created_at, entity_level, entity_name, note) VALUES (?,?,?,?)",
            (now(), entity_level, entity_name, note),
        )
        return cur.lastrowid


def list_notes(limit: int = 100) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM user_notes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {DB_PATH}")
