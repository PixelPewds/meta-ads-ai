"""
FastAPI app — wires the dashboard frontend to the analytics engine, the parser,
the SQLite memory, and the Claude AI module.

Run:
    cd meta-ads-ai
    pip install -r requirements.txt
    uvicorn backend.main:app --reload --port 8000

Then open http://localhost:8000/
"""

import csv
import io
import json
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import ai, analytics, comparison, database as db, parser

load_dotenv()
db.init_db()

ROOT = Path(__file__).parent.parent
FRONTEND_DIR = ROOT / "frontend"

app = FastAPI(title="Meta Ads AI Analytics", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- frontend ----------

@app.get("/", response_class=HTMLResponse)
def index():
    idx = FRONTEND_DIR / "index.html"
    if not idx.exists():
        return HTMLResponse("<h1>Frontend missing</h1>", status_code=500)
    return HTMLResponse(idx.read_text(encoding="utf-8"))


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---------- uploads ----------

@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        try:
            content = await f.read()
            fname = f.filename or "unnamed.csv"
            parsed = parser.parse_file(content, fname)
            upload_id = db.insert_upload(
                filename=fname,
                row_count=parsed["row_count"],
                date_min=parsed["date_min"],
                date_max=parsed["date_max"],
                columns=parsed["raw_columns"],
                report_level=parsed.get("report_level"),
                is_daily=parsed.get("is_daily", False),
                period_start=parsed.get("period_start"),
                period_end=parsed.get("period_end"),
            )
            db.insert_ad_rows(upload_id, parsed["rows"])
            results.append({
                "filename": fname,
                "upload_id": upload_id,
                "row_count": parsed["row_count"],
                "date_min": parsed["date_min"],
                "date_max": parsed["date_max"],
                "report_level": parsed.get("report_level"),
                "is_daily": parsed.get("is_daily", False),
                "period_start": parsed.get("period_start"),
                "period_end": parsed.get("period_end"),
            })
        except Exception as e:
            results.append({"filename": f.filename, "error": str(e)})
    return {"uploaded": results}


@app.get("/api/uploads")
def get_uploads():
    return {"uploads": db.list_uploads()}


# ---------- entities ----------

@app.get("/api/entities")
def get_entities():
    return db.distinct_entities()


# ---------- analytics ----------

@app.get("/api/metrics")
def get_metrics(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    campaign: Optional[str] = None,
    adset: Optional[str] = None,
    ad: Optional[str] = None,
    region: Optional[str] = None,
    granularity: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
):
    snap = analytics.full_snapshot(date_from, date_to, campaign, adset, ad, region, granularity)
    return snap


@app.get("/api/compare")
def get_compare(
    date_from: str,
    date_to: str,
    prev_from: Optional[str] = None,
    prev_to: Optional[str] = None,
    level: str = Query("campaign", pattern="^(campaign|adset|ad)$"),
):
    return comparison.compare_periods(date_from, date_to, prev_from, prev_to, level)


@app.get("/api/fatigue")
def get_fatigue(
    date_from: str,
    date_to: str,
    prev_from: Optional[str] = None,
    prev_to: Optional[str] = None,
):
    try:
        return {"fatigue": comparison.detect_fatigue(date_from, date_to, prev_from, prev_to)}
    except Exception as e:
        # Fatigue needs ad-level data; degrade rather than 500.
        return {"fatigue": [], "warning": f"fatigue scan unavailable: {e}"}


@app.post("/api/score-outcomes")
def post_score_outcomes(payload: Optional[dict] = None):
    payload = payload or {}
    window = int(payload.get("window_days", 21))
    scored = comparison.score_previous_recommendations(window_days=window)
    return {"scored": scored, "count": len(scored)}


# ---------- AI ----------

@app.post("/api/analyze")
def post_analyze(payload: Optional[dict] = None):
    payload = payload or {}
    # First: judge how prior recommendations actually played out.
    # This writes outcomes back to DB so the AI sees them in the context block.
    try:
        comparison.score_previous_recommendations()
    except Exception as e:
        print(f"[analyze] outcome scoring failed (non-fatal): {e}")

    snap = analytics.full_snapshot(
        date_from=payload.get("date_from"),
        date_to=payload.get("date_to"),
        campaign=payload.get("campaign"),
        adset=payload.get("adset"),
        ad=payload.get("ad"),
        region=payload.get("region"),
        granularity=payload.get("granularity", "daily"),
    )

    # Bolt fatigue + period comparison into the snapshot so Claude sees them.
    if payload.get("date_from") and payload.get("date_to"):
        try:
            snap["fatigue"] = comparison.detect_fatigue(
                payload["date_from"], payload["date_to"]
            )
            snap["period_comparison"] = comparison.compare_periods(
                payload["date_from"], payload["date_to"], level="campaign"
            )
        except Exception as e:
            print(f"[analyze] enrichment failed (non-fatal): {e}")

    upload_ids = [u["id"] for u in db.list_uploads()]
    result = ai.analyze(upload_ids, snap)
    return result


@app.post("/api/chat")
def post_chat(payload: dict):
    msg = (payload or {}).get("message", "").strip()
    if not msg:
        raise HTTPException(400, "message required")
    snap = analytics.full_snapshot(
        date_from=payload.get("date_from"),
        date_to=payload.get("date_to"),
        campaign=payload.get("campaign"),
        adset=payload.get("adset"),
        ad=payload.get("ad"),
        region=payload.get("region"),
    )
    answer = ai.chat(msg, snapshot=snap)
    return {"answer": answer}


@app.post("/api/summary")
def post_summary(payload: Optional[dict] = None):
    """
    Generate an executive summary for a relative period. Designed to be hit
    by a scheduled task (cron / Cowork scheduled task).

    Body: {"period": "daily" | "weekly" | "monthly"}
    """
    from datetime import datetime, timedelta
    payload = payload or {}
    period = payload.get("period", "weekly")
    days = {"daily": 1, "weekly": 7, "monthly": 30}.get(period, 7)
    today = datetime.utcnow().date()
    date_to = today.strftime("%Y-%m-%d")
    date_from = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return post_analyze({"date_from": date_from, "date_to": date_to, "granularity": "daily"})


@app.get("/api/chat/history")
def get_chat_history():
    return {"messages": db.fetch_chat(200)}


@app.get("/api/recommendations")
def get_recommendations():
    return {"recommendations": db.latest_recommendations(50)}


@app.get("/api/analyses")
def get_analyses():
    return {"analyses": db.list_analyses(100)}


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: int):
    a = db.get_analysis(analysis_id)
    if not a:
        raise HTTPException(404, "not found")
    return a


