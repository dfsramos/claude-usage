"""
dashboard-extended.py - Extended dashboard with per-turn cost analysis,
project filtering, custom date range selection, and session chat view.

Serves on localhost:8081 by default.
"""

import json
import os
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

DB_PATH = Path.home() / ".claude" / "usage.db"

# ── Pricing ───────────────────────────────────────────────────────────────────
PRICING = {
    "claude-opus-4-6":   {"input": 5.00,  "output": 25.00, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-opus-4-5":   {"input": 5.00,  "output": 25.00, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.00,  "output": 15.00, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.00,  "output":  5.00, "cache_write": 1.25,  "cache_read": 0.10},
    "claude-haiku-4-6":  {"input": 1.00,  "output":  5.00, "cache_write": 1.25,  "cache_read": 0.10},
}

def _get_pricing(model):
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    m = model.lower()
    if "opus"   in m: return PRICING["claude-opus-4-6"]
    if "sonnet" in m: return PRICING["claude-sonnet-4-6"]
    if "haiku"  in m: return PRICING["claude-haiku-4-5"]
    return None

def _calc_cost(model, inp, out, cache_read, cache_creation):
    p = _get_pricing(model)
    if not p:
        return 0.0
    return (
        inp            * p["input"]       / 1_000_000 +
        out            * p["output"]      / 1_000_000 +
        cache_read     * p["cache_read"]  / 1_000_000 +
        cache_creation * p["cache_write"] / 1_000_000
    )


def _extract_preview(content_json):
    """Return a short human-readable preview for the turns table."""
    try:
        items = json.loads(content_json) if content_json else []
        if not isinstance(items, list):
            items = [items]
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("type", "")
            if t == "text":
                text = item.get("text", "").strip()
                return text[:220] if text else ""
            if t == "tool_use":
                name = item.get("name", "")
                inp  = item.get("input", {})
                if name in ("Read", "Write", "Edit", "Glob") and "file_path" in inp:
                    return f"[{name}] {inp['file_path']}"
                if name == "Bash" and "command" in inp:
                    return f"[Bash] {str(inp['command'])[:120]}"
                if name == "Grep" and "pattern" in inp:
                    return f"[Grep] {inp['pattern']}"
                if name == "Agent" and "prompt" in inp:
                    return f"[Agent] {str(inp['prompt'])[:120]}"
                return f"[{name}]"
    except Exception:
        pass
    return ""


def _parse_content(content_json, max_text=6000, max_result=3000):
    """Parse stored content JSON into a structured list for the chat view."""
    TRUNC = "\n… [truncated]"
    try:
        raw = json.loads(content_json) if content_json else []
    except Exception:
        return content_json or ""

    # Simple string (plain user message)
    if isinstance(raw, str):
        return raw[:max_text]

    if not isinstance(raw, list):
        return str(raw)[:max_text]

    result = []
    for block in raw:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")

        if btype == "text":
            text = block.get("text", "")
            if len(text) > max_text:
                text = text[:max_text] + TRUNC
            result.append({"type": "text", "text": text})

        elif btype == "tool_use":
            result.append({
                "type":  "tool_use",
                "name":  block.get("name", ""),
                "input": block.get("input", {}),
            })

        elif btype == "tool_result":
            inner = block.get("content", "")
            if isinstance(inner, str):
                if len(inner) > max_result:
                    inner = inner[:max_result] + TRUNC
            elif isinstance(inner, list):
                truncated = []
                for item in inner:
                    if isinstance(item, dict) and item.get("type") == "text":
                        t = item.get("text", "")
                        if len(t) > max_result:
                            t = t[:max_result] + TRUNC
                        truncated.append({"type": "text", "text": t})
                    else:
                        truncated.append(item)
                inner = truncated
            result.append({
                "type":       "tool_result",
                "tool_use_id": block.get("tool_use_id", ""),
                "content":    inner,
                "is_error":   bool(block.get("is_error")),
            })

        else:
            # Pass unknown block types through so the frontend can decide
            result.append(block)

    return result


# ── Dashboard data ────────────────────────────────────────────────────────────

def get_dashboard_data(db_path=DB_PATH):
    if not Path(db_path).exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') AS model
        FROM turns GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    proj_rows = conn.execute("""
        SELECT COALESCE(project_name, 'unknown') AS project_name
        FROM sessions GROUP BY project_name
        ORDER BY MAX(last_timestamp) DESC
    """).fetchall()
    all_projects = [r["project_name"] for r in proj_rows]

    daily_rows = conn.execute("""
        SELECT substr(timestamp,1,10) AS day,
               COALESCE(model,'unknown') AS model,
               SUM(input_tokens) AS input, SUM(output_tokens) AS output,
               SUM(cache_read_tokens) AS cache_read,
               SUM(cache_creation_tokens) AS cache_creation,
               COUNT(*) AS turns
        FROM turns GROUP BY day, model ORDER BY day, model
    """).fetchall()
    daily_by_model = [{
        "day": r["day"], "model": r["model"],
        "input": r["input"] or 0, "output": r["output"] or 0,
        "cache_read": r["cache_read"] or 0, "cache_creation": r["cache_creation"] or 0,
        "turns": r["turns"] or 0,
    } for r in daily_rows]

    session_rows = conn.execute("""
        SELECT session_id, project_name, first_timestamp, last_timestamp,
               total_input_tokens, total_output_tokens,
               total_cache_read, total_cache_creation, model, turn_count
        FROM sessions ORDER BY last_timestamp DESC
    """).fetchall()

    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            dur = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            dur = 0
        sessions_all.append({
            "session_id":      r["session_id"][:8],
            "session_id_full": r["session_id"],          # needed for modal link
            "project":         r["project_name"] or "unknown",
            "last":            (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":       (r["last_timestamp"] or "")[:10],
            "duration_min":    dur,
            "model":           r["model"] or "unknown",
            "turns":           r["turn_count"] or 0,
            "input":           r["total_input_tokens"] or 0,
            "output":          r["total_output_tokens"] or 0,
            "cache_read":      r["total_cache_read"] or 0,
            "cache_creation":  r["total_cache_creation"] or 0,
        })

    turn_rows = conn.execute("""
        SELECT t.id, t.session_id, t.timestamp,
               COALESCE(t.model,'unknown') AS model,
               t.input_tokens, t.output_tokens,
               t.cache_read_tokens, t.cache_creation_tokens,
               COALESCE(t.tool_name,'') AS tool_name,
               COALESCE(s.project_name,'unknown') AS project,
               m.content
        FROM turns t
        JOIN sessions s ON t.session_id = s.session_id
        LEFT JOIN messages m ON m.turn_id = t.id AND m.role = 'assistant'
        WHERE t.input_tokens + t.output_tokens + t.cache_read_tokens + t.cache_creation_tokens > 0
        ORDER BY (t.input_tokens + t.output_tokens + t.cache_read_tokens + t.cache_creation_tokens) DESC
        LIMIT 600
    """).fetchall()

    turns_extended = []
    for r in turn_rows:
        cost = _calc_cost(r["model"], r["input_tokens"], r["output_tokens"],
                          r["cache_read_tokens"], r["cache_creation_tokens"])
        turns_extended.append({
            "id":             r["id"],
            "session_id":     r["session_id"],          # for opening session modal
            "timestamp":      (r["timestamp"] or "")[:16].replace("T", " "),
            "date":           (r["timestamp"] or "")[:10],
            "model":          r["model"],
            "project":        r["project"],
            "tool_name":      r["tool_name"],
            "input":          r["input_tokens"] or 0,
            "output":         r["output_tokens"] or 0,
            "cache_read":     r["cache_read_tokens"] or 0,
            "cache_creation": r["cache_creation_tokens"] or 0,
            "cost":           round(cost, 6),
            "preview":        _extract_preview(r["content"]),
        })

    conn.close()
    return {
        "all_models":     all_models,
        "all_projects":   all_projects,
        "daily_by_model": daily_by_model,
        "sessions_all":   sessions_all,
        "turns_extended": turns_extended,
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_session_detail(session_id, db_path=DB_PATH):
    """Return all turns + messages for a session, for the chat modal."""
    if not Path(db_path).exists():
        return None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    session = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if not session:
        # Fallback: prefix match (in case a short id was passed)
        session = conn.execute(
            "SELECT * FROM sessions WHERE session_id LIKE ?", (session_id + "%",)
        ).fetchone()
    if not session:
        conn.close()
        return None

    sid = session["session_id"]
    turn_rows = conn.execute("""
        SELECT t.id, t.timestamp, t.model,
               t.input_tokens, t.output_tokens,
               t.cache_read_tokens, t.cache_creation_tokens,
               t.tool_name,
               m.role, m.content
        FROM turns t
        LEFT JOIN messages m ON m.turn_id = t.id
        WHERE t.session_id = ?
        ORDER BY t.timestamp, t.id
    """, (sid,)).fetchall()
    conn.close()

    turns = []
    for r in turn_rows:
        role = r["role"] or "assistant"
        cost = _calc_cost(
            r["model"] or "",
            r["input_tokens"] or 0, r["output_tokens"] or 0,
            r["cache_read_tokens"] or 0, r["cache_creation_tokens"] or 0,
        )
        turns.append({
            "id":             r["id"],
            "timestamp":      (r["timestamp"] or "").replace("Z", "").replace("T", " "),
            "model":          r["model"] or "",
            "role":           role,
            "input_tokens":   r["input_tokens"] or 0,
            "output_tokens":  r["output_tokens"] or 0,
            "cache_read":     r["cache_read_tokens"] or 0,
            "cache_creation": r["cache_creation_tokens"] or 0,
            "cost":           round(cost, 6),
            "tool_name":      r["tool_name"] or "",
            "content":        _parse_content(r["content"]),
        })

    return {
        "session_id": sid,
        "project":    session["project_name"] or "unknown",
        "first_ts":   (session["first_timestamp"] or "")[:16].replace("T", " "),
        "last_ts":    (session["last_timestamp"] or "")[:16].replace("T", " "),
        "turns":      turns,
    }


# ── HTML template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Usage — Extended Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
    --text: #e2e8f0; --muted: #8892a4; --accent: #d97757;
    --blue: #4f8ef7; --green: #4ade80; --yellow: #fbbf24; --purple: #a78bfa;
    --red: #f87171;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  /* ── Header ── */
  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  header h1 { font-size: 17px; font-weight: 600; color: var(--accent); white-space: nowrap; flex: 1; }
  .meta { color: var(--muted); font-size: 12px; }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* ── Filter bar ── */
  #filter-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 8px 24px; }
  .filter-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 3px 0; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); min-width: 58px; white-space: nowrap; }
  .chip-group { display: flex; flex-wrap: wrap; gap: 4px; }
  .chip { display: flex; align-items: center; padding: 2px 9px; border-radius: 20px; border: 1px solid var(--border); cursor: pointer; font-size: 12px; color: var(--muted); user-select: none; white-space: nowrap; }
  .chip:hover { border-color: var(--accent); color: var(--text); }
  .chip.checked { background: rgba(217,119,87,0.12); border-color: var(--accent); color: var(--text); }
  .chip input { display: none; }
  .filter-btn { padding: 2px 9px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .range-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .range-btn { padding: 3px 11px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 12px; cursor: pointer; }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .range-btn.active { background: rgba(217,119,87,0.15); color: var(--accent); font-weight: 600; }
  .date-inputs { display: none; align-items: center; gap: 6px; }
  .date-inputs.visible { display: flex; }
  .date-inputs input[type=date] { background: var(--bg); border: 1px solid var(--border); color: var(--text); border-radius: 5px; padding: 3px 7px; font-size: 12px; color-scheme: dark; }
  .date-inputs span { color: var(--muted); font-size: 12px; }

  /* ── Layout ── */
  .container { max-width: 1440px; margin: 0 auto; padding: 20px 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 14px; margin-bottom: 20px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 5px; }
  .stat-card .value { font-size: 21px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 3px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 20px; }
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 18px 20px; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 14px; }
  .chart-wrap { position: relative; height: 230px; }
  .chart-wrap.tall { height: 280px; }

  /* ── Tables ── */
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 18px 20px; margin-bottom: 20px; overflow-x: auto; }
  .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
  .section-title { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .export-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 3px 10px; border-radius: 5px; cursor: pointer; font-size: 11px; }
  .export-btn:hover { color: var(--text); border-color: var(--accent); }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 7px 10px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  .sort-icon { font-size: 9px; }
  td { padding: 9px 10px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(79,142,247,0.15); color: var(--blue); }
  .tool-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(167,139,250,0.15); color: var(--purple); }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .session-link { color: var(--blue); font-family: monospace; cursor: pointer; text-decoration: none; font-size: 12px; }
  .session-link:hover { text-decoration: underline; }
  .preview-cell { max-width: 340px; }
  .preview-link { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 330px; display: block; cursor: pointer; }
  .preview-link:hover { color: var(--accent); text-decoration: underline; }
  .notice { color: var(--muted); font-size: 11px; margin-top: 8px; }

  /* ── Pagination ── */
  .pagination { display: flex; align-items: center; gap: 5px; margin-top: 12px; justify-content: flex-end; flex-wrap: wrap; }
  .page-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 12px; cursor: pointer; }
  .page-btn:hover { border-color: var(--accent); color: var(--text); }
  .page-btn.active { background: rgba(217,119,87,0.15); color: var(--accent); border-color: var(--accent); }
  .page-info { color: var(--muted); font-size: 12px; margin-right: 4px; }

  /* ── Session modal ── */
  #session-modal {
    display: none; position: fixed; inset: 0; z-index: 1000;
    background: rgba(0,0,0,0.72); align-items: center; justify-content: center;
  }
  #session-modal.open { display: flex; }
  .modal-panel {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    width: min(960px, 96vw); max-height: 92vh;
    display: flex; flex-direction: column; overflow: hidden;
    box-shadow: 0 24px 80px rgba(0,0,0,0.6);
  }
  .modal-header {
    padding: 14px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: baseline; gap: 10px; flex-shrink: 0; flex-wrap: wrap;
  }
  .modal-title { font-size: 15px; font-weight: 600; font-family: monospace; color: var(--text); }
  .modal-subtitle { font-size: 12px; color: var(--muted); flex: 1; }
  .modal-close {
    background: none; border: 1px solid var(--border); color: var(--muted);
    width: 28px; height: 28px; border-radius: 6px; cursor: pointer; font-size: 14px;
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .modal-close:hover { color: var(--text); border-color: var(--accent); }
  #modal-body { overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 3px; scroll-behavior: smooth; }

  /* ── Chat bubbles ── */
  .chat-turn { border-radius: 8px; padding: 10px 14px; margin: 3px 0; border-left: 3px solid transparent; }
  .user-turn { background: rgba(79,142,247,0.06); border-left-color: rgba(79,142,247,0.4); }
  .assistant-turn { background: rgba(217,119,87,0.05); border-left-color: rgba(217,119,87,0.35); }
  .highlighted-turn { outline: 2px solid var(--accent); outline-offset: 1px; }
  .turn-header { display: flex; align-items: center; gap: 7px; margin-bottom: 7px; flex-wrap: wrap; }
  .role-badge { padding: 1px 7px; border-radius: 4px; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; }
  .user-badge { background: rgba(79,142,247,0.2); color: var(--blue); }
  .assistant-badge { background: rgba(217,119,87,0.2); color: var(--accent); }
  .turn-model { font-size: 11px; color: var(--muted); }
  .turn-time { font-size: 11px; color: var(--muted); font-family: monospace; }
  .turn-cost { font-size: 11px; font-family: monospace; color: var(--green); background: rgba(74,222,128,0.1); padding: 1px 6px; border-radius: 4px; }
  .turn-tokens { font-size: 11px; color: var(--muted); font-family: monospace; }
  .turn-body { font-size: 13px; line-height: 1.65; }
  .text-block { margin: 2px 0; white-space: pre-wrap; word-break: break-word; }

  /* Code inside chat */
  .code-block { background: #0d1117; border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; overflow-x: auto; font-size: 12px; font-family: monospace; margin: 8px 0; white-space: pre; }
  .inline-code { background: rgba(0,0,0,0.35); border: 1px solid var(--border); border-radius: 3px; padding: 1px 5px; font-size: 12px; font-family: monospace; }

  /* Tool use blocks */
  .tool-use-block { background: rgba(167,139,250,0.07); border: 1px solid rgba(167,139,250,0.22); border-radius: 6px; margin: 6px 0; overflow: hidden; }
  .tool-use-header { padding: 5px 10px; display: flex; align-items: center; gap: 8px; background: rgba(167,139,250,0.09); border-bottom: 1px solid rgba(167,139,250,0.15); }
  .tool-name { color: var(--purple); font-weight: 600; font-size: 12px; white-space: nowrap; }
  .tool-summary { color: var(--muted); font-size: 12px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: monospace; }
  .expand-btn { background: none; border: 1px solid var(--border); color: var(--muted); cursor: pointer; font-size: 10px; padding: 1px 6px; border-radius: 3px; white-space: nowrap; flex-shrink: 0; }
  .expand-btn:hover { color: var(--text); border-color: var(--accent); }
  .tool-input { padding: 8px 10px; font-size: 11px; font-family: monospace; white-space: pre-wrap; word-break: break-all; margin: 0; max-height: 220px; overflow-y: auto; color: var(--muted); }
  .tool-input.collapsed { display: none; }

  /* Tool result blocks */
  .tool-result-block { background: rgba(74,222,128,0.05); border: 1px solid rgba(74,222,128,0.18); border-radius: 6px; margin: 6px 0; overflow: hidden; }
  .tool-result-block.is-error { background: rgba(248,113,113,0.07); border-color: rgba(248,113,113,0.25); }
  .tool-result-header { padding: 5px 10px; font-size: 12px; color: var(--muted); cursor: pointer; user-select: none; display: flex; align-items: center; gap: 6px; }
  .tool-result-header:hover { color: var(--text); }
  .tool-result-content { padding: 8px 10px; font-size: 11px; font-family: monospace; white-space: pre-wrap; word-break: break-all; margin: 0; max-height: 320px; overflow-y: auto; color: var(--muted); border-top: 1px solid rgba(74,222,128,0.12); }
  .tool-result-content.collapsed { display: none; }
  .is-error .tool-result-content { border-top-color: rgba(248,113,113,0.2); }

  footer { border-top: 1px solid var(--border); padding: 18px 24px; margin-top: 6px; }
  .footer-content { max-width: 1440px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; }
  .footer-content a { color: var(--blue); text-decoration: none; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }
</style>
</head>
<body>

<!-- Session Chat Modal -->
<div id="session-modal" onclick="if(event.target===this)closeModal()">
  <div class="modal-panel">
    <div class="modal-header">
      <span class="modal-title" id="modal-title">Session</span>
      <span class="modal-subtitle" id="modal-subtitle"></span>
      <button class="modal-close" onclick="closeModal()" title="Close (Esc)">✕</button>
    </div>
    <div id="modal-body"></div>
  </div>
</div>

<header>
  <h1>Claude Usage — Extended Dashboard</h1>
  <div class="meta" id="meta">Loading...</div>
  <button id="rescan-btn" onclick="triggerRescan()">↻ Rescan</button>
</header>

<div id="filter-bar">
  <div class="filter-row">
    <span class="filter-label">Models</span>
    <div class="chip-group" id="model-checkboxes"></div>
    <button class="filter-btn" onclick="selectAll('model')">All</button>
    <button class="filter-btn" onclick="clearAll('model')">None</button>
  </div>
  <div class="filter-row">
    <span class="filter-label">Projects</span>
    <div class="chip-group" id="project-checkboxes"></div>
    <button class="filter-btn" onclick="selectAll('project')">All</button>
    <button class="filter-btn" onclick="clearAll('project')">None</button>
  </div>
  <div class="filter-row">
    <span class="filter-label">Range</span>
    <div class="range-group">
      <button class="range-btn" data-range="7d"     onclick="setRange('7d')">7d</button>
      <button class="range-btn" data-range="30d"    onclick="setRange('30d')">30d</button>
      <button class="range-btn" data-range="90d"    onclick="setRange('90d')">90d</button>
      <button class="range-btn" data-range="all"    onclick="setRange('all')">All</button>
      <button class="range-btn" data-range="custom" onclick="setRange('custom')">Custom</button>
    </div>
    <div class="date-inputs" id="date-inputs">
      <span>From</span><input type="date" id="date-from" oninput="onDateChange()">
      <span>To</span><input type="date" id="date-to" oninput="onDateChange()">
    </div>
  </div>
</div>

<div class="container">
  <div class="stats-row" id="stats-row"></div>

  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title">Daily Token Usage</h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Top Projects by Cost</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
  </div>

  <div class="table-card">
    <div class="section-title">Cost by Model</div>
    <table><thead><tr>
      <th>Model</th>
      <th class="sortable" onclick="setSort('model','turns')">Turns <span class="sort-icon" id="msort-turns"></span></th>
      <th class="sortable" onclick="setSort('model','input')">Input <span class="sort-icon" id="msort-input"></span></th>
      <th class="sortable" onclick="setSort('model','output')">Output <span class="sort-icon" id="msort-output"></span></th>
      <th class="sortable" onclick="setSort('model','cache_read')">Cache Read <span class="sort-icon" id="msort-cache_read"></span></th>
      <th class="sortable" onclick="setSort('model','cache_creation')">Cache Write <span class="sort-icon" id="msort-cache_creation"></span></th>
      <th class="sortable" onclick="setSort('model','cost')">Est. Cost <span class="sort-icon" id="msort-cost"></span></th>
    </tr></thead><tbody id="model-cost-body"></tbody></table>
  </div>

  <div class="table-card">
    <div class="section-header">
      <div class="section-title">Most Expensive Turns <span id="turns-count-label" style="font-weight:400;text-transform:none;letter-spacing:0;font-size:11px"></span></div>
      <button class="export-btn" onclick="exportTurnsCSV()">⬇ CSV</button>
    </div>
    <table><thead><tr>
      <th>Timestamp</th>
      <th>Project</th>
      <th>Model</th>
      <th>Tool</th>
      <th class="sortable" onclick="setSort('turns','input')">Input <span class="sort-icon" id="tsort-input"></span></th>
      <th class="sortable" onclick="setSort('turns','output')">Output <span class="sort-icon" id="tsort-output"></span></th>
      <th class="sortable" onclick="setSort('turns','cache_read')">Cache R <span class="sort-icon" id="tsort-cache_read"></span></th>
      <th class="sortable" onclick="setSort('turns','cache_creation')">Cache W <span class="sort-icon" id="tsort-cache_creation"></span></th>
      <th class="sortable" onclick="setSort('turns','cost')">Est. Cost <span class="sort-icon" id="tsort-cost"></span></th>
      <th>Preview — click to open conversation</th>
    </tr></thead><tbody id="turns-body"></tbody></table>
    <div class="pagination" id="turns-pagination"></div>
    <div class="notice" id="turns-notice"></div>
  </div>

  <div class="table-card">
    <div class="section-header">
      <div class="section-title">Recent Sessions</div>
      <button class="export-btn" onclick="exportSessionsCSV()">⬇ CSV</button>
    </div>
    <table><thead><tr>
      <th>Session — click to view</th>
      <th>Project</th>
      <th class="sortable" onclick="setSort('session','last')">Last Active <span class="sort-icon" id="ssort-last"></span></th>
      <th class="sortable" onclick="setSort('session','duration_min')">Duration <span class="sort-icon" id="ssort-duration_min"></span></th>
      <th>Model</th>
      <th class="sortable" onclick="setSort('session','turns')">Turns <span class="sort-icon" id="ssort-turns"></span></th>
      <th class="sortable" onclick="setSort('session','input')">Input <span class="sort-icon" id="ssort-input"></span></th>
      <th class="sortable" onclick="setSort('session','output')">Output <span class="sort-icon" id="ssort-output"></span></th>
      <th class="sortable" onclick="setSort('session','cost')">Est. Cost <span class="sort-icon" id="ssort-cost"></span></th>
    </tr></thead><tbody id="sessions-body"></tbody></table>
    <div class="pagination" id="sessions-pagination"></div>
  </div>

  <div class="table-card">
    <div class="section-header">
      <div class="section-title">Cost by Project</div>
      <button class="export-btn" onclick="exportProjectsCSV()">⬇ CSV</button>
    </div>
    <table><thead><tr>
      <th>Project</th>
      <th class="sortable" onclick="setSort('project','sessions')">Sessions <span class="sort-icon" id="psort-sessions"></span></th>
      <th class="sortable" onclick="setSort('project','turns')">Turns <span class="sort-icon" id="psort-turns"></span></th>
      <th class="sortable" onclick="setSort('project','input')">Input <span class="sort-icon" id="psort-input"></span></th>
      <th class="sortable" onclick="setSort('project','output')">Output <span class="sort-icon" id="psort-output"></span></th>
      <th class="sortable" onclick="setSort('project','cost')">Est. Cost <span class="sort-icon" id="psort-cost"></span></th>
    </tr></thead><tbody id="project-cost-body"></tbody></table>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Cost estimates use Anthropic API pricing (April 2026). Click any Session ID or turn preview to open the conversation chat view. Top 600 turns tracked individually. Daily chart is model &amp; date filtered; all other views also respect the project filter.</p>
  </div>
</footer>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}
function fmt(n) {
  if (!n) return '0';
  if (n >= 1e9) return (n/1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toFixed(4); }
function fmtCostBig(c) { return '$' + c.toFixed(2); }

// ── Pricing ────────────────────────────────────────────────────────────────
const PRICING = {
  'claude-opus-4-6':   { input:5.00, output:25.00, cache_write:6.25,  cache_read:0.50 },
  'claude-opus-4-5':   { input:5.00, output:25.00, cache_write:6.25,  cache_read:0.50 },
  'claude-sonnet-4-6': { input:3.00, output:15.00, cache_write:3.75,  cache_read:0.30 },
  'claude-sonnet-4-5': { input:3.00, output:15.00, cache_write:3.75,  cache_read:0.30 },
  'claude-haiku-4-5':  { input:1.00, output:5.00,  cache_write:1.25,  cache_read:0.10 },
  'claude-haiku-4-6':  { input:1.00, output:5.00,  cache_write:1.25,  cache_read:0.10 },
};
function isBillable(m) { if (!m) return false; const l = m.toLowerCase(); return l.includes('opus')||l.includes('sonnet')||l.includes('haiku'); }
function getPricing(m) {
  if (!m) return null;
  if (PRICING[m]) return PRICING[m];
  for (const k of Object.keys(PRICING)) if (m.startsWith(k)) return PRICING[k];
  const l = m.toLowerCase();
  if (l.includes('opus'))   return PRICING['claude-opus-4-6'];
  if (l.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (l.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  return null;
}
function calcCost(m, inp, out, cr, cw) {
  if (!isBillable(m)) return 0;
  const p = getPricing(m);
  if (!p) return 0;
  return inp*p.input/1e6 + out*p.output/1e6 + cr*p.cache_read/1e6 + cw*p.cache_write/1e6;
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set(), selectedProjects = new Set();
let selectedRange = '30d', customFrom = '', customTo = '';
let charts = {};
const sortState = {
  model:   {col:'cost', dir:'desc'}, session: {col:'last',  dir:'desc'},
  project: {col:'cost', dir:'desc'}, turns:   {col:'cost',  dir:'desc'},
};
const pageState = { session:{page:0,size:25}, turns:{page:0,size:25} };
let lastFilteredSessions = [], lastFilteredTurns = [], lastByProject = [];

// ── Date range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = {'7d':'Last 7 Days','30d':'Last 30 Days','90d':'Last 90 Days','all':'All Time','custom':'Custom Range'};
const RANGE_TICKS  = {'7d':7,'30d':15,'90d':13,'all':12,'custom':15};
function getDateCutoff() {
  if (selectedRange === 'all') return {from:null,to:null};
  if (selectedRange === 'custom') return {from:customFrom||null,to:customTo||null};
  const days = selectedRange==='7d'?7:selectedRange==='30d'?30:90;
  const d = new Date(); d.setDate(d.getDate()-days);
  return {from:d.toISOString().slice(0,10),to:null};
}
function inRange(dateStr) {
  const {from,to} = getDateCutoff();
  const d = (dateStr||'').slice(0,10);
  if (from && d < from) return false;
  if (to   && d > to)   return false;
  return true;
}
function setRange(range) {
  selectedRange = range;
  document.querySelectorAll('.range-btn').forEach(b=>b.classList.toggle('active',b.dataset.range===range));
  document.getElementById('date-inputs').classList.toggle('visible', range==='custom');
  updateURL(); applyFilter();
}
function onDateChange() {
  customFrom = document.getElementById('date-from').value;
  customTo   = document.getElementById('date-to').value;
  applyFilter();
}

// ── URL ────────────────────────────────────────────────────────────────────
function updateURL() {
  const p = new URLSearchParams();
  if (selectedRange !== '30d') p.set('range', selectedRange);
  if (customFrom) p.set('from', customFrom);
  if (customTo)   p.set('to', customTo);
  if (rawData) {
    const billable = rawData.all_models.filter(m=>isBillable(m));
    if (!billable.every(m=>selectedModels.has(m))) p.set('models',[...selectedModels].join(','));
    if (!rawData.all_projects.every(x=>selectedProjects.has(x))) p.set('projects',[...selectedProjects].join(','));
  }
  history.replaceState(null,'',window.location.pathname+(p.toString()?'?'+p.toString():''));
}
function readURL() {
  const p = new URLSearchParams(window.location.search);
  return {range:p.get('range')||'30d',from:p.get('from')||'',to:p.get('to')||'',models:p.get('models'),projects:p.get('projects')};
}

// ── Filter chips ───────────────────────────────────────────────────────────
function buildFilter(ids, containerId, group, fromURL, defaultCheck) {
  const fromSet = fromURL ? new Set(fromURL.split(',')) : null;
  const set = group==='model' ? selectedModels : selectedProjects;
  set.clear();
  document.getElementById(containerId).innerHTML = ids.map(id => {
    const checked = fromSet ? fromSet.has(id) : defaultCheck(id);
    if (checked) set.add(id);
    return `<label class="chip${checked?' checked':''}" data-group="${group}">
      <input type="checkbox" value="${esc(id)}" ${checked?'checked':''} onchange="onChip(this,'${group}')">
      ${esc(id)}</label>`;
  }).join('');
}
function onChip(cb, group) {
  const set = group==='model'?selectedModels:selectedProjects;
  const label = cb.closest('label');
  if (cb.checked){set.add(cb.value);label.classList.add('checked');}
  else{set.delete(cb.value);label.classList.remove('checked');}
  updateURL(); applyFilter();
}
function selectAll(group) {
  const set = group==='model'?selectedModels:selectedProjects;
  document.querySelectorAll(`[data-group="${group}"] input`).forEach(cb=>{cb.checked=true;set.add(cb.value);cb.closest('label').classList.add('checked');});
  updateURL(); applyFilter();
}
function clearAll(group) {
  const set = group==='model'?selectedModels:selectedProjects;
  document.querySelectorAll(`[data-group="${group}"] input`).forEach(cb=>{cb.checked=false;set.delete(cb.value);cb.closest('label').classList.remove('checked');});
  updateURL(); applyFilter();
}

// ── Sort ───────────────────────────────────────────────────────────────────
function setSort(table, col) {
  const s = sortState[table];
  if (s.col===col) s.dir=s.dir==='desc'?'asc':'desc'; else{s.col=col;s.dir='desc';}
  if (pageState[table]) pageState[table].page=0;
  updateAllSortIcons(); applyFilter();
}
function updateAllSortIcons() {
  document.querySelectorAll('.sort-icon').forEach(el=>el.textContent='');
  [['model','msort'],['session','ssort'],['project','psort'],['turns','tsort']].forEach(([t,p])=>{
    const {col,dir}=sortState[t];
    const el=document.getElementById(`${p}-${col}`);
    if(el) el.textContent=dir==='desc'?' ▼':' ▲';
  });
}
function doSort(arr, table, costFn) {
  const {col,dir}=sortState[table];
  return [...arr].sort((a,b)=>{
    const av=col==='cost'?costFn(a):(a[col]??0);
    const bv=col==='cost'?costFn(b):(b[col]??0);
    if (typeof av==='string') return dir==='desc'?bv.localeCompare(av):av.localeCompare(bv);
    return dir==='desc'?bv-av:av-bv;
  });
}

// ── Pagination ─────────────────────────────────────────────────────────────
function renderPagination(containerId, state, total, onPageFn) {
  const pages = Math.ceil(total/state.size);
  if (pages<=1){document.getElementById(containerId).innerHTML='';return;}
  const cur=state.page;
  let html=`<span class="page-info">${cur*state.size+1}–${Math.min((cur+1)*state.size,total)} of ${total}</span>`;
  if(cur>0) html+=`<button class="page-btn" onclick="${onPageFn}(${cur-1})">‹</button>`;
  const s=Math.max(0,cur-3),e=Math.min(pages-1,cur+3);
  for(let i=s;i<=e;i++) html+=`<button class="page-btn${i===cur?' active':''}" onclick="${onPageFn}(${i})">${i+1}</button>`;
  if(cur<pages-1) html+=`<button class="page-btn" onclick="${onPageFn}(${cur+1})">›</button>`;
  document.getElementById(containerId).innerHTML=html;
}
function goSessionPage(p){pageState.session.page=p;renderSessionsTable(lastFilteredSessions);}
function goTurnsPage(p){pageState.turns.page=p;renderTurnsTable(lastFilteredTurns);}

// ── Main filter ────────────────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  const filteredDaily = rawData.daily_by_model.filter(r=>selectedModels.has(r.model)&&inRange(r.day));
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day]={day:r.day,input:0,output:0,cache_read:0,cache_creation:0};
    const d=dailyMap[r.day]; d.input+=r.input;d.output+=r.output;d.cache_read+=r.cache_read;d.cache_creation+=r.cache_creation;
  }
  const daily=Object.values(dailyMap).sort((a,b)=>a.day.localeCompare(b.day));

  const filteredSessions=rawData.sessions_all.filter(s=>selectedModels.has(s.model)&&selectedProjects.has(s.project)&&inRange(s.last_date));

  const modelMap={};
  for(const r of filteredDaily){
    if(!modelMap[r.model]) modelMap[r.model]={model:r.model,input:0,output:0,cache_read:0,cache_creation:0,turns:0,sessions:0};
    const m=modelMap[r.model]; m.input+=r.input;m.output+=r.output;m.cache_read+=r.cache_read;m.cache_creation+=r.cache_creation;m.turns+=r.turns;
  }
  for(const s of filteredSessions) if(modelMap[s.model]) modelMap[s.model].sessions++;
  const byModel=Object.values(modelMap);

  const projMap={};
  for(const s of filteredSessions){
    if(!projMap[s.project]) projMap[s.project]={project:s.project,input:0,output:0,cache_read:0,cache_creation:0,turns:0,sessions:0,cost:0};
    const p=projMap[s.project]; p.input+=s.input;p.output+=s.output;p.cache_read+=s.cache_read;p.cache_creation+=s.cache_creation;p.turns+=s.turns;p.sessions++;
    p.cost+=calcCost(s.model,s.input,s.output,s.cache_read,s.cache_creation);
  }
  const byProject=Object.values(projMap);

  const filteredTurns=rawData.turns_extended.filter(t=>selectedModels.has(t.model)&&selectedProjects.has(t.project)&&inRange(t.date));

  const totals={
    sessions:filteredSessions.length,
    turns:byModel.reduce((s,m)=>s+m.turns,0),
    input:byModel.reduce((s,m)=>s+m.input,0),
    output:byModel.reduce((s,m)=>s+m.output,0),
    cache_read:byModel.reduce((s,m)=>s+m.cache_read,0),
    cache_creation:byModel.reduce((s,m)=>s+m.cache_creation,0),
    cost:filteredSessions.reduce((s,sess)=>s+calcCost(sess.model,sess.input,sess.output,sess.cache_read,sess.cache_creation),0),
  };

  document.getElementById('daily-chart-title').textContent='Daily Token Usage — '+RANGE_LABELS[selectedRange]+' (model & date filter)';
  renderStats(totals);
  renderDailyChart(daily);
  renderModelChart(byModel);
  renderProjectChart(byProject);

  lastFilteredSessions=doSort(filteredSessions,'session',s=>calcCost(s.model,s.input,s.output,s.cache_read,s.cache_creation));
  lastByProject=doSort(byProject,'project',p=>p.cost);
  lastFilteredTurns=doSort(filteredTurns,'turns',t=>t.cost);
  pageState.session.page=0; pageState.turns.page=0;

  renderModelCostTable(byModel);
  renderTurnsTable(lastFilteredTurns);
  renderSessionsTable(lastFilteredSessions);
  renderProjectCostTable(lastByProject);
}

// ── Stat cards ─────────────────────────────────────────────────────────────
function renderStats(t) {
  const lbl=RANGE_LABELS[selectedRange].toLowerCase();
  document.getElementById('stats-row').innerHTML=[
    {label:'Sessions',      value:t.sessions.toLocaleString(),sub:lbl},
    {label:'Turns',         value:fmt(t.turns),sub:lbl},
    {label:'Input Tokens',  value:fmt(t.input),sub:lbl},
    {label:'Output Tokens', value:fmt(t.output),sub:lbl},
    {label:'Cache Read',    value:fmt(t.cache_read),sub:'from prompt cache'},
    {label:'Cache Write',   value:fmt(t.cache_creation),sub:'writes to prompt cache'},
    {label:'Est. Cost',     value:fmtCostBig(t.cost),sub:'API pricing',color:'#4ade80'},
  ].map(s=>`<div class="stat-card"><div class="label">${s.label}</div><div class="value"${s.color?` style="color:${s.color}"`:''}">${esc(s.value)}</div>${s.sub?`<div class="sub">${esc(s.sub)}</div>`:''}</div>`).join('');
}

// ── Charts ─────────────────────────────────────────────────────────────────
const TC={input:'rgba(79,142,247,0.8)',output:'rgba(167,139,250,0.8)',cache_read:'rgba(74,222,128,0.6)',cache_creation:'rgba(251,191,36,0.6)'};
const MC=['#d97757','#4f8ef7','#4ade80','#a78bfa','#fbbf24','#f472b6','#34d399','#60a5fa'];

function renderDailyChart(daily) {
  const ctx=document.getElementById('chart-daily').getContext('2d');
  if(charts.daily) charts.daily.destroy();
  charts.daily=new Chart(ctx,{type:'bar',data:{labels:daily.map(d=>d.day),datasets:[
    {label:'Input',data:daily.map(d=>d.input),backgroundColor:TC.input,stack:'t'},
    {label:'Output',data:daily.map(d=>d.output),backgroundColor:TC.output,stack:'t'},
    {label:'Cache Read',data:daily.map(d=>d.cache_read),backgroundColor:TC.cache_read,stack:'t'},
    {label:'Cache Write',data:daily.map(d=>d.cache_creation),backgroundColor:TC.cache_creation,stack:'t'},
  ]},options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{labels:{color:'#8892a4',boxWidth:12}}},
    scales:{x:{ticks:{color:'#8892a4',maxTicksLimit:RANGE_TICKS[selectedRange]||15},grid:{color:'#2a2d3a'}},y:{ticks:{color:'#8892a4',callback:v=>fmt(v)},grid:{color:'#2a2d3a'}}},
  }});
}
function renderModelChart(byModel) {
  const ctx=document.getElementById('chart-model').getContext('2d');
  if(charts.model) charts.model.destroy();
  if(!byModel.length) return;
  charts.model=new Chart(ctx,{type:'doughnut',data:{labels:byModel.map(m=>m.model),datasets:[{data:byModel.map(m=>m.input+m.output),backgroundColor:MC,borderWidth:2,borderColor:'#1a1d27'}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'bottom',labels:{color:'#8892a4',boxWidth:12,font:{size:11}}},tooltip:{callbacks:{label:c=>` ${c.label}: ${fmt(c.raw)} tokens`}}}},
  });
}
function renderProjectChart(byProject) {
  const top=[...byProject].sort((a,b)=>b.cost-a.cost).slice(0,10);
  const ctx=document.getElementById('chart-project').getContext('2d');
  if(charts.project) charts.project.destroy();
  if(!top.length) return;
  charts.project=new Chart(ctx,{type:'bar',data:{labels:top.map(p=>p.project.length>24?'…'+p.project.slice(-22):p.project),datasets:[{label:'Est. Cost ($)',data:top.map(p=>+p.cost.toFixed(4)),backgroundColor:MC}]},
    options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>` $${c.raw.toFixed(4)}`}}},
      scales:{x:{ticks:{color:'#8892a4',callback:v=>'$'+v.toFixed(2)},grid:{color:'#2a2d3a'}},y:{ticks:{color:'#8892a4',font:{size:11}},grid:{color:'#2a2d3a'}}},
    },
  });
}

