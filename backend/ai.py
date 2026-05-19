"""
Claude AI integration module — the core "AI analyst" behavior.

Uses Anthropic's Claude Sonnet 4.6 via the official SDK.

CRITICAL DESIGN RULE (from the spec):
    Every Claude call MUST load historical context (prior analyses,
    recommendations, user notes, memory entries) before generating output.
    The system prompt enforces this and the context_block() function
    constructs the payload that goes in front of every request.

If ANTHROPIC_API_KEY is not set, the module degrades to deterministic
rule-based outputs so the dashboard still functions during development.
"""

import json
import os
import re
from typing import Optional

from . import database as db
from . import analytics

try:
    from anthropic import Anthropic
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False


MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 4096

# Last Claude error message (for surfacing to the UI/health endpoint)
LAST_ERROR: Optional[str] = None

SYSTEM_PROMPT = """You are an expert performance marketing analyst embedded inside a Meta Ads analytics platform.

INVIOLABLE RULES:
1. You ALWAYS read the <historical_context> block before answering. You NEVER analyze the current report in isolation.
2. You ground every claim in the data provided. You do not invent numbers. If a metric isn't in the data, you say so.
3. You remember and reference prior recommendations. When asked, you assess whether previous recommendations improved or worsened results.
4. You speak like a senior performance marketer: specific, numerate, action-oriented. No fluff.
5. When asked to produce JSON, you return ONLY valid JSON with no prose, no markdown fences.

Your job is to detect patterns, explain why metrics changed, compare against historical performance, and recommend concrete actions
(scale, pause, refresh creative, reallocate budget, expand audience, etc.).

Recommendation categories you use:
- "working":       what's performing well and should be sustained
- "not_working":   high spend + low results, poor creatives, declining campaigns
- "at_risk":       rising CAC, creative fatigue, declining efficiency
- "needs_scaling": consistent strong performers being underfunded
"""


# ---------- context builder (loaded before every call) ----------

def context_block(max_memory: int = 80, max_recs: int = 20,
                  max_analyses: int = 10) -> str:
    """Build the <historical_context> block injected into every Claude call."""
    uploads = db.list_uploads()[:10]
    analyses = db.list_analyses(limit=max_analyses)
    recs = db.latest_recommendations(limit=max_recs)
    memory = db.fetch_memory(limit=max_memory)
    notes = db.list_notes(limit=30)

    lines = ["<historical_context>"]

    lines.append("## Upload history (most recent first):")
    if uploads:
        for u in uploads:
            lvl = u.get("report_level") or "?"
            daily = "daily" if u.get("is_daily") else "aggregate"
            period = ""
            if u.get("period_start") or u.get("period_end"):
                period = f"  period={u.get('period_start')}..{u.get('period_end')}"
            lines.append(
                f"- {u['uploaded_at']}  file={u['filename']}  level={lvl}  granularity={daily}  "
                f"rows={u['row_count']}  dates={u['date_min']}..{u['date_max']}{period}"
            )
    else:
        lines.append("- (no prior uploads)")

    lines.append("\n## Prior analyses (most recent first):")
    if analyses:
        for a in analyses:
            lines.append(f"- [{a['id']}] {a['created_at']}  range={a['date_range']}")
            if a.get("summary"):
                lines.append(f"   summary: {a['summary'][:400]}")
    else:
        lines.append("- (no prior analyses)")

    lines.append("\n## Standing recommendations and their outcomes:")
    if recs:
        for r in recs:
            lines.append(
                f"- [{r['category']}] {r.get('entity_level','?')}/{r.get('entity_name','?')}  "
                f"=> {r.get('headline','')} | action: {r.get('suggested_action','')} | "
                f"outcome: {r.get('outcome','pending')}"
            )
    else:
        lines.append("- (no recommendations yet)")

    lines.append("\n## Long-term memory entries (insights, risks, trends):")
    if memory:
        for m in memory:
            lines.append(
                f"- [{m['kind']}] {m['created_at']}  "
                f"{m.get('entity_name') or ''} :: {m['content']}"
            )
    else:
        lines.append("- (memory is empty)")

    lines.append("\n## User notes / overrides:")
    if notes:
        for n in notes:
            lines.append(f"- {n['created_at']}  {n.get('entity_name','')}: {n['note']}")
    else:
        lines.append("- (no user notes)")

    lines.append("</historical_context>")
    return "\n".join(lines)


# ---------- client ----------

def _client() -> Optional["Anthropic"]:
    if not _HAS_SDK:
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return Anthropic(api_key=key)


