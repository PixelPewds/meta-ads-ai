"""
Period-vs-period comparison + creative fatigue detection + automatic outcome
scoring for previously-issued recommendations.

These are pure data functions — no Claude calls. They feed both the dashboard
comparison view and the AI context, so the model reasons over real deltas
rather than guessing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from . import analytics, database as db


# ----------------------------------------------------------------------------
# Period vs period
# ----------------------------------------------------------------------------

def _to_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def derive_previous_period(date_from: str, date_to: str) -> tuple[str, str]:
    """Previous window of equal length, ending the day before date_from."""
    a = _to_date(date_from)
    b = _to_date(date_to)
    length = (b - a).days + 1
    prev_end = a - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    return prev_start.strftime("%Y-%m-%d"), prev_end.strftime("%Y-%m-%d")


def compare_periods(date_from: str, date_to: str,
                    prev_from: Optional[str] = None,
                    prev_to: Optional[str] = None,
                    level: str = "campaign") -> dict:
    """
    Returns headline KPI deltas + per-entity deltas at the requested level.
    Falls back to the previous-window-of-equal-length if prev_* not supplied.
    """
    if not (prev_from and prev_to):
        prev_from, prev_to = derive_previous_period(date_from, date_to)

    cur_df  = analytics.load_filtered(level, date_from, date_to, None, None, None)
    prev_df = analytics.load_filtered(level, prev_from, prev_to, None, None, None)

    cur_m  = analytics.compute_metrics(cur_df)
    prev_m = analytics.compute_metrics(prev_df)

    def _pct(c, p):
        if not p:
            return None if not c else float("inf")
        return round(((c - p) / p) * 100, 2)

    headline = {
        k: {
            "current": cur_m[k],
            "previous": prev_m[k],
            "abs": round(cur_m[k] - prev_m[k], 2),
            "pct": _pct(cur_m[k], prev_m[k]),
        }
        for k in cur_m
    }

    # Per-entity deltas
    entities = _entity_deltas(cur_df, prev_df, level)

    return {
        "current":  {"from": date_from, "to": date_to,  "metrics": cur_m},
        "previous": {"from": prev_from, "to": prev_to,  "metrics": prev_m},
        "headline": headline,
        "entities": entities,
        "level":    level,
    }


def _entity_deltas(cur_df: pd.DataFrame, prev_df: pd.DataFrame, level: str) -> list[dict]:
    if cur_df.empty and prev_df.empty:
        return []
    cur  = analytics.breakdown(cur_df,  level, top_n=200)
    prev = analytics.breakdown(prev_df, level, top_n=200)
    prev_by = {r["entity"]: r for r in prev}
    out = []
    for r in cur:
        p = prev_by.get(r["entity"], {})
        out.append({
            "entity": r["entity"],
            "spend":  _delta(r.get("spend"),  p.get("spend")),
            "conversations": _delta(r.get("conversations"), p.get("conversations")),
            "cost_per_conversation": _delta(r.get("cost_per_conversation"), p.get("cost_per_conversation"), invert=True),
            "cac":    _delta(r.get("cac"),    p.get("cac"), invert=True),
            "roas":   _delta(r.get("roas"),   p.get("roas")),
            "ctr":    _delta(r.get("ctr"),    p.get("ctr")),
        })
    # entities present previously but gone now
    cur_names = {r["entity"] for r in cur}
    for name, p in prev_by.items():
        if name in cur_names:
            continue
        out.append({
            "entity": name,
            "spend": _delta(0, p.get("spend")),
            "conversations": _delta(0, p.get("conversations")),
            "cost_per_conversation": _delta(0, p.get("cost_per_conversation"), invert=True),
            "cac": _delta(0, p.get("cac"), invert=True),
            "roas": _delta(0, p.get("roas")),
            "ctr": _delta(0, p.get("ctr")),
            "status": "removed",
        })
    out.sort(key=lambda e: abs((e["spend"]["pct"] or 0)), reverse=True)
    return out[:50]


def _delta(cur, prev, invert: bool = False) -> dict:
    cur = float(cur or 0)
    prev = float(prev or 0)
    abs_d = round(cur - prev, 2)
    if not prev:
        pct = None if not cur else float("inf")
    else:
        pct = round(((cur - prev) / prev) * 100, 2)
    direction = "flat"
    if abs_d > 0:
        direction = "down" if invert else "up"
    elif abs_d < 0:
        direction = "up" if invert else "down"
    return {"current": cur, "previous": prev, "abs": abs_d, "pct": pct, "direction": direction}


# ----------------------------------------------------------------------------
# Creative fatigue
# ----------------------------------------------------------------------------

def detect_fatigue(date_from: str, date_to: str,
                   prev_from: Optional[str] = None,
                   prev_to: Optional[str] = None) -> list[dict]:
    """
    Per-ad heuristic: frequency rose AND (CTR fell OR cost-per-conversation rose).
    Returns at-risk candidates ranked by severity. Requires ad-level data;
    returns [] gracefully if only campaign- or adset-level files were uploaded.
    """
    if not (prev_from and prev_to):
        prev_from, prev_to = derive_previous_period(date_from, date_to)

    cur_df  = analytics.load_filtered("ad", date_from, date_to, None, None, None)
    prev_df = analytics.load_filtered("ad", prev_from, prev_to, None, None, None)
    if cur_df.empty or prev_df.empty:
        return []
    # Need ad-level rows specifically. If we fell back to campaign data,
    # there'll be no 'ad' column; skip fatigue detection rather than crash.
    if "ad" not in cur_df.columns or "ad" not in prev_df.columns:
        return []

    cur  = analytics.breakdown(cur_df,  "ad", top_n=200)
    prev = {r["entity"]: r for r in analytics.breakdown(prev_df, "ad", top_n=200)}
    flagged = []
    for r in cur:
        p = prev.get(r["entity"])
        if not p:
            continue
        # Frequency requires impressions/reach — we computed reach in DB; recompute here
        cur_freq  = _safe_div(r.get("impressions", 0), _reach(r, cur_df))
        prev_freq = _safe_div(p.get("impressions", 0), _reach(p, prev_df))
        ctr_drop  = (p.get("ctr") or 0) > 0 and (r.get("ctr") or 0) < (p.get("ctr") or 0) * 0.85
        freq_rise = prev_freq > 0 and cur_freq > prev_freq * 1.15
        cpc_rise  = (p.get("cost_per_conversation") or 0) > 0 and (r.get("cost_per_conversation") or 0) > (p.get("cost_per_conversation") or 0) * 1.20
        if freq_rise and (ctr_drop or cpc_rise):
            flagged.append({
                "entity": r["entity"],
                "level": "ad",
                "current": {"ctr": r.get("ctr"), "freq": round(cur_freq, 2),
                            "cost_per_conversation": r.get("cost_per_conversation")},
                "previous":{"ctr": p.get("ctr"), "freq": round(prev_freq, 2),
                            "cost_per_conversation": p.get("cost_per_conversation")},
                "signals": {
                    "ctr_drop": ctr_drop,
                    "freq_rise": freq_rise,
                    "cpc_rise": cpc_rise,
                },
                "headline": "Creative fatigue suspected",
                "rationale": _fatigue_reason(r, p, cur_freq, prev_freq),
                "suggested_action": "Refresh creative or rotate ad variant; consider lookalike expansion.",
            })
    return flagged


def _fatigue_reason(r, p, cur_freq, prev_freq) -> str:
    bits = []
    bits.append(f"frequency {prev_freq:.2f} → {cur_freq:.2f}")
    if p.get("ctr"):
        bits.append(f"CTR {p['ctr']:.2f}% → {r.get('ctr',0):.2f}%")
    if p.get("cost_per_conversation"):
        bits.append(f"cost/conv ₹{p['cost_per_conversation']:.0f} → ₹{r.get('cost_per_conversation',0):.0f}")
    return "; ".join(bits)


def _reach(row: dict, df: pd.DataFrame) -> float:
    """Pull reach for this entity from the underlying dataframe."""
    if "reach" not in df.columns or "ad" not in df.columns:
        return 0.0
    try:
        sub = df[df["ad"] == row.get("entity")]
        if sub.empty:
            return 0.0
        return float(sub["reach"].sum())
    except Exception:
        return 0.0


def _safe_div(a, b):
    try:
        if not b:
            return 0.0
        return float(a) / float(b)
    except Exception:
        return 0.0


# ----------------------------------------------------------------------------
# Outcome scoring of past recommendations
# ----------------------------------------------------------------------------

def score_previous_recommendations(window_days: int = 21) -> list[dict]:
    """
    For every recommendation with outcome='pending', compute objective deltas
    on the named entity in the post-recommendation window vs the equal-length
    pre-recommendation window. Updates outcome in DB and returns the scoring list.
    """
    scored = []
    pending = [r for r in db.latest_recommendations(200) if (r.get("outcome") or "pending") == "pending"]
    for rec in pending:
        entity = rec.get("entity_name")
        level  = rec.get("entity_level") or "campaign"
        if not entity:
            continue
        created = rec.get("created_at", "")[:10]  # YYYY-MM-DD
        if not created:
            continue
        try:
            post_from = created
            post_to   = (_to_date(created) + timedelta(days=window_days)).strftime("%Y-%m-%d")
            pre_to    = (_to_date(created) - timedelta(days=1)).strftime("%Y-%m-%d")
            pre_from  = (_to_date(created) - timedelta(days=window_days)).strftime("%Y-%m-%d")
        except Exception:
            continue

        filters_post = {level: entity}
        filters_pre  = {level: entity}
        cur_df  = analytics.load_filtered(level, post_from, post_to, **{k: filters_post.get(k) for k in ("campaign","adset","ad")})
        prev_df = analytics.load_filtered(level, pre_from,  pre_to,  **{k: filters_pre.get(k)  for k in ("campaign","adset","ad")})

        cur_m  = analytics.compute_metrics(cur_df)
        prev_m = analytics.compute_metrics(prev_df)

        if not (cur_df.shape[0] or prev_df.shape[0]):
            continue

        outcome, evidence = _judge_outcome(rec.get("category"), cur_m, prev_m)
        scored.append({
            "rec_id": rec["id"],
            "entity": entity,
            "category": rec.get("category"),
            "outcome": outcome,
            "evidence": evidence,
        })
        with db.conn() as c:
            c.execute(
                "UPDATE recommendations SET outcome = ? WHERE id = ?",
                (outcome, rec["id"]),
            )
            # Also write a memory entry so future Claude calls see it
            c.execute(
                "INSERT INTO memory_entries (created_at, kind, entity_level, entity_name, content, source_analysis_id) "
                "VALUES (?,?,?,?,?,?)",
                (db.now(), "outcome", level, entity,
                 f"[{rec.get('category')}] outcome={outcome}. {evidence}",
                 rec.get("analysis_id")),
            )
    return scored


def _judge_outcome(category: str, cur: dict, prev: dict) -> tuple[str, str]:
    """Simple heuristic by category. Returns (outcome, evidence string)."""
    cat = (category or "").lower()
    cac_delta  = (cur.get("cac", 0) or 0) - (prev.get("cac", 0) or 0)
    roas_delta = (cur.get("roas", 0) or 0) - (prev.get("roas", 0) or 0)
    conv_delta = (cur.get("conversations", 0) or 0) - (prev.get("conversations", 0) or 0)
    spend_delta = (cur.get("spend", 0) or 0) - (prev.get("spend", 0) or 0)

    evidence = (f"CAC Δ ₹{cac_delta:+.0f}; ROAS Δ {roas_delta:+.2f}; "
                f"convs Δ {conv_delta:+.0f}; spend Δ ₹{spend_delta:+.0f}")

    if cat == "needs_scaling":
        improved = conv_delta > 0 and roas_delta >= -0.1
    elif cat == "at_risk":
        improved = cac_delta < 0 or roas_delta > 0
    elif cat == "not_working":
        improved = cac_delta < 0 and conv_delta >= 0
    else:  # working
        improved = roas_delta >= 0 and conv_delta >= 0

    if abs(cac_delta) < 1 and abs(roas_delta) < 0.05 and abs(conv_delta) < 1:
        return "unchanged", evidence
    return ("improved" if improved else "worsened"), evidence