@app.get("/api/memory")
def get_memory(limit: int = 200):
    return {"memory": db.fetch_memory(limit)}


# ---------- user notes ----------

@app.post("/api/notes")
def post_note(payload: dict):
    note = (payload or {}).get("note", "").strip()
    if not note:
        raise HTTPException(400, "note required")
    nid = db.insert_note(
        note,
        entity_level=payload.get("entity_level"),
        entity_name=payload.get("entity_name"),
    )
    return {"id": nid}


@app.get("/api/notes")
def get_notes():
    return {"notes": db.list_notes(200)}


# ---------- export ----------

@app.get("/api/export/csv")
def export_csv(date_from: Optional[str] = None, date_to: Optional[str] = None):
    snap = analytics.full_snapshot(date_from, date_to, None, None, None)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["entity_level", "entity", "spend", "conversations", "purchases",
                "revenue", "cac", "cost_per_conversation", "roas", "ctr", "clicks", "impressions"])
    for level_key, level_name in (("campaigns", "campaign"), ("adsets", "adset"), ("ads", "ad")):
        for r in snap.get(level_key, []):
            w.writerow([
                level_name, r.get("entity"),
                r.get("spend"), r.get("conversations"), r.get("purchases"),
                r.get("revenue"), r.get("cac"), r.get("cost_per_conversation"),
                r.get("roas"), r.get("ctr"), r.get("clicks"), r.get("impressions"),
            ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=meta-ads-summary.csv"},
    )


@app.get("/api/export/pdf")
def export_pdf(analysis_id: Optional[int] = None):
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError:
        raise HTTPException(500, "reportlab not installed")

    a = db.get_analysis(analysis_id) if analysis_id else None
    if not a:
        analyses = db.list_analyses(1)
        if not analyses:
            raise HTTPException(404, "no analyses available — run /api/analyze first")
        a = db.get_analysis(analyses[0]["id"])

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"Meta Ads AI Analysis #{a['id']}", styles["Title"]),
        Paragraph(f"Generated: {a['created_at']} &nbsp;|&nbsp; Range: {a.get('date_range','n/a')}", styles["Normal"]),
        Spacer(1, 12),
        Paragraph("<b>Executive Summary</b>", styles["Heading2"]),
        Paragraph(a.get("summary") or "(none)", styles["BodyText"]),
        Spacer(1, 12),
        Paragraph("<b>Commentary</b>", styles["Heading2"]),
        Paragraph((a.get("commentary") or "(none)").replace("\n", "<br/>"), styles["BodyText"]),
        Spacer(1, 12),
        Paragraph("<b>Recommendations</b>", styles["Heading2"]),
    ]
    for r in a.get("recommendations", []):
        story.append(Paragraph(
            f"<b>[{r['category']}] {r.get('entity_name','')}</b> — {r.get('headline','')}",
            styles["BodyText"],
        ))
        story.append(Paragraph(f"<i>Why:</i> {r.get('rationale','')}", styles["BodyText"]))
        story.append(Paragraph(f"<i>Action:</i> {r.get('suggested_action','')}", styles["BodyText"]))
        story.append(Spacer(1, 6))
    doc.build(story)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=analysis-{a['id']}.pdf"},
    )


