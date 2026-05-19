"""
File parser for Meta Ads Manager exports (CSV / XLSX / XLS).

Meta Ads Manager actually exports ONE FILE PER LEVEL:
  - Campaigns export   (only the Campaign Name column identifies the entity)
  - Ad Sets export     (Campaign Name + Ad Set Name)
  - Ads export         (Campaign Name + Ad Set Name + Ad Name)

Files are .xls by default (legacy binary Excel, needs xlrd) unless the user
chose CSV. They typically have NO per-day rows — instead each row is one
entity aggregated over "Reporting starts" → "Reporting ends" (the date range
shown in the filename, e.g. "Mar-9-2026-May-16-2026").

This parser:
  - Reads CSV / XLSX / XLS
  - Auto-detects the report level from the filename and the columns present
  - Normalises a large set of Meta column synonyms to a canonical schema
  - Skips Meta's summary / "Total" / "Account total" rows
  - When no per-day Day column exists, uses Reporting starts as the row date
    (so the data lands somewhere in the timeseries; the UI also surfaces the
    fact that the export is aggregate, not daily)
"""

import io
import re
from typing import Optional

import pandas as pd


# Canonical -> list of acceptable synonyms (will be norm()'d before matching)
COLUMN_MAP = {
    # entity identification
    "campaign":      ["campaign name", "campaign", "campaign_name"],
    "adset":         ["ad set name", "ad set", "adset", "adset name",
                      "ad_set", "ad_set_name", "ad-set name"],
    "ad":            ["ad name", "ad", "ad_name"],

    # dates
    "date":          ["day", "date", "reporting date"],
    "reporting_starts": ["reporting starts", "reporting_starts", "starts",
                         "date start", "from date", "period start"],
    "reporting_ends":   ["reporting ends", "reporting_ends", "ends",
                         "date stop", "to date", "period end"],

    # spend
    "spend":         ["amount spent", "amount spent (usd)", "amount spent (inr)",
                      "amount spent (eur)", "amount spent (gbp)",
                      "spend", "cost", "amount_spent", "amount_spent_usd",
                      "amount_spent_inr", "total spent"],

    # delivery
    "impressions":   ["impressions", "impr", "imps"],
    "reach":         ["reach"],
    "frequency":     ["frequency", "freq"],

    # clicks
    "clicks":        ["clicks (all)", "clicks all", "clicks", "link clicks",
                      "link_clicks", "clicks_all"],
    "link_clicks":   ["link clicks", "outbound clicks", "outbound_clicks"],

    # conversations (the spec's primary KPI for this account)
    "conversations": ["messaging conversations started",
                      "new messaging conversations",
                      "conversations started",
                      "conversations",
                      "messaging_conversations_started",
                      "messaging conversations",
                      "messaging conv. started"],
    "cost_per_conversation": [
        "cost per messaging conversation started",
        "cost per messaging conversation",
        "cost per new messaging conversation",
        "cost per conversation",
    ],

    # results column (generic Meta "Results" column when optimizing for X)
    "results":       ["results", "result"],
    "cost_per_result": ["cost per result", "cost per results"],

    # purchases / revenue
    "purchases":     ["purchases", "website purchases", "purchase",
                      "meta pixel purchases", "offline purchases"],
    "revenue":       ["purchase conversion value", "purchases conversion value",
                      "website purchases conversion value", "purchase value",
                      "revenue", "conversion value",
                      "website_purchase_conversion_value"],
    "roas_value":    ["website purchase roas", "purchase roas (return on ad spend)",
                      "purchase roas", "roas"],

    # misc
    "currency":      ["currency", "reporting currency"],
    "region":        ["region", "country", "city", "geo", "location"],
    "delivery":      ["delivery status", "delivery", "status"],
    "objective":     ["objective", "campaign objective"],
    "budget":        ["budget", "campaign budget", "ad set budget", "daily budget"],
}


# Rows whose entity name matches these are Meta's roll-up rows — skip them.
SUMMARY_ROW_PATTERNS = [
    re.compile(r"^\s*(account|campaign|ad ?set|ad)\s+total\s*$", re.I),
    re.compile(r"^\s*total\s*$", re.I),
    re.compile(r"^\s*results from \d+ ", re.I),
    re.compile(r"^\s*grand total\s*$", re.I),
]


