"""
Analytics engine.

Computes the core metrics requested in the spec:
  - Daily CAC change
  - Daily cost per conversation
  - Daily conversations
  - Daily spend
  - Plus advanced: ROAS, CTR, CPC, CPM, conversion rate

Supports Campaign / Ad Set / Ad level rollups,
date-range filtering, granularity (daily/weekly/monthly),
and USD->INR conversion.
"""

import os
import pandas as pd
from typing import Optional
from . import database as db


USD_TO_INR = float(os.environ.get("USD_TO_INR", "83.0"))


def _convert_to_inr(row: dict) -> dict:
    if row.get("currency") == "USD":
        for k in ("spend", "revenue"):
            row[k] = (row.get(k) or 0) * USD_TO_INR
        row["currency"] = "INR"
    return row


def _to_dataframe(rows: list[dict], convert: bool = True) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=[
            "date", "campaign", "adset", "ad", "spend", "impressions",
            "clicks", "results", "conversations", "purchases", "revenue", "currency"
        ])
    if convert:
        rows = [_convert_to_inr(dict(r)) for r in rows]
    df = pd.DataFrame(rows)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


def _safe_div(a, b):
    try:
        if b == 0 or pd.isna(b):
            return 0.0
        return float(a) / float(b)
    except Exception:
        return 0.0


def compute_metrics(df: pd.DataFrame) -> dict:
    """Aggregate KPI summary across the whole dataframe."""
    if df.empty:
        return {
            "spend": 0, "conversations": 0, "purchases": 0, "revenue": 0,
            "impressions": 0, "clicks": 0, "results": 0,
            "cac": 0, "cost_per_conversation": 0, "ctr": 0, "cpc": 0, "cpm": 0, "roas": 0,
            "conversion_rate": 0,
        }
    spend = float(df["spend"].sum())
    conversations = float(df["conversations"].sum())
    purchases = float(df["purchases"].sum())
    revenue = float(df["revenue"].sum())
    impressions = float(df["impressions"].sum())
    clicks = float(df["clicks"].sum())
    results = float(df["results"].sum())
    return {
        "spend": round(spend, 2),
        "conversations": round(conversations, 2),
        "purchases": round(purchases, 2),
        "revenue": round(revenue, 2),
        "impressions": round(impressions, 2),
        "clicks": round(clicks, 2),
        "results": round(results, 2),
        "cac": round(_safe_div(spend, purchases or results), 2),
        "cost_per_conversation": round(_safe_div(spend, conversations), 2),
        "ctr": round(_safe_div(clicks, impressions) * 100, 4),
        "cpc": round(_safe_div(spend, clicks), 2),
        "cpm": round(_safe_div(spend, impressions) * 1000, 2),
        "roas": round(_safe_div(revenue, spend), 3),
        "conversion_rate": round(_safe_div(purchases or results, clicks) * 100, 4),
    }


def timeseries(df: pd.DataFrame, granularity: str = "daily") -> list[dict]:
    """Return aggregated time series for charts."""
    if df.empty or "date" not in df.columns:
        return []
    grp_key = {"daily": "D", "weekly": "W", "monthly": "MS"}.get(granularity, "D")
    g = df.dropna(subset=["date"]).groupby(pd.Grouper(key="date", freq=grp_key)).agg(
        spend=("spend", "sum"),
        conversations=("conversations", "sum"),
        purchases=("purchases", "sum"),
        revenue=("revenue", "sum"),
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
        results=("results", "sum"),
    ).reset_index()
    g["cac"] = g.apply(lambda r: _safe_div(r["spend"], r["purchases"] or r["results"]), axis=1)
    g["cost_per_conversation"] = g.apply(lambda r: _safe_div(r["spend"], r["conversations"]), axis=1)
    g["roas"] = g.apply(lambda r: _safe_div(r["revenue"], r["spend"]), axis=1)
    g["date"] = g["date"].dt.strftime("%Y-%m-%d")
    return g.to_dict(orient="records")


