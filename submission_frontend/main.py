"""Manager dashboard — expense approval workflow front-end."""
from __future__ import annotations

import asyncio
import json
import logging
import os

import vertexai
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from google.adk.sessions import VertexAiSessionService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "mcp-test-487013")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-east1")
AGENT_RUNTIME_ID = os.environ.get(
    "AGENT_RUNTIME_ID",
    "projects/1064204015476/locations/us-east1/reasoningEngines/5773720275405242368",
)

vertexai.init(project=PROJECT_ID, location=LOCATION)
session_service = VertexAiSessionService(project=PROJECT_ID, location=LOCATION)

app = FastAPI(title="Expense Approval Dashboard")


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_list(result, attr: str = "sessions") -> list:
    """Coerce SDK list-result shapes into a plain Python list."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    val = getattr(result, attr, None)
    if val is not None:
        return list(val)
    try:
        return list(result)
    except Exception:
        return []


def _find_pending(events: list) -> list[dict]:
    """Return unresolved adk_request_input function-call entries."""
    resolved: set[str] = set()
    for ev in events:
        if not (ev.content and ev.content.parts):
            continue
        for p in ev.content.parts:
            fr = getattr(p, "function_response", None)
            if fr and getattr(fr, "name", None) == "adk_request_input":
                resolved.add(fr.id)

    calls: list[dict] = []
    for ev in events:
        if not (ev.content and ev.content.parts):
            continue
        for p in ev.content.parts:
            fc = getattr(p, "function_call", None)
            if (
                fc
                and getattr(fc, "name", None) == "adk_request_input"
                and fc.id not in resolved
            ):
                calls.append({"interrupt_id": fc.id, "args": dict(fc.args or {})})
    return calls


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/api/pending")
async def api_pending():
    """Query VertexAiSessionService, return every unresolved approval request."""
    # list_sessions / get_session are async coroutines — await directly, no to_thread
    try:
        raw = await session_service.list_sessions(app_name=AGENT_RUNTIME_ID)
    except Exception as exc:
        logger.exception("list_sessions failed")
        raise HTTPException(status_code=500, detail=str(exc))

    sessions = _to_list(raw, "sessions")
    items: list[dict] = []

    for sess in sessions:
        sid = getattr(sess, "id", None) or getattr(sess, "name", "")
        # Use the actual user_id from the session (agents-cli sets this to 'cli-user')
        uid = getattr(sess, "user_id", None) or "cli-user"
        if not sid:
            continue

        try:
            full = await session_service.get_session(
                app_name=AGENT_RUNTIME_ID,
                user_id=uid,
                session_id=sid,
            )
        except Exception:
            logger.warning("Skipping session %s — get_session failed", sid)
            continue

        events = list(getattr(full, "events", []) or [])

        calls = _find_pending(events)
        if not calls:
            continue

        state = dict(getattr(full, "state", {}) or {})
        expense = state.get("expense", {})
        risk = state.get("risk_assessment", {})

        for call in calls:
            items.append({
                "session_id": sid,
                "user_id": uid,
                "interrupt_id": call["interrupt_id"],
                "expense": expense,
                "risk_assessment": risk,
            })

    return {"pending": items}


@app.post("/api/action/{session_id}")
async def api_action(session_id: str, request: Request):
    """Resume a paused Agent Runtime session with an approve/reject decision."""
    body = await request.json()
    decision = body.get("decision", "reject")
    interrupt_id = body.get("interrupt_id")
    # Use the user_id that owns the session (passed from /api/pending response)
    user_id = body.get("user_id", "cli-user")

    if not interrupt_id:
        raise HTTPException(status_code=400, detail="interrupt_id is required")

    # Pass the resume payload directly as the dict value of `message`
    message = {
        "role": "user",
        "parts": [{
            "function_response": {
                "id": interrupt_id,
                "name": "adk_request_input",
                "response": {"approved": decision == "approve"},
            }
        }],
    }

    try:
        from vertexai import agent_engines

        def _stream() -> list[str]:
            engine = agent_engines.get(AGENT_RUNTIME_ID)
            parts: list[str] = []
            for event in engine.stream_query(
                user_id=user_id,
                session_id=session_id,
                message=message,
            ):
                if getattr(event, "content", None) and event.content.parts:
                    for p in event.content.parts:
                        t = getattr(p, "text", None)
                        if t:
                            parts.append(t)
            return parts

        text_parts = await asyncio.to_thread(_stream)

        return {
            "status": "ok",
            "decision": decision,
            "response": "\n".join(text_parts) or f"Expense {decision}d.",
        }

    except Exception as exc:
        logger.exception("Failed to resume session %s", session_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/submit")
async def api_submit(request: Request):
    """Send a new expense JSON to the Agent Runtime and start a session."""
    body = await request.json()
    for field in ("amount", "submitter", "category", "description", "date"):
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing field: {field}")

    expense_json = json.dumps(body)

    def _stream():
        from vertexai import agent_engines
        engine = agent_engines.get(AGENT_RUNTIME_ID)
        parts: list[str] = []
        for event in engine.stream_query(user_id="cli-user", message=expense_json):
            if getattr(event, "content", None) and event.content.parts:
                for p in event.content.parts:
                    t = getattr(p, "text", None)
                    if t:
                        parts.append(t)
        return parts

    try:
        text_parts = await asyncio.to_thread(_stream)
        response_text = "\n".join(text_parts)
        status = "auto_approved" if response_text else "pending_review"
        return {"status": status, "response": response_text}
    except Exception as exc:
        logger.exception("Failed to submit expense")
        raise HTTPException(status_code=500, detail=str(exc))


# ── embedded dashboard UI ─────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Expense Approval Hub</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:              #07070e;
      --surface:         rgba(255,255,255,0.04);
      --surface-hi:      rgba(255,255,255,0.07);
      --border:          rgba(255,255,255,0.08);
      --border-hi:       rgba(255,255,255,0.16);
      --text:            #f0f0f8;
      --muted:           rgba(240,240,248,0.52);
      --dim:             rgba(240,240,248,0.28);
      --purple:          #7c3aed;
      --blue:            #3b82f6;
      --teal:            #14b8a6;
      --green:           #10b981;
      --yellow:          #f59e0b;
      --red:             #ef4444;
      --r:               16px;
      --r-sm:            8px;
    }

    body {
      font-family: 'Inter', sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      overflow-x: hidden;
    }

    /* Ambient background glows */
    body::before {
      content: '';
      position: fixed; inset: 0; z-index: 0; pointer-events: none;
      background:
        radial-gradient(ellipse 900px 700px at 10% 5%,  rgba(124,58,237,.13) 0%, transparent 70%),
        radial-gradient(ellipse 700px 900px at 90% 90%, rgba(20,184,166,.09) 0%, transparent 70%),
        radial-gradient(ellipse 1100px 500px at 50% 50%, rgba(59,130,246,.05) 0%, transparent 70%);
    }

    .wrap { position: relative; z-index: 1; max-width: 1200px; margin: 0 auto; padding: 0 24px; }

    /* ── Header ── */
    header {
      border-bottom: 1px solid var(--border);
      backdrop-filter: blur(24px);
      background: rgba(7,7,14,.85);
      position: sticky; top: 0; z-index: 100;
    }
    .hdr {
      display: flex; align-items: center; justify-content: space-between;
      padding: 18px 0;
    }
    .logo { display: flex; align-items: center; gap: 12px; }
    .logo-icon {
      width: 40px; height: 40px; border-radius: 10px;
      background: linear-gradient(135deg, var(--purple), var(--blue));
      display: flex; align-items: center; justify-content: center; font-size: 18px;
    }
    .logo-name { font-size: 17px; font-weight: 700; letter-spacing: -.3px; }
    .logo-sub  { font-size: 11px; color: var(--muted); margin-top: 1px; }

    .hdr-right { display: flex; align-items: center; gap: 12px; }

    .live-pill {
      display: flex; align-items: center; gap: 6px;
      background: rgba(16,185,129,.1); border: 1px solid rgba(16,185,129,.22);
      border-radius: 20px; padding: 5px 12px;
      font-size: 11px; font-weight: 600; color: var(--green); letter-spacing: .4px;
    }
    .live-dot {
      width: 6px; height: 6px; border-radius: 50%; background: var(--green);
      animation: blink 2s ease-in-out infinite;
    }
    @keyframes blink { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.4;transform:scale(.75)} }

    .btn-ghost {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--r-sm);
      color: var(--muted); padding: 7px 14px; font-size: 12px; font-family: inherit;
      cursor: pointer; transition: all .2s; display: flex; align-items: center; gap: 6px;
    }
    .btn-ghost:hover { background: var(--surface-hi); color: var(--text); border-color: var(--border-hi); }

    /* ── Stats ── */
    .stats { display: grid; grid-template-columns: repeat(3,1fr); gap: 16px; padding: 28px 0 0; }

    .stat-card {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--r);
      padding: 20px 22px; backdrop-filter: blur(20px); transition: all .2s;
    }
    .stat-card:hover { background: var(--surface-hi); border-color: var(--border-hi); }
    .stat-label { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .6px; margin-bottom: 8px; }
    .stat-val   { font-size: 30px; font-weight: 800; letter-spacing: -1px; }
    .stat-hint  { font-size: 11px; color: var(--dim); margin-top: 4px; }

    /* ── Section header ── */
    .sec-hdr {
      display: flex; align-items: center; justify-content: space-between;
      margin: 28px 0 18px;
    }
    .sec-title { font-size: 15px; font-weight: 600; }
    .badge-count {
      background: rgba(124,58,237,.15); border: 1px solid rgba(124,58,237,.28);
      border-radius: 20px; padding: 3px 10px; font-size: 11px; font-weight: 700; color: #a78bfa;
    }

    /* ── Cards grid ── */
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
      gap: 20px; padding-bottom: 60px;
    }

    /* ── Expense card ── */
    .card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--r); backdrop-filter: blur(22px);
      overflow: hidden; transition: transform .3s, box-shadow .3s, border-color .3s, opacity .4s;
      animation: cardIn .4s ease both;
    }
    @keyframes cardIn { from{opacity:0;transform:translateY(14px)} to{opacity:1;transform:translateY(0)} }
    .card:hover { border-color: var(--border-hi); transform: translateY(-3px); box-shadow: 0 24px 64px rgba(0,0,0,.5); }
    .card.resolved { opacity: .35; pointer-events: none; transform: scale(.97); }

    .card-top {
      padding: 18px 20px 14px;
      border-bottom: 1px solid var(--border);
    }
    .card-meta { display: flex; justify-content: space-between; align-items: flex-start; }

    .sub-row { display: flex; align-items: center; gap: 10px; }
    .avatar {
      width: 36px; height: 36px; border-radius: 50%; flex-shrink: 0;
      background: linear-gradient(135deg, var(--purple), var(--blue));
      display: flex; align-items: center; justify-content: center;
      font-size: 12px; font-weight: 800; color: #fff;
    }
    .sub-name { font-size: 14px; font-weight: 600; }
    .sub-date { font-size: 11px; color: var(--muted); margin-top: 1px; }

    .amt { text-align: right; }
    .amt-val { font-size: 22px; font-weight: 800; letter-spacing: -.5px; }
    .amt-lbl { font-size: 10px; color: var(--dim); text-transform: uppercase; letter-spacing: .5px; margin-top: 1px; }

    .card-body { padding: 14px 20px; }

    .cat-badge {
      display: inline-flex; align-items: center;
      background: rgba(59,130,246,.1); border: 1px solid rgba(59,130,246,.2);
      border-radius: 6px; padding: 3px 9px;
      font-size: 10px; font-weight: 700; color: #93c5fd;
      text-transform: uppercase; letter-spacing: .6px; margin-bottom: 10px;
    }

    .desc {
      font-size: 13px; color: var(--muted); line-height: 1.55; margin-bottom: 12px;
      display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    }

    .risk {
      border-radius: var(--r-sm); padding: 9px 12px;
    }
    .risk.low    { background: rgba(16,185,129,.08);  border: 1px solid rgba(16,185,129,.18); }
    .risk.medium { background: rgba(245,158,11,.08);  border: 1px solid rgba(245,158,11,.18); }
    .risk.high   { background: rgba(239,68,68,.08);   border: 1px solid rgba(239,68,68,.18);  }
    .risk.none   { background: var(--surface);        border: 1px solid var(--border);        }

    .risk-hdr { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; }
    .risk-dot { width: 6px; height: 6px; border-radius: 50%; }
    .risk.low    .risk-dot { background: var(--green);  }
    .risk.medium .risk-dot { background: var(--yellow); }
    .risk.high   .risk-dot { background: var(--red);    }
    .risk.none   .risk-dot { background: var(--dim);    }
    .risk-lbl { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .8px; }
    .risk.low    .risk-lbl { color: var(--green);  }
    .risk.medium .risk-lbl { color: var(--yellow); }
    .risk.high   .risk-lbl { color: var(--red);    }
    .risk.none   .risk-lbl { color: var(--dim);    }
    .risk-sum { font-size: 11px; color: var(--muted); line-height: 1.45; }

    /* ── Action buttons ── */
    .card-actions { padding: 12px 20px 18px; display: flex; gap: 10px; }

    .btn-action {
      flex: 1; padding: 10px 0; border-radius: var(--r-sm); border: none;
      font-family: inherit; font-size: 13px; font-weight: 600;
      cursor: pointer; transition: all .2s;
      display: flex; align-items: center; justify-content: center; gap: 6px;
    }
    .btn-action:disabled { opacity: .5; cursor: not-allowed; }

    .btn-reject {
      background: rgba(239,68,68,.1); border: 1px solid rgba(239,68,68,.22); color: #fca5a5;
    }
    .btn-reject:hover:not(:disabled) {
      background: rgba(239,68,68,.2); border-color: rgba(239,68,68,.4);
      box-shadow: 0 0 20px rgba(239,68,68,.18);
    }
    .btn-approve {
      background: linear-gradient(135deg, #059669, #10b981); color: #fff;
      border: 1px solid transparent; box-shadow: 0 4px 16px rgba(16,185,129,.22);
    }
    .btn-approve:hover:not(:disabled) {
      box-shadow: 0 6px 28px rgba(16,185,129,.42); transform: translateY(-1px);
    }

    /* ── Spinner ── */
    .spin {
      width: 13px; height: 13px; border-radius: 50%;
      border: 2px solid rgba(255,255,255,.3); border-top-color: #fff;
      animation: rot .7s linear infinite;
    }
    @keyframes rot { to{transform:rotate(360deg)} }

    /* ── States ── */
    .state-wrap { grid-column: 1/-1; display: flex; flex-direction: column; align-items: center; padding: 80px 0; gap: 14px; }
    .state-icon { font-size: 44px; animation: float 3s ease-in-out infinite; }
    @keyframes float { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-10px)} }
    .state-title { font-size: 17px; font-weight: 600; }
    .state-sub   { font-size: 13px; color: var(--muted); }

    .loading-ring {
      width: 22px; height: 22px; border-radius: 50%;
      border: 2px solid var(--border); border-top-color: var(--purple);
      animation: rot .8s linear infinite;
    }

    .err-wrap {
      grid-column: 1/-1;
      background: rgba(239,68,68,.06); border: 1px solid rgba(239,68,68,.18);
      border-radius: var(--r); padding: 24px; text-align: center;
      font-size: 13px; color: #fca5a5;
    }

    /* ── Modal ── */
    .overlay {
      position: fixed; inset: 0; z-index: 200;
      background: rgba(7,7,14,.75); backdrop-filter: blur(10px);
      opacity: 0; pointer-events: none; transition: opacity .3s;
    }
    .overlay.open { opacity: 1; pointer-events: all; }

    .panel {
      position: fixed; top: 0; right: 0; bottom: 0; z-index: 201;
      width: 480px; max-width: 100vw;
      background: #0c0c18; border-left: 1px solid var(--border);
      padding: 32px; overflow-y: auto;
      transform: translateX(100%);
      transition: transform .42s cubic-bezier(.16,1,.3,1);
    }
    .overlay.open .panel { transform: translateX(0); }

    .panel-hdr { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 28px; }
    .panel-icon  { font-size: 34px; margin-bottom: 8px; }
    .panel-title { font-size: 20px; font-weight: 700; margin-bottom: 4px; }
    .panel-sub   { font-size: 12px; color: var(--muted); }
    .panel-close {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--r-sm); width: 32px; height: 32px;
      display: flex; align-items: center; justify-content: center;
      cursor: pointer; color: var(--muted); font-size: 14px;
      transition: all .2s; flex-shrink: 0;
    }
    .panel-close:hover { background: var(--surface-hi); color: var(--text); }

    .p-sec { margin-bottom: 22px; }
    .p-sec-title {
      font-size: 10px; font-weight: 700; text-transform: uppercase;
      letter-spacing: 1px; color: var(--dim);
      padding-bottom: 8px; margin-bottom: 10px;
      border-bottom: 1px solid var(--border);
    }
    .p-row {
      display: flex; justify-content: space-between; align-items: flex-start;
      padding: 5px 0; font-size: 13px;
    }
    .p-lbl { color: var(--muted); flex-shrink: 0; margin-right: 16px; }
    .p-val { color: var(--text); font-weight: 500; text-align: right; word-break: break-word; }

    .dec-badge {
      display: inline-flex; align-items: center; gap: 5px;
      padding: 5px 14px; border-radius: 20px; font-size: 12px; font-weight: 700;
    }
    .dec-badge.approved { background: rgba(16,185,129,.15); color: var(--green); border: 1px solid rgba(16,185,129,.25); }
    .dec-badge.rejected { background: rgba(239,68,68,.15);  color: var(--red);   border: 1px solid rgba(239,68,68,.25);  }

    .raw {
      background: rgba(0,0,0,.35); border: 1px solid var(--border); border-radius: var(--r-sm);
      padding: 14px; font-family: 'SF Mono','Fira Code',monospace;
      font-size: 11px; color: var(--muted); white-space: pre-wrap; word-break: break-all;
      max-height: 220px; overflow-y: auto; line-height: 1.65;
    }

    .btn-close-panel {
      width: 100%; padding: 11px; margin-top: 20px;
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--r-sm);
      font-family: inherit; font-size: 13px; font-weight: 500; color: var(--muted);
      cursor: pointer; transition: all .2s;
    }
    .btn-close-panel:hover { background: var(--surface-hi); color: var(--text); }

    /* ── Toast ── */
    .toast {
      position: fixed; bottom: 24px; left: 50%;
      transform: translateX(-50%) translateY(80px);
      background: rgba(15,15,25,.95); border: 1px solid var(--border-hi);
      backdrop-filter: blur(20px); border-radius: 10px;
      padding: 11px 20px; font-size: 13px; font-weight: 500;
      z-index: 400; transition: transform .35s cubic-bezier(.16,1,.3,1);
      white-space: nowrap;
    }
    .toast.show { transform: translateX(-50%) translateY(0); }

    @media (max-width: 640px) {
      .stats { grid-template-columns: 1fr; }
      .panel { width: 100%; border-left: none; border-top: 1px solid var(--border); top: auto; height: 80vh; }
    }

    /* ── Tabs ── */
    .tab-nav { display: flex; gap: 2px; padding: 24px 0 0; border-bottom: 1px solid var(--border); margin-bottom: 24px; }
    .tab-btn {
      background: transparent; border: none; border-bottom: 2px solid transparent;
      margin-bottom: -1px; color: var(--muted); font-family: inherit;
      font-size: 14px; font-weight: 500; padding: 10px 18px;
      cursor: pointer; transition: all .2s; display: flex; align-items: center; gap: 7px;
    }
    .tab-btn.active { color: var(--text); border-bottom-color: var(--purple); }
    .tab-btn:hover:not(.active) { color: var(--text); }
    .tab-pip {
      background: var(--purple); color: #fff; border-radius: 10px;
      font-size: 10px; font-weight: 700; padding: 1px 7px; min-width: 18px; text-align: center;
    }
    .tab-content { display: none; }
    .tab-content.active { display: block; }

    /* ── Submit form ── */
    .form-wrap { max-width: 680px; }
    .form-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--r); backdrop-filter: blur(22px); padding: 28px;
      margin-bottom: 16px;
    }
    .form-card-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .6px; margin-bottom: 20px; }
    .form-grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .form-row { margin-bottom: 16px; }
    .form-row:last-child { margin-bottom: 0; }
    .f-label { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; display: block; }
    .f-input {
      width: 100%; background: rgba(0,0,0,.35); border: 1px solid var(--border);
      border-radius: var(--r-sm); color: var(--text); font-family: inherit;
      font-size: 14px; padding: 10px 14px; outline: none; transition: border-color .2s;
      -webkit-appearance: none; appearance: none;
    }
    .f-input::placeholder { color: var(--dim); }
    .f-input:focus { border-color: rgba(124,58,237,.6); box-shadow: 0 0 0 3px rgba(124,58,237,.1); }
    select.f-input { cursor: pointer; }
    textarea.f-input { resize: vertical; min-height: 80px; }

    .json-preview-block {
      background: rgba(0,0,0,.4); border: 1px solid var(--border); border-radius: var(--r-sm);
      padding: 14px; font-family: 'SF Mono','Fira Code',monospace;
      font-size: 12px; color: var(--muted); white-space: pre; line-height: 1.7;
      overflow-x: auto;
    }

    .btn-submit {
      width: 100%; padding: 13px; border: none; border-radius: var(--r-sm);
      background: linear-gradient(135deg, var(--purple), var(--blue));
      color: #fff; font-family: inherit; font-size: 14px; font-weight: 600;
      cursor: pointer; transition: all .2s;
      display: flex; align-items: center; justify-content: center; gap: 8px;
      box-shadow: 0 4px 20px rgba(124,58,237,.25);
    }
    .btn-submit:hover:not(:disabled) { box-shadow: 0 6px 30px rgba(124,58,237,.45); transform: translateY(-1px); }
    .btn-submit:disabled { opacity: .5; cursor: not-allowed; }

    .submit-result {
      border-radius: var(--r); padding: 20px 24px;
      display: flex; align-items: flex-start; gap: 14px;
      animation: cardIn .3s ease both;
      margin-bottom: 16px;
    }
    .submit-result.auto { background: rgba(16,185,129,.08); border: 1px solid rgba(16,185,129,.2); }
    .submit-result.review { background: rgba(245,158,11,.08); border: 1px solid rgba(245,158,11,.2); }
    .submit-result.error { background: rgba(239,68,68,.08); border: 1px solid rgba(239,68,68,.2); }
    .result-icon { font-size: 24px; flex-shrink: 0; margin-top: 2px; }
    .result-title { font-size: 15px; font-weight: 700; margin-bottom: 4px; }
    .result-sub   { font-size: 13px; color: var(--muted); line-height: 1.5; }
    .result-raw   { margin-top: 10px; font-family: monospace; font-size: 11px; color: var(--dim); white-space: pre-wrap; }
  </style>
</head>
<body>

<header>
  <div class="wrap">
    <div class="hdr">
      <div class="logo">
        <div class="logo-icon">💼</div>
        <div>
          <div class="logo-name">Expense Approval Hub</div>
          <div class="logo-sub">Ambient AI &nbsp;·&nbsp; Agent Runtime</div>
        </div>
      </div>
      <div class="hdr-right">
        <div class="live-pill"><div class="live-dot"></div>LIVE</div>
        <button class="btn-ghost" onclick="doRefresh()">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
            <path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/>
            <path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/>
          </svg>
          Refresh
        </button>
      </div>
    </div>
  </div>
</header>

<div class="wrap">
  <div class="stats">
    <div class="stat-card">
      <div class="stat-label">Pending Approvals</div>
      <div class="stat-val" id="s-pending">—</div>
      <div class="stat-hint">Awaiting your decision</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">High Risk</div>
      <div class="stat-val" id="s-high" style="color:var(--red)">—</div>
      <div class="stat-hint">Flagged by AI reviewer</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total Exposure</div>
      <div class="stat-val" id="s-amt">—</div>
      <div class="stat-hint">Pending dollar value</div>
    </div>
  </div>

  <!-- Tab nav -->
  <div class="tab-nav">
    <button class="tab-btn active" id="tab-btn-pending" onclick="switchTab('pending')">
      Pending Approvals <span class="tab-pip" id="cnt">0</span>
    </button>
    <button class="tab-btn" id="tab-btn-submit" onclick="switchTab('submit')">
      Submit Expense
    </button>
  </div>

  <!-- Tab: Pending Approvals -->
  <div class="tab-content active" id="tab-pending">
    <div class="grid" id="grid">
      <div class="state-wrap">
        <div class="loading-ring"></div>
        <div class="state-sub">Loading approvals…</div>
      </div>
    </div>
  </div>

  <!-- Tab: Submit Expense -->
  <div class="tab-content" id="tab-submit">
    <div class="form-wrap">
      <div id="submit-result-area"></div>

      <div class="form-card">
        <div class="form-card-title">Expense Details</div>
        <div class="form-grid-2">
          <div class="form-row">
            <label class="f-label" for="f-submitter">Submitter</label>
            <input id="f-submitter" class="f-input" type="text" placeholder="Full name" oninput="updatePreview()" />
          </div>
          <div class="form-row">
            <label class="f-label" for="f-amount">Amount (USD)</label>
            <input id="f-amount" class="f-input" type="number" step="0.01" min="0" placeholder="0.00" oninput="updatePreview()" />
          </div>
          <div class="form-row">
            <label class="f-label" for="f-category">Category</label>
            <select id="f-category" class="f-input" onchange="updatePreview()">
              <option value="">Select…</option>
              <option value="meals">Meals</option>
              <option value="travel">Travel</option>
              <option value="equipment">Equipment</option>
              <option value="entertainment">Entertainment</option>
              <option value="software">Software</option>
              <option value="other">Other</option>
            </select>
          </div>
          <div class="form-row">
            <label class="f-label" for="f-date">Date</label>
            <input id="f-date" class="f-input" type="date" oninput="updatePreview()" />
          </div>
        </div>
        <div class="form-row" style="margin-top:4px">
          <label class="f-label" for="f-desc">Description</label>
          <textarea id="f-desc" class="f-input form-textarea" placeholder="Brief description of the expense…" oninput="updatePreview()"></textarea>
        </div>
      </div>

      <div class="form-card">
        <div class="form-card-title">JSON Preview</div>
        <div class="json-preview-block" id="json-preview">{}</div>
      </div>

      <button class="btn-submit" id="submit-btn" onclick="submitExpense()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
          <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
        </svg>
        Submit to Agent Runtime
      </button>
    </div>
  </div>
</div>

<!-- Slide-out detail panel -->
<div class="overlay" id="overlay" onclick="overlayClick(event)">
  <div class="panel" id="panel">
    <div class="panel-hdr">
      <div>
        <div class="panel-icon" id="p-icon">✅</div>
        <div class="panel-title" id="p-title">Decision Recorded</div>
        <div class="panel-sub" id="p-sub">Human reviewer</div>
      </div>
      <button class="panel-close" onclick="closePanel()">✕</button>
    </div>
    <div id="p-body"></div>
    <button class="btn-close-panel" onclick="closePanel()">Close</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
  let pending = [];
  let autoTimer;

  // ── tab switching ────────────────────────────────────────
  function switchTab(name) {
    ['pending','submit'].forEach(t => {
      document.getElementById('tab-' + t).classList.toggle('active', t === name);
      document.getElementById('tab-btn-' + t).classList.toggle('active', t === name);
    });
  }

  // ── submit form ──────────────────────────────────────────
  function buildPayload() {
    return {
      submitter:   document.getElementById('f-submitter').value.trim(),
      amount:      parseFloat(document.getElementById('f-amount').value) || 0,
      category:    document.getElementById('f-category').value,
      description: document.getElementById('f-desc').value.trim(),
      date:        document.getElementById('f-date').value,
    };
  }

  function updatePreview() {
    const p = buildPayload();
    document.getElementById('json-preview').textContent = JSON.stringify(p, null, 2);
  }

  async function submitExpense() {
    const payload = buildPayload();
    const missing = ['submitter','category','description','date'].filter(k => !payload[k]);
    if (!payload.amount) missing.push('amount');
    if (missing.length) {
      showSubmitResult('error', '⚠ Missing fields', 'Please fill in: ' + missing.join(', '));
      return;
    }

    const btn = document.getElementById('submit-btn');
    btn.disabled = true;
    btn.innerHTML = '<div class="spin"></div> Submitting…';
    document.getElementById('submit-result-area').innerHTML = '';

    try {
      const res  = await fetch('/api/submit', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'HTTP ' + res.status);

      if (data.status === 'auto_approved') {
        showSubmitResult('auto', '✅ Auto-Approved',
          'Amount is under the $100 threshold — no review required.',
          data.response);
      } else {
        showSubmitResult('review', '⏳ Sent for Review',
          'The expense exceeds the threshold and is queued for human approval. Check the Pending Approvals tab.',
          '');
        // Refresh pending list after a short delay so the card appears
        setTimeout(() => fetchPending(), 2000);
      }
      clearForm();
    } catch(e) {
      showSubmitResult('error', '⚠ Submission Failed', e.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Submit to Agent Runtime';
    }
  }

  function showSubmitResult(type, title, sub, raw) {
    document.getElementById('submit-result-area').innerHTML = `
      <div class="submit-result ${type === 'auto_approved' ? 'auto' : type}">
        <div class="result-icon">${type==='auto'?'✅':type==='review'?'⏳':'❌'}</div>
        <div>
          <div class="result-title">${title}</div>
          <div class="result-sub">${sub}</div>
          ${raw ? '<div class="result-raw">' + raw + '</div>' : ''}
        </div>
      </div>`;
  }

  function clearForm() {
    ['f-submitter','f-amount','f-desc','f-date'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('f-category').value = '';
    updatePreview();
  }

  // ── utils ────────────────────────────────────────────────
  const fmt = (n) =>
    new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(n||0);

  const initials = (name) =>
    (name||'?').split(' ').map(w=>w[0]).join('').toUpperCase().slice(0,2);

  const riskClass = (lvl) => {
    const l=(lvl||'').toLowerCase();
    return l==='high'?'high':l==='medium'?'medium':l==='low'?'low':'none';
  };

  // ── render ───────────────────────────────────────────────
  function renderCards(items) {
    const g = document.getElementById('grid');

    // update stats
    document.getElementById('s-pending').textContent = items.length;
    document.getElementById('s-high').textContent =
      items.filter(i=>(i.risk_assessment?.risk_level||'').toLowerCase()==='high').length;
    document.getElementById('s-amt').textContent =
      fmt(items.reduce((s,i)=>s+(i.expense?.amount||0),0));
    document.getElementById('cnt').textContent = items.length;

    if (!items.length) {
      g.innerHTML = `<div class="state-wrap">
        <div class="state-icon">✅</div>
        <div class="state-title">All caught up!</div>
        <div class="state-sub">No expenses are waiting for review right now.</div>
      </div>`;
      return;
    }

    g.innerHTML = items.map((item, idx) => {
      const exp  = item.expense || {};
      const risk = item.risk_assessment || {};
      const rc   = riskClass(risk.risk_level);
      const rl   = rc === 'none' ? 'Pending review' : risk.risk_level.toUpperCase() + ' RISK';

      return `<div class="card" id="card-${item.session_id}" style="animation-delay:${idx*0.055}s">
        <div class="card-top">
          <div class="card-meta">
            <div class="sub-row">
              <div class="avatar">${initials(exp.submitter)}</div>
              <div>
                <div class="sub-name">${exp.submitter||'Unknown'}</div>
                <div class="sub-date">${exp.date||'—'}</div>
              </div>
            </div>
            <div class="amt">
              <div class="amt-val">${fmt(exp.amount)}</div>
              <div class="amt-lbl">Expense</div>
            </div>
          </div>
        </div>
        <div class="card-body">
          <div class="cat-badge">${exp.category||'Uncategorized'}</div>
          <p class="desc">${exp.description||'No description provided.'}</p>
          <div class="risk ${rc}">
            <div class="risk-hdr">
              <div class="risk-dot"></div>
              <div class="risk-lbl">${rl}</div>
            </div>
            ${risk.summary?`<div class="risk-sum">${risk.summary}</div>`:''}
          </div>
        </div>
        <div class="card-actions">
          <button class="btn-action btn-reject" id="rej-${item.session_id}"
            onclick="act('${item.session_id}','${item.interrupt_id}','${item.user_id}','reject')">
            ✕&nbsp; Reject
          </button>
          <button class="btn-action btn-approve" id="apr-${item.session_id}"
            onclick="act('${item.session_id}','${item.interrupt_id}','${item.user_id}','approve')">
            ✓&nbsp; Approve
          </button>
        </div>
      </div>`;
    }).join('');
  }

  // ── fetch pending ────────────────────────────────────────
  async function fetchPending() {
    try {
      const res  = await fetch('/api/pending');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      pending    = data.pending || [];
      renderCards(pending);
    } catch(e) {
      document.getElementById('grid').innerHTML =
        `<div class="err-wrap">⚠ Could not load approvals: ${e.message}</div>`;
    }
  }

  // ── take action ──────────────────────────────────────────
  async function act(sessionId, interruptId, userId, decision) {
    const card = document.getElementById('card-' + sessionId);
    const aprB = document.getElementById('apr-' + sessionId);
    const rejB = document.getElementById('rej-' + sessionId);

    aprB.disabled = rejB.disabled = true;
    if (decision === 'approve') aprB.innerHTML = '<div class="spin"></div>';
    else                        rejB.innerHTML = '<div class="spin"></div>';

    try {
      const res  = await fetch('/api/action/' + sessionId, {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({decision, interrupt_id: interruptId, user_id: userId}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'HTTP ' + res.status);

      card.classList.add('resolved');
      toast(decision === 'approve' ? '✓ Approved' : '✕ Rejected');
      openPanel(decision, data, pending.find(i=>i.session_id===sessionId));

      setTimeout(() => {
        pending = pending.filter(i=>i.session_id!==sessionId);
        renderCards(pending);
      }, 1400);

    } catch(e) {
      toast('⚠ ' + e.message, true);
      aprB.disabled = rejB.disabled = false;
      aprB.innerHTML = '✓&nbsp; Approve';
      rejB.innerHTML = '✕&nbsp; Reject';
    }
  }

  // ── panel ────────────────────────────────────────────────
  function openPanel(decision, data, item) {
    const exp  = item?.expense || {};
    const risk = item?.risk_assessment || {};
    const ok   = decision === 'approve';
    const rc   = riskClass(risk.risk_level);

    document.getElementById('p-icon').textContent  = ok ? '✅' : '❌';
    document.getElementById('p-title').textContent = ok ? 'Expense Approved' : 'Expense Rejected';
    document.getElementById('p-sub').textContent   =
      'Human reviewer · ' + new Date().toLocaleTimeString();

    let html = `<div class="p-sec">
      <div class="p-sec-title">Decision</div>
      <div class="p-row">
        <span class="p-lbl">Status</span>
        <span class="dec-badge ${decision}d">${ok?'✓ Approved':'✕ Rejected'}</span>
      </div>
    </div>`;

    if (exp.submitter) {
      html += `<div class="p-sec">
        <div class="p-sec-title">Expense Details</div>
        <div class="p-row"><span class="p-lbl">Submitter</span><span class="p-val">${exp.submitter}</span></div>
        <div class="p-row"><span class="p-lbl">Amount</span><span class="p-val">${fmt(exp.amount)}</span></div>
        <div class="p-row"><span class="p-lbl">Category</span><span class="p-val">${exp.category||'—'}</span></div>
        <div class="p-row"><span class="p-lbl">Description</span><span class="p-val">${exp.description||'—'}</span></div>
        <div class="p-row"><span class="p-lbl">Date</span><span class="p-val">${exp.date||'—'}</span></div>
      </div>`;
    }

    if (risk.risk_level) {
      html += `<div class="p-sec">
        <div class="p-sec-title">Risk Assessment</div>
        <div class="risk ${rc}" style="margin-bottom:10px">
          <div class="risk-hdr"><div class="risk-dot"></div>
          <div class="risk-lbl">${risk.risk_level.toUpperCase()} RISK</div></div>
          ${risk.summary?`<div class="risk-sum">${risk.summary}</div>`:''}
        </div>
        ${(risk.flags||[]).length
          ?`<div class="p-row"><span class="p-lbl">Flags</span><span class="p-val" style="color:var(--muted)">${risk.flags.join(', ')}</span></div>`
          :''}
      </div>`;
    }

    if (data.response) {
      html += `<div class="p-sec">
        <div class="p-sec-title">Agent Response</div>
        <div class="raw">${data.response}</div>
      </div>`;
    }

    document.getElementById('p-body').innerHTML = html;
    document.getElementById('overlay').classList.add('open');
  }

  function overlayClick(e) { if (e.target===document.getElementById('overlay')) closePanel(); }
  function closePanel()    { document.getElementById('overlay').classList.remove('open'); }

  // ── toast ────────────────────────────────────────────────
  function toast(msg, isErr=false) {
    const t = document.getElementById('toast');
    t.textContent   = msg;
    t.style.color   = isErr ? '#fca5a5' : '';
    t.style.borderColor = isErr ? 'rgba(239,68,68,.3)' : '';
    t.classList.add('show');
    setTimeout(()=>t.classList.remove('show'), 3000);
  }

  // ── refresh ──────────────────────────────────────────────
  function doRefresh() {
    document.getElementById('grid').innerHTML = `<div class="state-wrap">
      <div class="loading-ring"></div><div class="state-sub">Refreshing…</div>
    </div>`;
    fetchPending();
  }

  // init + auto-refresh every 30 s
  document.getElementById('f-date').value = new Date().toISOString().slice(0,10);
  updatePreview();
  fetchPending();
  autoTimer = setInterval(fetchPending, 30000);
</script>
</body>
</html>"""