// ── Table renderers ────────────────────────────────────────────────────────
function renderModelCostTable(byModel) {
  document.getElementById('model-cost-body').innerHTML=doSort(byModel,'model',m=>calcCost(m.model,m.input,m.output,m.cache_read,m.cache_creation)).map(m=>{
    const cost=calcCost(m.model,m.input,m.output,m.cache_read,m.cache_creation);
    return `<tr><td><span class="model-tag">${esc(m.model)}</span></td><td class="num">${fmt(m.turns)}</td><td class="num">${fmt(m.input)}</td><td class="num">${fmt(m.output)}</td><td class="num">${fmt(m.cache_read)}</td><td class="num">${fmt(m.cache_creation)}</td><td class="${isBillable(m.model)?'cost':'cost-na'}">${isBillable(m.model)?fmtCost(cost):'n/a'}</td></tr>`;
  }).join('');
}

function renderTurnsTable(turns) {
  const {page,size}=pageState.turns;
  const slice=turns.slice(page*size,(page+1)*size);
  document.getElementById('turns-body').innerHTML=slice.map(t=>{
    const toolCell=t.tool_name?`<td><span class="tool-tag">${esc(t.tool_name)}</span></td>`:`<td class="muted">—</td>`;
    const preview=t.preview||'—';
    const clickAttr=t.session_id?`onclick="openSessionModal('${esc(t.session_id)}',${t.id})" title="Click to open conversation at this turn"`:'';
    return `<tr>
      <td class="muted" style="white-space:nowrap;font-family:monospace;font-size:12px">${esc(t.timestamp)}</td>
      <td>${esc(t.project)}</td>
      <td><span class="model-tag">${esc(t.model)}</span></td>
      ${toolCell}
      <td class="num">${fmt(t.input)}</td>
      <td class="num">${fmt(t.output)}</td>
      <td class="num">${fmt(t.cache_read)}</td>
      <td class="num">${fmt(t.cache_creation)}</td>
      <td class="${isBillable(t.model)?'cost':'cost-na'}">${isBillable(t.model)?fmtCost(t.cost):'n/a'}</td>
      <td class="preview-cell"><span class="preview-link" ${clickAttr}>${esc(preview)}</span></td>
    </tr>`;
  }).join('');
  document.getElementById('turns-count-label').textContent=`— ${turns.length.toLocaleString()} turns`;
  document.getElementById('turns-notice').textContent=
    turns.length===rawData.turns_extended.length
      ?`Showing all ${turns.length} tracked turns. Apply filters to narrow down.`
      :`Filtered from top ${rawData.turns_extended.length} turns (by token count).`;
  renderPagination('turns-pagination',pageState.turns,turns.length,'goTurnsPage');
}

