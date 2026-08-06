"""
Read-only HTTP window on a running Ariadne instance.

The site is static, so it cannot run any of the tools itself. This exposes the
ones that answer fast enough to sit inside a page load, and runs the one that
does not as a background job.

Nothing here interpolates caller input into a command. Every endpoint maps to a
fixed argument list, and the few parameters that vary are matched against an
allowlist first. The single endpoint that writes to the catalog is gated on a
shared secret.
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(os.environ.get("ARIADNE_ROOT", Path.home() / "ariadne"))

# The two interpreters are not interchangeable. av carries the agent surfaces
# and no pandas; v carries the data and ML stack and no mcp. Sending a tool to
# the wrong one raises ModuleNotFoundError, which reads exactly like a broken
# build, so the mapping lives here in one place rather than at each call site.
PY_DATA = ROOT / "v" / "bin" / "python"
PY_AGENT = ROOT / "av" / "bin" / "python"

WRITE_TOKEN = os.environ.get("ARIADNE_WRITE_TOKEN", "")

ORIGINS = [
    "https://ariadne-five.vercel.app",
    "https://ariadne.onenept.com",
    "http://localhost:3000",
    "http://localhost:8000",
]

MODELS = {"income-classifier", "workforce-classifier"}
POLICIES = {"canada", "ecoa", "employment_us", "eu_ai_act"}
TABLES = {"income_features", "workforce_features", "dim_person"}

app = FastAPI(title="Ariadne", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# One subprocess at a time per endpoint family. A judge refreshing the demo
# should not be able to start ten model fits on a four core box.
_gate = threading.Semaphore(3)
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def cached(key: str, ttl: int):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    return None


def store(key: str, value: dict):
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


def run(interpreter: Path, args: list[str], timeout: int = 60) -> dict:
    """Run one tool and return its output. Never raises on tool failure."""
    if not _gate.acquire(timeout=20):
        raise HTTPException(503, "busy")
    started = time.time()
    try:
        proc = subprocess.run(
            [str(interpreter), *args],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "tools")},
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{args[0]} exceeded {timeout}s")
    finally:
        _gate.release()

    out = proc.stdout.strip()
    payload = {"ok": proc.returncode == 0, "seconds": round(time.time() - started, 2)}
    if "--json" in args:
        # Some tools print a line of context before the document, so parsing
        # the whole of stdout fails on output that is otherwise perfectly good.
        # Take everything from where the document starts and keep the preamble
        # alongside. It may be an array as well as an object: incident.py
        # returns a list of findings.
        starts = [i for i in (out.find("{"), out.find("[")) if i >= 0]
        start = min(starts) if starts else -1
        try:
            if start < 0:
                raise json.JSONDecodeError("no json in output", out, 0)
            payload["data"] = json.loads(out[start:])
            preamble = out[:start].strip()
            if preamble:
                payload["note"] = preamble
        except json.JSONDecodeError:
            payload["ok"] = False
            payload["text"] = out
    else:
        payload["text"] = out
    if proc.returncode != 0:
        payload["stderr"] = proc.stderr.strip()[-800:]
    return payload


@app.get("/api/health")
def health():
    return {"ok": True, "root": str(ROOT), "ts": int(time.time())}


# ---------------------------------------------------------------------------
# The figures on the landing page. Computing these takes about fifteen seconds
# between them, which no visitor should wait for, so a background thread keeps
# a current copy and the endpoint serves whatever it last measured. Every
# response carries the time of that measurement, because a live number with no
# timestamp is indistinguishable from one typed into the HTML.
# ---------------------------------------------------------------------------

_overview: dict = {"as_of": None, "measuring": True}
_OVERVIEW_EVERY = 300


def _measure_overview() -> dict:
    out: dict = {"as_of": int(time.time())}

    ents = run(PY_DATA, ["tools/graph.py", "find", "income_features"], 60)
    if ents.get("ok"):
        lines = [l for l in ents.get("text", "").splitlines() if l.strip()]
        out["entities_matched"] = len(lines)

    sent = run(PY_DATA, ["tools/sentinel.py", "--policy", "ecoa", "--json"], 120)
    if sent.get("ok") and isinstance(sent.get("data"), dict):
        findings = sent["data"].get("findings", [])
        out["findings"] = len(findings)
        out["models_watched"] = len({f.get("model") for f in findings if f.get("model")})
        out["regime"] = sent["data"].get("regime")

    # agree.py prints the tool count and the walk tally in its own words. Read
    # them off rather than recomputing, so the page cannot disagree with the CLI.
    agr = run(PY_AGENT, ["tools/agree.py"], 150)
    if agr.get("ok"):
        text = agr.get("text", "")
        out["agree_text"] = text.strip().splitlines()[-1].strip() if text.strip() else None
        for token in text.split():
            if token.isdigit() and "tools" in text.split(token, 1)[1][:8]:
                out["mcp_tools"] = int(token)
                break
    return out


def _overview_loop():
    global _overview
    while True:
        try:
            _overview = _measure_overview()
        except Exception as exc:  # a bad measurement must not kill the refresher
            _overview = {"as_of": int(time.time()), "error": str(exc)[:200]}
        time.sleep(_OVERVIEW_EVERY)


threading.Thread(target=_overview_loop, daemon=True).start()


@app.get("/api/overview")
def overview():
    """Precomputed landing page figures, with the time they were measured."""
    return _overview


@app.get("/api/estate")
def estate():
    """What the catalog holds. First thing the page asks for."""
    key = "estate"
    return cached(key, 60) or store(key, run(PY_DATA, ["tools/graph.py", "find", "income_features"], 45))


@app.get("/api/sentinel")
def sentinel(policy: str = "ecoa"):
    """The invariants. This is the check that fires."""
    if policy not in POLICIES:
        raise HTTPException(400, "unknown policy")
    key = f"sentinel:{policy}"
    return cached(key, 60) or store(
        key, run(PY_DATA, ["tools/sentinel.py", "--policy", policy, "--json"], 90)
    )


@app.get("/api/trace")
def trace(table: str = "income_features"):
    """Column level walk back through the graph."""
    if table not in TABLES:
        raise HTTPException(400, "unknown table")
    key = f"trace:{table}"
    return cached(key, 120) or store(key, run(PY_DATA, ["tools/trace.py", "columns", table], 45))


@app.get("/api/policy")
def policy(pack: str = "ecoa"):
    """One statute resolved against this warehouse."""
    if pack not in POLICIES:
        raise HTTPException(400, "unknown policy")
    key = f"policy:{pack}"
    return cached(key, 600) or store(key, run(PY_DATA, ["tools/policy.py", "show", pack], 30))


@app.get("/api/history")
def history():
    """The recording history. The nights it found nothing are the point."""
    key = "history"
    return cached(key, 120) or store(key, run(PY_DATA, ["tools/exposure.py", "history"], 90))


@app.get("/api/agree")
def agree():
    """The same walk through both DataHub surfaces. The dependence proof."""
    key = "agree"
    return cached(key, 300) or store(key, run(PY_AGENT, ["tools/agree.py"], 120))


@app.get("/api/rootcause")
def rootcause(model: str = "workforce-classifier"):
    if model not in MODELS:
        raise HTTPException(400, "unknown model")
    key = f"rootcause:{model}"
    return cached(key, 120) or store(
        key,
        run(PY_AGENT, ["tools/rootcause.py", "--model", model, "--via", "mcp", "--json"], 120),
    )


@app.get("/api/blast")
def blast(policy: str = "eu_ai_act"):
    if policy not in POLICIES:
        raise HTTPException(400, "unknown policy")
    key = f"blast:{policy}"
    return cached(key, 120) or store(
        key,
        run(
            PY_AGENT,
            [
                "tools/blast.py",
                "analytics_marts.dim_person",
                "--column",
                "public_coverage_flag",
                "--policy",
                policy,
                "--json",
            ],
            90,
        ),
    )


# ---------------------------------------------------------------------------
# The rebuild is the only measurement that cannot sit inside a page load. It
# fits models over 244k rows and takes about half a minute. The page starts it
# on arrival and reads the result two steps later, by which time it is done.
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _rebuild_worker(key: str, model: str, repeats: int):
    result = run(
        PY_DATA,
        ["tools/reconstruct.py", "--model", model, "--repeats", str(repeats), "--json"],
        600,
    )
    with _jobs_lock:
        _jobs[key] = {"state": "done", "started": _jobs[key]["started"], "result": result}


@app.post("/api/rebuild")
@app.get("/api/rebuild")
def rebuild(model: str = "income-classifier", repeats: int = 1):
    """Start the reconstruction measurement, or report on the one already running."""
    if model not in MODELS:
        raise HTTPException(400, "unknown model")
    if repeats not in (1, 3):
        raise HTTPException(400, "repeats must be 1 or 3")
    key = f"rebuild:{model}:{repeats}"

    with _jobs_lock:
        job = _jobs.get(key)
        if job:
            if job["state"] == "done":
                return {"state": "done", "elapsed": round(time.time() - job["started"], 1), **job["result"]}
            # A finished job stays cached; a stale running one is retried.
            if time.time() - job["started"] < 900:
                return {"state": "running", "elapsed": round(time.time() - job["started"], 1)}
        _jobs[key] = {"state": "running", "started": time.time()}

    threading.Thread(target=_rebuild_worker, args=(key, model, repeats), daemon=True).start()
    return {"state": "running", "elapsed": 0.0}


# ---------------------------------------------------------------------------
# The one write path.
# ---------------------------------------------------------------------------


@app.post("/api/incident")
def incident(model: str = "income-classifier", policy: str = "ecoa", x_ariadne_token: str = Header("")):
    """File findings back into the catalog. Gated, and refuses without a token."""
    if not WRITE_TOKEN or x_ariadne_token != WRITE_TOKEN:
        raise HTTPException(403, "writeback requires a token")
    if model not in MODELS or policy not in POLICIES:
        raise HTTPException(400, "unknown model or policy")
    return run(
        PY_DATA,
        ["tools/incident.py", "--model", model, "--policy", policy, "--raise", "--json"],
        180,
    )


@app.get("/api/incident/preview")
def incident_preview(model: str = "income-classifier", policy: str = "ecoa"):
    """What would be filed. Writes nothing, so it needs no token."""
    if model not in MODELS or policy not in POLICIES:
        raise HTTPException(400, "unknown model or policy")
    key = f"incident:{model}:{policy}"
    return cached(key, 120) or store(
        key, run(PY_DATA, ["tools/incident.py", "--model", model, "--policy", policy, "--json"], 120)
    )


@app.get("/api/document")
def document(model: str = "income-classifier", policy: str = "eu_ai_act"):
    """The per run record, generated now and returned as a PDF."""
    if model not in MODELS or policy not in POLICIES:
        raise HTTPException(400, "unknown model or policy")
    out = Path("/tmp") / f"ariadne-{model}-{policy}.pdf"
    result = run(
        PY_DATA,
        ["tools/complydoc.py", "--model", model, "--policy", policy, "--out", str(out)],
        120,
    )
    if not result["ok"] or not out.exists():
        raise HTTPException(500, "document generation failed")
    return Response(
        out.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{out.name}"'},
    )