def _norm(s) -> str:
    s = str(s).strip().lower()
    s = re.sub(r"[\(\)\[\]]", "", s)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def _build_reverse_map() -> dict[str, str]:
    rev = {}
    for canon, syns in COLUMN_MAP.items():
        for s in syns:
            rev[_norm(s)] = canon
        rev[_norm(canon)] = canon
    return rev


REVERSE = _build_reverse_map()


def _read_dataframe(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """
    Read CSV / XLS / XLSX with the right engine.

    Meta Ads Manager 'Export to Excel' actually produces one of THREE different
    file formats with the same .xls extension:
      1. Real binary BIFF Excel (rare, old Ads Manager UI)  -> xlrd
      2. SpreadsheetML 2003 XML (most common today)         -> our XML parser
      3. HTML table disguised as .xls                       -> pd.read_html
    Plus actual .xlsx (real Open XML zip) when the user picks the modern option.

    We sniff the first bytes to pick the right reader and fall back gracefully.
    """
    name = (filename or "").lower()
    head = file_bytes[:512]
    head_l = head.lower()

    def _is_xlsx_zip(b: bytes) -> bool:
        return b.startswith(b"PK\x03\x04")

    def _is_biff_xls(b: bytes) -> bool:
        # Compound File Binary (legacy .xls): D0 CF 11 E0 A1 B1 1A E1
        return b.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1")

    def _is_spreadsheetml(b: bytes) -> bool:
        # Excel 2003 XML — has <?xml + Workbook / urn:schemas-microsoft-com
        return b.lstrip().startswith(b"<?xml") and (b"urn:schemas-microsoft-com:office:spreadsheet" in b or b"<Workbook" in b)

    def _is_html(b: bytes) -> bool:
        return b"<html" in b.lower() or b"<table" in b.lower()

    # Detection by content first, falling back to extension
    if _is_xlsx_zip(head):
        return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    if _is_biff_xls(head):
        return pd.read_excel(io.BytesIO(file_bytes), engine="xlrd")
    if _is_spreadsheetml(head_l) or _is_spreadsheetml(head):
        return _read_spreadsheetml_xml(file_bytes)
    if _is_html(head):
        return _read_html_table(file_bytes)

    # No clear signature — fall back to extension heuristics
    if name.endswith(".xlsx"):
        return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    if name.endswith(".xls"):
        for attempt in (
            lambda: pd.read_excel(io.BytesIO(file_bytes), engine="xlrd"),
            lambda: _read_spreadsheetml_xml(file_bytes),
            lambda: _read_html_table(file_bytes),
            lambda: pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl"),
        ):
            try:
                return attempt()
            except Exception:
                continue
        raise ValueError(
            "Could not parse .xls — Meta sometimes exports an unusual XML "
            "variant. Re-export from Ads Manager choosing .xlsx instead of .xls, "
            "or open in Excel and Save As CSV."
        )

    # CSV path
    try:
        return pd.read_csv(io.BytesIO(file_bytes))
    except UnicodeDecodeError:
        return pd.read_csv(io.BytesIO(file_bytes), encoding="latin-1")


def _read_spreadsheetml_xml(file_bytes: bytes) -> pd.DataFrame:
    """Parse Excel 2003 SpreadsheetML XML — the format Meta uses for .xls."""
    try:
        from lxml import etree
        root = etree.fromstring(file_bytes)
    except Exception:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(file_bytes)

    ns = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}

    rows_data: list[list] = []
    # Find the first non-empty Worksheet -> Table
    for table in root.iter("{%s}Table" % ns["ss"]):
        for row_el in table.iter("{%s}Row" % ns["ss"]):
            row_vals = []
            expected_idx = 1
            for cell in row_el.findall("{%s}Cell" % ns["ss"]):
                # Honor ss:Index gaps (sparse rows)
                idx_attr = cell.get("{%s}Index" % ns["ss"])
                if idx_attr:
                    idx = int(idx_attr)
                    while expected_idx < idx:
                        row_vals.append(None)
                        expected_idx += 1
                data = cell.find("{%s}Data" % ns["ss"])
                row_vals.append(data.text if data is not None else None)
                expected_idx += 1
            rows_data.append(row_vals)
        if rows_data:
            break  # take the first sheet with data

    if not rows_data:
        raise ValueError("SpreadsheetML XML had no rows")

    # First non-empty row = header
    header = None
    body = []
    for r in rows_data:
        if header is None:
            if any(c not in (None, "") for c in r):
                header = [str(c) if c is not None else f"col_{i}" for i, c in enumerate(r)]
        else:
            # Pad/truncate to header length
            if len(r) < len(header):
                r = r + [None] * (len(header) - len(r))
            elif len(r) > len(header):
                r = r[: len(header)]
            body.append(r)
    if header is None:
        raise ValueError("SpreadsheetML XML had no header row")
    return pd.DataFrame(body, columns=header)