function renderSessionsTable(sessions) {
  const {page,size}=pageState.session;
  const slice=sessions.slice(page*size,(page+1)*size);
  document.getElementById('sessions-body').innerHTML=slice.map(s=>{
    const cost=calcCost(s.model,s.input,s.output,s.cache_read,s.cache_creation);
    return `<tr>
      <td><a class="session-link" onclick="openSessionModal('${esc(s.session_id_full)}')" title="Open conversation">${esc(s.session_id)}…</a></td>
      <td>${esc(s.project)}</td>
      <td class="muted">${esc(s.last)}</td>
      <td class="muted">${esc(s.duration_min)}m</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      <td class="${isBillable(s.model)?'cost':'cost-na'}">${isBillable(s.model)?fmtCost(cost):'n/a'}</td>
    </tr>`;
  }).join('');
  renderPagination('sessions-pagination',pageState.session,sessions.length,'goSessionPage');
}

function renderProjectCostTable(byProject) {
  document.getElementById('project-cost-body').innerHTML=byProject.map(p=>`<tr>
    <td>${esc(p.project)}</td><td class="num">${p.sessions}</td><td class="num">${fmt(p.turns)}</td>
    <td class="num">${fmt(p.input)}</td><td class="num">${fmt(p.output)}</td><td class="cost">${fmtCost(p.cost)}</td>
  </tr>`).join('');
}