# ---------- context backup / restore ----------

@app.get("/api/context/export")
def export_context():
    """Download the AI's full long-term memory (analyses + recs + memory + notes)."""
    payload = {
        "version": 1,
        "exported_at": db.now(),
        "uploads":         db.list_uploads(),
        "analyses":        [db.get_analysis(a["id"]) for a in db.list_analyses(500)],
        "memory":          db.fetch_memory(2000),
        "notes":           db.list_notes(2000),
        "recommendations": db.latest_recommendations(2000),
    }
    body = json.dumps(payload, indent=2, default=str)
    return StreamingResponse(
        iter([body]),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=ads-ai-context.json"},
    )


@app.post("/api/context/import")
async def import_context(file: UploadFile = File(...)):
    """
    Restore from a previous /context/export bundle. Memory + analyses +
    recommendations are appended (idempotent on content, not on IDs).
    """
    raw = await file.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"invalid json: {e}")

    imported = {"memory": 0, "notes": 0, "analyses": 0, "recommendations": 0}

    for m in data.get("memory", []):
        db.insert_memory(
            kind=m.get("kind", "insight"),
            content=m.get("content", ""),
            entity_level=m.get("entity_level"),
            entity_name=m.get("entity_name"),
        )
        imported["memory"] += 1

    for n in data.get("notes", []):
        db.insert_note(
            note=n.get("note", ""),
            entity_level=n.get("entity_level"),
            entity_name=n.get("entity_name"),
        )
        imported["notes"] += 1

    for a in data.get("analyses", []):
        # metrics_json comes back from get_analysis() as a JSON string; parse
        # before re-passing so insert_analysis doesn't double-encode it.
        metrics = a.get("metrics_json") or a.get("metrics") or {}
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except json.JSONDecodeError:
                metrics = {}
        new_id = db.insert_analysis(
            upload_ids=[],
            date_range=a.get("date_range", ""),
            summary=a.get("summary", ""),
            commentary=a.get("commentary", ""),
            metrics=metrics,
            model=a.get("model", "imported"),
        )
        for rec in a.get("recommendations", []) or []:
            db.insert_recommendations(new_id, [rec])
            imported["recommendations"] += 1
        imported["analyses"] += 1

    return {"imported": imported}


# ---------- health ----------

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "ai_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
        "last_ai_error": ai.LAST_ERROR,
    }


@app.get("/api/health/claude")
def health_claude():
    """Actually pings Anthropic to verify the key + model work."""
    return ai.ping()