def breakdown(df: pd.DataFrame, level: str = "campaign", top_n: int = 25) -> list[dict]:
    """Per-entity breakdown at campaign / adset / ad level."""
    if df.empty or level not in df.columns:
        return []
    g = df.groupby(level).agg(
        spend=("spend", "sum"),
        conversations=("conversations", "sum"),
        purchases=("purchases", "sum"),
        revenue=("revenue", "sum"),
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
        results=("results", "sum"),
    ).reset_index()
    g["cac"] = g.apply(lambda r: _safe_div(r["spend"], r["purchases"] or r["results"]), axis=1)
    g["cost_per_conversation"] = g.apply(lambda r: _safe_div(r["spend"], r["conversations"]), axis=1)
    g["roas"] = g.apply(lambda r: _safe_div(r["revenue"], r["spend"]), axis=1)
    g["ctr"] = g.apply(lambda r: _safe_div(r["clicks"], r["impressions"]) * 100, axis=1)
    g = g.sort_values("spend", ascending=False).head(top_n)
    g = g.rename(columns={level: "entity"})
    return g.to_dict(orient="records")


def daily_cac_change(df: pd.DataFrame) -> list[dict]:
    """Required: daily CAC change over the date range."""
    ts = timeseries(df, "daily")
    out = []
    prev = None
    for row in ts:
        cur = row["cac"]
        change = None if prev in (None, 0) else round(((cur - prev) / prev) * 100, 2)
        out.append({"date": row["date"], "cac": round(cur, 2), "change_pct": change})
        prev = cur
    return out


def compare_periods(df_a: pd.DataFrame, df_b: pd.DataFrame) -> dict:
    """Compare two periods (e.g. this week vs last week)."""
    a = compute_metrics(df_a)
    b = compute_metrics(df_b)
    delta = {}
    for k in a:
        if isinstance(a[k], (int, float)) and isinstance(b[k], (int, float)):
            delta[k] = {
                "current": a[k],
                "previous": b[k],
                "abs": round(a[k] - b[k], 2),
                "pct": round(_safe_div(a[k] - b[k], b[k]) * 100, 2),
            }
    return delta


def load_filtered(level: str, date_from: Optional[str], date_to: Optional[str],
                  campaign: Optional[str], adset: Optional[str], ad: Optional[str],
                  region: Optional[str] = None,
                  convert_currency: bool = True) -> pd.DataFrame:
    """Fetch the most specific rows we have for the requested aggregation level."""
    rows = db.fetch_ad_rows_for_level(
        level,
        date_from=date_from, date_to=date_to,
        campaign=campaign, adset=adset, ad=ad, region=region,
    )
    return _to_dataframe(rows, convert=convert_currency)


def full_snapshot(date_from: Optional[str] = None, date_to: Optional[str] = None,
                  campaign: Optional[str] = None, adset: Optional[str] = None,
                  ad: Optional[str] = None, region: Optional[str] = None,
                  granularity: str = "daily") -> dict:
    # Use each level's own export when available to avoid double-counting
    # if the user has uploaded Campaigns + Ad Sets + Ads files for the same period.
    df_camp = load_filtered("campaign", date_from, date_to, campaign, adset, ad, region)
    df_aset = load_filtered("adset",    date_from, date_to, campaign, adset, ad, region)
    df_ad   = load_filtered("ad",       date_from, date_to, campaign, adset, ad, region)

    # Headline KPIs prefer the highest-level (least granular = no double-count) data.
    df_main = df_camp if not df_camp.empty else (df_aset if not df_aset.empty else df_ad)

    return {
        "metrics": compute_metrics(df_main),
        "timeseries": timeseries(df_main, granularity),
        "campaigns": breakdown(df_camp if not df_camp.empty else df_main, "campaign"),
        "adsets":    breakdown(df_aset if not df_aset.empty else df_main, "adset"),
        "ads":       breakdown(df_ad   if not df_ad.empty   else df_main, "ad"),
        "daily_cac_change": daily_cac_change(df_main),
        "currency": "INR",
        "row_count": int(len(df_main)),
        "levels_loaded": {
            "campaign": int(len(df_camp)),
            "adset":    int(len(df_aset)),
            "ad":       int(len(df_ad)),
        },
    }