// ── Session modal ──────────────────────────────────────────────────────────
const sessionCache = {};

async function openSessionModal(sessionId, highlightTurnId) {
  const modal = document.getElementById('session-modal');
  const body  = document.getElementById('modal-body');
  modal.classList.add('open');
  body.innerHTML = '<div style="padding:40px;text-align:center;color:var(--muted)">Loading conversation…</div>';

  try {
    let data = sessionCache[sessionId];
    if (!data) {
      const resp = await fetch('/api/session/' + encodeURIComponent(sessionId));
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      data = await resp.json();
      sessionCache[sessionId] = data;
    }

    document.getElementById('modal-title').textContent = 'Session ' + data.session_id.slice(0,8) + '…';
    document.getElementById('modal-subtitle').textContent =
      data.project + ' · ' + data.first_ts + ' → ' + data.last_ts + ' · ' + data.turns.length + ' turns';

    body.innerHTML = data.turns.map(t => renderChatTurn(t, t.id === highlightTurnId)).join('');

    if (highlightTurnId) {
      const el = document.getElementById('turn-' + highlightTurnId);
      if (el) el.scrollIntoView({behavior:'smooth', block:'center'});
    } else {
      body.scrollTop = 0;
    }
  } catch(e) {
    body.innerHTML = `<div style="padding:20px;color:var(--red)">Failed to load session: ${esc(e.message)}</div>`;
  }
}