def _read_html_table(file_bytes: bytes) -> pd.DataFrame:
    """Read the first usable table from HTML-disguised Excel exports."""
    bio = io.BytesIO(file_bytes)
    tables = pd.read_html(bio)
    if not tables:
        raise ValueError("no HTML table found")
    # Return the largest by row count — Meta sometimes prepends a one-row meta table
    return max(tables, key=lambda t: len(t))


def _detect_currency(raw_columns: list[str], default: str = "USD") -> str:
    cols = " ".join(c.lower() for c in raw_columns)
    if "inr" in cols or "₹" in cols:
        return "INR"
    if "usd" in cols or "$" in cols:
        return "USD"
    if "eur" in cols or "€" in cols:
        return "EUR"
    if "gbp" in cols or "£" in cols:
        return "GBP"
    return default


def _coerce_float(v) -> float:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s in ("-", "—"):
        return 0.0
    # Strip currency symbols, commas, percent signs
    s = re.sub(r"[^\d\.\-]", "", s)
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _coerce_date(v) -> Optional[str]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        d = pd.to_datetime(v, errors="coerce")
        if pd.isna(d):
            return None
        return d.strftime("%Y-%m-%d")
    except Exception:
        return None


def _detect_report_level(filename: str, columns_canonical: set[str]) -> str:
    """
    Returns one of: 'campaign', 'adset', 'ad', 'mixed'.
    Filename hints take priority since Meta names files clearly:
      Force-Interns-Campaigns-...   -> campaign
      Force-Interns-Ad-sets-...     -> adset
      Force-Interns-Ads-...         -> ad
    Falls back to column presence.
    """
    fn = (filename or "").lower()
    # Hyphen/space tolerant matching
    if re.search(r"(?:^|[\-_ /])ad[\- ]?sets?(?:[\-_ \.]|$)", fn):
        return "adset"
    if re.search(r"(?:^|[\-_ /])campaigns?(?:[\-_ \.]|$)", fn):
        return "campaign"
    if re.search(r"(?:^|[\-_ /])ads?(?:[\-_ \.]|$)", fn):
        return "ad"

    # Fallback to columns
    if "ad" in columns_canonical:
        return "ad"
    if "adset" in columns_canonical:
        return "adset"
    if "campaign" in columns_canonical:
        return "campaign"
    return "mixed"


def _is_summary_row(rec: dict) -> bool:
    """Skip Meta's 'Account total' / 'Total' aggregate rows."""
    for key in ("ad", "adset", "campaign"):
        val = rec.get(key)
        if not val:
            continue
        for pat in SUMMARY_ROW_PATTERNS:
            if pat.match(str(val)):
                return True
    return False


