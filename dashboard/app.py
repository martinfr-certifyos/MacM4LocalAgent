"""FastAPI + HTMX dashboard at http://127.0.0.1:4001.

Pages:
  /           live stats (HTMX polling), routing pie, recent requests
  /compare    A/B compare form; stored history
  /compare/{id}  side-by-side view of one comparison

The dashboard reads cost/cost.db (populated by the LiteLLM router callback)
and also drives the A/B comparator via compare/ab.py.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cost.ingest import connect       # noqa: E402
from cost.savings import summarize    # noqa: E402
from compare.ab import run as ab_run  # noqa: E402

app = FastAPI(title="MacM4LocalAgent Dashboard")
templates = Jinja2Templates(directory=str(REPO_ROOT / "dashboard" / "templates"))
# Disable Jinja2's template cache: avoids an upstream LRUCache hash bug seen on
# Python 3.14, and the perf hit is negligible for a tiny local dashboard.
templates.env.cache = None
app.mount("/static", StaticFiles(directory=str(REPO_ROOT / "dashboard" / "static")), name="static")


def _fmt_ts(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> Any:
    s7 = summarize(7)
    return templates.TemplateResponse(
        request, "index.html", {"s7": s7},
    )


@app.get("/stats", response_class=HTMLResponse)
def stats_fragment(request: Request) -> Any:
    """HTMX-polled live tickers."""
    today  = summarize(1)
    week   = summarize(7)
    alltime = summarize(None)

    conn = connect()
    recent = [dict(r) for r in conn.execute(
        "SELECT id, ts, model, tier, input_tok, output_tok, actual_cost, latency_ms, route_reason "
        "FROM requests ORDER BY id DESC LIMIT 25"
    ).fetchall()]
    conn.close()
    for r in recent:
        r["ts_human"] = _fmt_ts(r["ts"])

    return templates.TemplateResponse(
        request, "_stats.html",
        {"today": today, "week": week, "alltime": alltime, "recent": recent},
    )


@app.get("/api/stats")
def api_stats() -> JSONResponse:
    return JSONResponse({
        "today": summarize(1),
        "week":  summarize(7),
        "month": summarize(30),
        "all":   summarize(None),
    })


@app.get("/compare", response_class=HTMLResponse)
def compare_index(request: Request) -> Any:
    conn = connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, ts, prompt, judge_score, local_ms, claude_ms, local_cost, claude_cost "
        "FROM comparisons ORDER BY id DESC LIMIT 50"
    ).fetchall()]
    conn.close()
    for r in rows:
        r["ts_human"] = _fmt_ts(r["ts"])
        r["prompt_short"] = (r["prompt"][:140] + "...") if len(r["prompt"]) > 140 else r["prompt"]
    return templates.TemplateResponse(request, "compare_index.html", {"rows": rows})


@app.post("/compare/run")
def compare_run(prompt: str = Form(...)) -> RedirectResponse:
    res = ab_run(prompt)
    return RedirectResponse(url=f"/compare/{res['id']}", status_code=303)


@app.get("/compare/{cmp_id}", response_class=HTMLResponse)
def compare_one(request: Request, cmp_id: int) -> Any:
    conn = connect()
    row = conn.execute("SELECT * FROM comparisons WHERE id = ?", (cmp_id,)).fetchone()
    conn.close()
    if not row:
        return HTMLResponse(f"<p>comparison {cmp_id} not found</p>", status_code=404)
    r = dict(row)
    r["ts_human"] = _fmt_ts(r["ts"])
    return templates.TemplateResponse(request, "compare_one.html", {"r": r})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard.app:app", host="127.0.0.1", port=4001, reload=False)