function closeModal() {
  document.getElementById('session-modal').classList.remove('open');
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ── Chat turn renderer ─────────────────────────────────────────────────────
function renderChatTurn(turn, highlighted) {
  const role = turn.role || 'assistant';
  const time = (turn.timestamp || '').slice(11, 16); // HH:MM

  let header;
  if (role === 'user') {
    header = `<div class="turn-header">
      <span class="role-badge user-badge">User</span>
      <span class="turn-time">${esc(time)}</span>
    </div>`;
  } else {
    const hasCost = turn.cost > 0;
    const hasTokens = turn.input_tokens > 0 || turn.output_tokens > 0;
    header = `<div class="turn-header">
      <span class="role-badge assistant-badge">Claude</span>
      ${turn.model ? `<span class="turn-model">${esc(turn.model)}</span>` : ''}
      <span class="turn-time">${esc(time)}</span>
      ${hasTokens ? `<span class="turn-tokens">${fmt(turn.input_tokens)} in / ${fmt(turn.output_tokens)} out${turn.cache_read>0?' / '+fmt(turn.cache_read)+' cache-r':''}</span>` : ''}
      ${hasCost ? `<span class="turn-cost">${fmtCost(turn.cost)}</span>` : ''}
    </div>`;
  }

  return `<div class="chat-turn ${role}-turn${highlighted?' highlighted-turn':''}" id="turn-${turn.id}">
    ${header}
    <div class="turn-body">${renderContent(turn.content)}</div>
  </div>`;
}

// ── Content renderer ───────────────────────────────────────────────────────
function renderContent(content) {
  if (!content && content !== 0) return '<span style="color:var(--muted);font-size:12px">—</span>';
  if (typeof content === 'string') return renderMarkdown(content);
  if (!Array.isArray(content)) return `<pre class="code-block">${esc(JSON.stringify(content,null,2))}</pre>`;

  return content.map(block => {
    if (!block || typeof block !== 'object') return '';
    switch(block.type) {
      case 'text':
        return `<div class="text-block">${renderMarkdown(block.text||'')}</div>`;
      case 'tool_use':
        return renderToolUse(block);
      case 'tool_result':
        return renderToolResult(block);
      default:
        return `<div class="text-block" style="color:var(--muted);font-size:11px">[${esc(block.type)}]</div>`;
    }
  }).join('');
}

function renderMarkdown(text) {
  if (!text) return '';
  // Split on ``` code blocks first to avoid escaping their contents
  const parts = text.split(/(```[\s\S]*?```)/g);
  return parts.map(part => {
    if (part.startsWith('```')) {
      const inner = part.slice(3, -3);
      const nl = inner.indexOf('\n');
      const code = nl >= 0 ? inner.slice(nl + 1) : inner;
      return `<pre class="code-block">${esc(code)}</pre>`;
    }
    // Escape HTML, then apply inline markdown
    let html = esc(part);
    html = html.replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>');
    html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\n/g, '<br>');
    return html;
  }).join('');
}