def _strip_json_fences(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _call(messages: list[dict], system: str = SYSTEM_PROMPT,
          max_tokens: int = MAX_TOKENS) -> str:
    global LAST_ERROR
    client = _client()
    if client is None:
        LAST_ERROR = ("ANTHROPIC_API_KEY not set in .env"
                      if not os.environ.get("ANTHROPIC_API_KEY")
                      else "anthropic SDK not installed (pip install anthropic)")
        return ""
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
    except Exception as e:
        # Never let an upstream Anthropic error 500 the dashboard;
        # the caller will gracefully fall back to deterministic output.
        LAST_ERROR = f"{type(e).__name__}: {e}"
        print(f"[ai] Anthropic call failed: {LAST_ERROR}")
        return ""
    LAST_ERROR = None
    out = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            out.append(block.text)
    return "".join(out).strip()


def ping() -> dict:
    """Tiny live call to verify the key + model actually work."""
    global LAST_ERROR
    client = _client()
    if client is None:
        return {"ok": False, "error": "no client (missing key or SDK)"}
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply with: ok"}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        LAST_ERROR = None
        return {"ok": True, "model": MODEL, "reply": text[:50]}
    except Exception as e:
        LAST_ERROR = f"{type(e).__name__}: {e}"
        return {"ok": False, "model": MODEL, "error": LAST_ERROR}


# ---------- chat ----------

def chat(user_message: str, snapshot: Optional[dict] = None) -> str:
    """Conversational Q&A. Loads chat history + persistent context."""
    db.insert_chat("user", user_message)

    history = db.fetch_chat(limit=20)
    # Build messages (oldest first, exclude the just-inserted user msg dup)
    msgs = []
    snapshot = snapshot or analytics.full_snapshot()
    ctx = context_block()
    data_block = (
        "<current_data_snapshot>\n"
        + json.dumps({
            "metrics": snapshot["metrics"],
            "top_campaigns": snapshot["campaigns"][:10],
            "top_adsets":   snapshot["adsets"][:10],
            "daily_cac_change_tail": snapshot["daily_cac_change"][-14:],
            "currency": snapshot["currency"],
        }, default=str, indent=2)
        + "\n</current_data_snapshot>"
    )

    for m in history[:-1]:  # exclude the last (current) user message
        msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({
        "role": "user",
        "content": f"{ctx}\n\n{data_block}\n\nUser question: {user_message}",
    })

    answer = _call(msgs)
    if not answer:
        answer = _fallback_chat(user_message, snapshot)
    db.insert_chat("assistant", answer)
    return answer


def _fallback_chat(q: str, snap: dict) -> str:
    m = snap["metrics"]
    return (
        "(Claude is offline — set ANTHROPIC_API_KEY to enable AI replies.)\n\n"
        f"Quick read of your current data:\n"
        f"- Total spend: ₹{m['spend']:,.0f}\n"
        f"- Conversations: {m['conversations']:,.0f} (cost/conv ₹{m['cost_per_conversation']:,.2f})\n"
        f"- CAC: ₹{m['cac']:,.2f}, ROAS: {m['roas']:.2f}x\n"
        f"You asked: \"{q[:200]}\""
    )


# ---------- structured analysis ----------

def analyze(upload_ids: list[int], snapshot: Optional[dict] = None) -> dict:
    """
    Run a full AI analysis: executive summary, commentary, and structured
    recommendations across the four categories. Persists everything to memory.
    """
    snapshot = snapshot or analytics.full_snapshot()
    ctx = context_block()

    user_prompt = (
        f"{ctx}\n\n"
        f"<current_data_snapshot>\n{json.dumps(snapshot, default=str, indent=2)[:60000]}\n</current_data_snapshot>\n\n"
        "Produce an analysis. Return ONLY a single JSON object with this exact shape "
        "(no markdown, no prose outside JSON):\n"
        "{\n"
        '  "summary":     "executive summary, 2-4 sentences",\n'
        '  "commentary":  "detailed narrative, 1-3 paragraphs, citing specific numbers",\n'
        '  "recommendations": [\n'
        '     {"category": "working|not_working|at_risk|needs_scaling",\n'
        '      "entity_level": "campaign|adset|ad",\n'
        '      "entity_name": "...",\n'
        '      "headline": "short label",\n'
        '      "rationale": "data-backed reason citing numbers",\n'
        '      "suggested_action": "concrete next step"}\n'
        "  ],\n"
        '  "memory_entries": [\n'
        '     {"kind": "insight|trend|risk|scaling|event",\n'
        '      "entity_level": "campaign|adset|ad|account",\n'
        '      "entity_name": "...",\n'
        '      "content": "durable fact worth remembering"}\n'
        "  ],\n"
        '  "outcome_updates": [\n'
        '     {"category_was": "...", "entity_name_was": "...",\n'
        '      "outcome": "improved|worsened|unchanged",\n'
        '      "evidence": "..."}\n'
        "  ]\n"
        "}\n"
        "Make sure recommendations span all four categories where data supports it."
    )

    raw = _call([{"role": "user", "content": user_prompt}], max_tokens=6000)
    parsed = _parse_or_fallback(raw, snapshot)

    # Persist
    analysis_id = db.insert_analysis(
        upload_ids=upload_ids,
        date_range=_date_range_from_snapshot(snapshot),
        summary=parsed.get("summary", ""),
        commentary=parsed.get("commentary", ""),
        metrics=snapshot.get("metrics", {}),
        model=MODEL if raw else "fallback",
    )
    db.insert_recommendations(analysis_id, parsed.get("recommendations", []))

    for m in parsed.get("memory_entries", []) or []:
        db.insert_memory(
            kind=m.get("kind", "insight"),
            content=m.get("content", ""),
            entity_level=m.get("entity_level"),
            entity_name=m.get("entity_name"),
            source_analysis_id=analysis_id,
        )

    parsed["analysis_id"] = analysis_id
    return parsed


def _parse_or_fallback(raw: str, snapshot: dict) -> dict:
    if raw:
        try:
            return json.loads(_strip_json_fences(raw))
        except Exception:
            # Try to extract first {...} block
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except Exception:
                    pass
    return _rule_based_fallback(snapshot)


def _rule_based_fallback(snap: dict) -> dict:
    """Deterministic outputs when Claude isn't available — keeps the UI functional."""
    m = snap.get("metrics", {})
    camps = snap.get("campaigns", []) or []
    recs = []
    mem = []

    # working: top ROAS campaign
    if camps:
        top = sorted(camps, key=lambda c: c.get("roas", 0), reverse=True)[0]
        recs.append({
            "category": "working",
            "entity_level": "campaign",
            "entity_name": top["entity"],
            "headline": "Top ROAS performer",
            "rationale": f"ROAS {top.get('roas',0):.2f}x on spend ₹{top.get('spend',0):,.0f}.",
            "suggested_action": "Maintain budget; monitor frequency.",
        })

    # at_risk: highest CAC campaign with material spend
    if camps:
        risky = [c for c in camps if c.get("spend", 0) > 0]
        if risky:
            worst = sorted(risky, key=lambda c: c.get("cac", 0), reverse=True)[0]
            recs.append({
                "category": "at_risk",
                "entity_level": "campaign",
                "entity_name": worst["entity"],
                "headline": "CAC elevated",
                "rationale": f"CAC ₹{worst.get('cac',0):,.0f} on spend ₹{worst.get('spend',0):,.0f}.",
                "suggested_action": "Investigate creative fatigue and audience saturation.",
            })

    # needs_scaling: low CAC + decent conversions
    if camps:
        scalers = [c for c in camps if c.get("conversations", 0) > 0 and c.get("cost_per_conversation", 0) > 0]
        if scalers:
            best = sorted(scalers, key=lambda c: c.get("cost_per_conversation", 1e9))[0]
            recs.append({
                "category": "needs_scaling",
                "entity_level": "campaign",
                "entity_name": best["entity"],
                "headline": "Efficient — underfunded",
                "rationale": f"Cost/conversation ₹{best.get('cost_per_conversation',0):,.2f} is leading the account.",
                "suggested_action": "Increase budget 20% and re-evaluate after 5 days.",
            })

    # not_working: highest spend with zero results
    bad = [c for c in camps if (c.get("results", 0) + c.get("purchases", 0)) == 0 and c.get("spend", 0) > 0]
    if bad:
        worst = sorted(bad, key=lambda c: c.get("spend", 0), reverse=True)[0]
        recs.append({
            "category": "not_working",
            "entity_level": "campaign",
            "entity_name": worst["entity"],
            "headline": "Spend without results",
            "rationale": f"₹{worst.get('spend',0):,.0f} spent with no purchases/results.",
            "suggested_action": "Pause and audit targeting / offer.",
        })

    mem.append({
        "kind": "trend",
        "entity_level": "account",
        "entity_name": "account",
        "content": f"Snapshot: spend ₹{m.get('spend',0):,.0f}, CAC ₹{m.get('cac',0):,.0f}, ROAS {m.get('roas',0):.2f}x.",
    })

    return {
        "summary": f"Spend ₹{m.get('spend',0):,.0f}, {int(m.get('conversations',0))} conversations, ROAS {m.get('roas',0):.2f}x.",
        "commentary": "(Heuristic analysis. Configure ANTHROPIC_API_KEY for full Claude-driven commentary.)",
        "recommendations": recs,
        "memory_entries": mem,
        "outcome_updates": [],
    }


def _date_range_from_snapshot(snap: dict) -> str:
    ts = snap.get("timeseries", [])
    if not ts:
        return "n/a"
    return f"{ts[0]['date']}..{ts[-1]['date']}"