def parse_file(file_bytes: bytes, filename: str) -> dict:
    """
    Returns:
      {
        "rows": [ {date, campaign, adset, ad, spend, ...}, ... ],
        "raw_columns": [...],
        "row_count": int,
        "date_min": "YYYY-MM-DD" | None,
        "date_max": "YYYY-MM-DD" | None,
        "report_level": "campaign|adset|ad|mixed",
        "is_daily": bool,         # True if rows have per-day dates
        "period_start": "YYYY-MM-DD" | None,  # from Reporting starts
        "period_end":   "YYYY-MM-DD" | None,
      }
    """
    df = _read_dataframe(file_bytes, filename)
    # Drop fully-empty rows that Meta sometimes appends
    df = df.dropna(how="all").reset_index(drop=True)
    raw_columns = [str(c) for c in df.columns]

    # Build column -> canonical map for this file
    col_to_canon = {}
    for c in raw_columns:
        canon = REVERSE.get(_norm(c))
        if canon and canon not in col_to_canon.values():
            col_to_canon[c] = canon

    canon_set = set(col_to_canon.values())
    report_level = _detect_report_level(filename, canon_set)
    currency = _detect_currency(raw_columns)

    # Period from Reporting starts/ends (used when no Day column exists)
    period_start = None
    period_end = None
    for orig, canon in col_to_canon.items():
        if canon == "reporting_starts":
            vals = df[orig].dropna().tolist()
            if vals:
                period_start = _coerce_date(vals[0])
        if canon == "reporting_ends":
            vals = df[orig].dropna().tolist()
            if vals:
                period_end = _coerce_date(vals[0])
    # Fallback: try to lift the period from the filename, e.g. "Mar-9-2026-May-16-2026"
    if not (period_start and period_end):
        m = re.search(
            r"([A-Za-z]{3,9}[-\s]\d{1,2}[-\s]\d{4})[-\s]([A-Za-z]{3,9}[-\s]\d{1,2}[-\s]\d{4})",
            filename or "",
        )
        if m:
            period_start = period_start or _coerce_date(m.group(1).replace("-", " "))
            period_end = period_end or _coerce_date(m.group(2).replace("-", " "))

    has_day_col = "date" in canon_set

    out_rows = []
    dates = []
    for _, row in df.iterrows():
        rec = {
            "currency": currency,
            "report_level": report_level,
            "raw": {str(k): (None if pd.isna(v) else v) for k, v in row.to_dict().items()},
        }
        # Per-row date or fallback to period start
        rec["date"] = None
        for orig_col, canon in col_to_canon.items():
            val = row[orig_col]
            if canon == "date":
                rec["date"] = _coerce_date(val)
            elif canon in ("reporting_starts", "reporting_ends"):
                # already captured at file level
                pass
            elif canon in ("campaign", "adset", "ad", "region", "currency",
                           "delivery", "objective"):
                rec[canon] = None if pd.isna(val) else str(val).strip()
            elif canon == "budget":
                rec["budget"] = _coerce_float(val)
            else:
                # numeric metrics
                rec[canon] = _coerce_float(val)

        if not rec["date"]:
            rec["date"] = period_start

        if rec["date"]:
            dates.append(rec["date"])

        # If we got a cost_per_conversation directly but not conversations,
        # try to back-derive: convs = spend / cpc
        if rec.get("conversations") in (None, 0) and rec.get("cost_per_conversation"):
            sp = rec.get("spend") or 0
            cpc = rec.get("cost_per_conversation") or 0
            if cpc > 0:
                rec["conversations"] = round(sp / cpc, 2)

        # Skip rows with no identifiable entity
        if not any(rec.get(k) for k in ("campaign", "adset", "ad")):
            continue
        # Skip Meta summary rows
        if _is_summary_row(rec):
            continue
        out_rows.append(rec)

    return {
        "rows": out_rows,
        "raw_columns": raw_columns,
        "row_count": len(out_rows),
        "date_min": min(dates) if dates else period_start,
        "date_max": max(dates) if dates else period_end,
        "report_level": report_level,
        "is_daily": has_day_col,
        "period_start": period_start,
        "period_end": period_end,
    }


# Smart matching: detect when an entity in a new upload is the same as a historical one
def fuzzy_match(name: str, candidates: list[str], threshold: float = 0.8) -> Optional[str]:
    """Return the best-matching historical name above threshold, or None."""
    if not name or not candidates:
        return None
    name_n = _norm(name)
    best = None
    best_score = 0.0
    for cand in candidates:
        c_n = _norm(cand)
        if c_n == name_n:
            return cand
        a = set(name_n.split("_"))
        b = set(c_n.split("_"))
        if not a or not b:
            continue
        score = len(a & b) / max(len(a | b), 1)
        if score > best_score:
            best_score = score
            best = cand
    return best if best_score >= threshold else None