function renderToolUse(block) {
  const name  = block.name || 'tool';
  const input = block.input || {};
  // Build a compact summary of the primary argument
  let summary = '';
  if (input.file_path)  summary = input.file_path;
  else if (input.command)    summary = String(input.command).slice(0, 140);
  else if (input.pattern)    summary = input.pattern;
  else if (input.prompt)     summary = String(input.prompt).slice(0, 140);
  else if (input.skill)      summary = input.skill;
  else { const k = Object.keys(input)[0]; if (k) summary = `${k}: ${JSON.stringify(input[k])}`.slice(0,140); }

  const inputJson = JSON.stringify(input, null, 2);
  const needsToggle = inputJson.length > 200;
  const uid = 'tu' + Math.random().toString(36).slice(2,8);

  return `<div class="tool-use-block">
    <div class="tool-use-header">
      <span class="tool-name">${esc(name)}</span>
      ${summary ? `<span class="tool-summary">${esc(summary)}</span>` : ''}
      ${needsToggle ? `<button class="expand-btn" onclick="toggleBlock('${uid}',this)">args ▼</button>` : ''}
    </div>
    <pre class="tool-input${needsToggle?' collapsed':''}" id="${uid}">${esc(inputJson)}</pre>
  </div>`;
}

function renderToolResult(block) {
  const isError = !!block.is_error;
  const content = block.content;
  let text = '';
  if (typeof content === 'string') {
    text = content;
  } else if (Array.isArray(content)) {
    text = content.map(c => {
      if (typeof c === 'string') return c;
      if (c && c.type === 'text') return c.text || '';
      return JSON.stringify(c);
    }).join('\n');
  } else if (content) {
    text = JSON.stringify(content);
  }

  const isLong  = text.length > 300;
  const uid     = 'tr' + Math.random().toString(36).slice(2,8);
  const chevron = isLong ? ' ▼ (click to expand)' : '';

  return `<div class="tool-result-block${isError?' is-error':''}">
    <div class="tool-result-header" onclick="toggleBlock('${uid}',this)">
      ${isError ? '✗ Error' : '↩ Result'}${chevron}
    </div>
    <pre class="tool-result-content${isLong?' collapsed':''}" id="${uid}">${esc(text)}</pre>
  </div>`;
}

function toggleBlock(id, btn) {
  const el = document.getElementById(id);
  if (!el) return;
  const collapsed = el.classList.toggle('collapsed');
  if (btn) {
    // Update chevron direction
    btn.textContent = btn.textContent.replace(/[▼▲]/g, collapsed ? '▼' : '▲');
  }
}

// ── CSV export ─────────────────────────────────────────────────────────────
function csvF(v){const s=String(v??'');return(s.includes(',')||s.includes('"')||s.includes('\n'))?`"${s.replace(/"/g,'""')}"`:s;}
function csvTs(){const d=new Date();return`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;}
function downloadCSV(name,header,rows){
  const lines=[header.map(csvF).join(','),...rows.map(r=>r.map(csvF).join(','))];
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([lines.join('\n')],{type:'text/csv;charset=utf-8;'}));
  a.download=`${name}_${csvTs()}.csv`; a.click(); URL.revokeObjectURL(a.href);
}
function exportTurnsCSV(){
  downloadCSV('turns',['Timestamp','Project','Model','Tool','Input','Output','Cache Read','Cache Write','Est. Cost','Preview'],
    lastFilteredTurns.map(t=>[t.timestamp,t.project,t.model,t.tool_name,t.input,t.output,t.cache_read,t.cache_creation,t.cost.toFixed(6),t.preview]));
}
function exportSessionsCSV(){
  downloadCSV('sessions',['Session','Project','Last Active','Duration (min)','Model','Turns','Input','Output','Cache Read','Cache Write','Est. Cost'],
    lastFilteredSessions.map(s=>{const c=calcCost(s.model,s.input,s.output,s.cache_read,s.cache_creation);
      return[s.session_id_full,s.project,s.last,s.duration_min,s.model,s.turns,s.input,s.output,s.cache_read,s.cache_creation,c.toFixed(4)];}));
}
function exportProjectsCSV(){
  downloadCSV('projects',['Project','Sessions','Turns','Input','Output','Cache Read','Cache Write','Est. Cost'],
    lastByProject.map(p=>[p.project,p.sessions,p.turns,p.input,p.output,p.cache_read,p.cache_creation,p.cost.toFixed(4)]));
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn=document.getElementById('rescan-btn');
  btn.disabled=true; btn.textContent='↻ Scanning…';
  try {
    const resp=await fetch('/api/rescan',{method:'POST'});
    const d=await resp.json();
    btn.textContent=`↻ Done (${d.new} new, ${d.updated} upd)`;
    await loadData();
  } catch(e){btn.textContent='↻ Error';}
  setTimeout(()=>{btn.textContent='↻ Rescan';btn.disabled=false;},4000);
}

// ── Load ───────────────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp=await fetch('/api/data');
    const d=await resp.json();
    if(d.error){document.body.innerHTML=`<div style="padding:40px;color:var(--red)">${esc(d.error)}</div>`;return;}
    document.getElementById('meta').textContent='Updated: '+d.generated_at+' · Auto-refresh in 60s';

    const isFirst=rawData===null;
    rawData=d;

    if(isFirst){
      const url=readURL();
      selectedRange=['7d','30d','90d','all','custom'].includes(url.range)?url.range:'30d';
      customFrom=url.from; customTo=url.to;
      document.querySelectorAll('.range-btn').forEach(b=>b.classList.toggle('active',b.dataset.range===selectedRange));
      document.getElementById('date-inputs').classList.toggle('visible',selectedRange==='custom');
      if(customFrom) document.getElementById('date-from').value=customFrom;
      if(customTo)   document.getElementById('date-to').value=customTo;

      // Sort models: opus > sonnet > haiku > other
      const sortedModels=[...d.all_models].sort((a,b)=>{
        const rank=m=>m.includes('opus')?0:m.includes('sonnet')?1:m.includes('haiku')?2:3;
        return rank(a.toLowerCase())-rank(b.toLowerCase())||a.localeCompare(b);
      });
      buildFilter(sortedModels,'model-checkboxes','model',url.models,m=>isBillable(m));
      buildFilter(d.all_projects,'project-checkboxes','project',url.projects,()=>true);
      updateAllSortIcons();
    }
    applyFilter();
  } catch(e){console.error(e);}
}

loadData();
setInterval(loadData,60000);
</script>
</body>
</html>
"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML_TEMPLATE.encode())

        elif self.path == "/api/data":
            self._json(get_dashboard_data())

        elif self.path.startswith("/api/session/"):
            session_id = self.path[len("/api/session/"):]
            data = get_session_detail(session_id)
            if data:
                self._json(data)
            else:
                self.send_response(404); self.end_headers()

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/api/rescan":
            if DB_PATH.exists():
                DB_PATH.unlink()
            from scanner import scan
            self._json(scan(verbose=False))
        else:
            self.send_response(404); self.end_headers()

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        body = json.dumps(data).encode("utf-8")
        self._send(200, "application/json", body)


def serve(host=None, port=None):
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8081"))
    server = HTTPServer((host, port), DashboardHandler)
    print(f"Extended dashboard → http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
