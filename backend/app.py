"""FastAPI backend for QAbench expert-verify submissions.

The questionnaire structure lives in data/questionnaire.json (single source of
truth; verify.html and scripts/show_submissions.py read the same file).
Responses are stored as a JSON blob so adding/removing dimensions needs no DB
migration.

Guards against the obvious abuse paths:
- Requires `X-QAbench-Client: 1` header (keeps naive scrapers out)
- Validates (paper_slug, question_id) against an in-memory index built from
  data/verify/papers/*/questions.json at startup
- Validates responses against questionnaire.json (keys and scale range)
- Rate-limits by hashed IP (60/min) via slowapi
- Hashes IP + UA with a daily-rotating salt before storing
- Issues an anonymous session cookie that scopes per-browser dedup
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Optional, Any

from fastapi import FastAPI, Request, Response, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, constr
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import db as _db

# ---------------------------------------------------------------- paths
BACKEND_DIR = Path(__file__).resolve().parent
ROOT = BACKEND_DIR.parent
VERIFY_DIR = ROOT / "data" / "verify" / "papers"
QUESTIONNAIRE_PATH = ROOT / "data" / "questionnaire.json"
FRONTEND_DIR = ROOT / "frontend"
DATA_DIR = ROOT / "data"

COOKIE_NAME = "qb_sid"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
CLIENT_HEADER_VALUE = "1"

# ---------------------------------------------------------------- app
limiter = Limiter(key_func=get_remote_address, default_limits=[])
app = FastAPI(title="QAbench verify API", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(CORSMiddleware, allow_origins=[], allow_methods=["POST"], allow_headers=["*"])

# ---------------------------------------------------------------- loaded state
VALID: set[tuple[str, str]] = set()                 # (slug, qid) pairs
QUESTIONNAIRE: dict[str, Any] = {}                  # the whole config doc
DIM_KEYS: list[str] = []
SCALE_MIN: int = 1
SCALE_MAX: int = 5
COMMENT_MAX: int = 2000


def _load_valid_index() -> None:
    VALID.clear()
    if not VERIFY_DIR.exists():
        return
    for p in VERIFY_DIR.glob("*/questions.json"):
        doc = json.loads(p.read_text())
        slug = doc.get("paper_slug") or p.parent.name
        for q in doc.get("questions", []):
            VALID.add((slug, q["id"]))


def _load_questionnaire() -> None:
    global QUESTIONNAIRE, DIM_KEYS, SCALE_MIN, SCALE_MAX, COMMENT_MAX
    if not QUESTIONNAIRE_PATH.exists():
        raise RuntimeError(f"missing {QUESTIONNAIRE_PATH}")
    QUESTIONNAIRE = json.loads(QUESTIONNAIRE_PATH.read_text())
    DIM_KEYS = [d["key"] for d in QUESTIONNAIRE.get("dimensions", [])]
    scale = QUESTIONNAIRE.get("scale", {})
    SCALE_MIN = int(scale.get("min", 1))
    SCALE_MAX = int(scale.get("max", 5))
    COMMENT_MAX = int(QUESTIONNAIRE.get("comment", {}).get("max_length", 2000))
    if not DIM_KEYS:
        raise RuntimeError("questionnaire.json has no dimensions")


# ---------------------------------------------------------------- startup
_CONN = None
_SALT = None


@app.on_event("startup")
def _startup() -> None:
    global _CONN, _SALT
    _CONN = _db.connect()
    _SALT = _db.read_or_rotate_salt()
    _load_valid_index()
    _load_questionnaire()
    print(f"[qabench] loaded {len(VALID)} valid (slug, qid) pairs, "
          f"questionnaire has {len(DIM_KEYS)} dimensions (scale {SCALE_MIN}-{SCALE_MAX})")


# ---------------------------------------------------------------- request body
class SubmitBody(BaseModel):
    paper_slug: constr(strip_whitespace=True, min_length=1, max_length=80)
    question_id: constr(strip_whitespace=True, min_length=1, max_length=200)
    responses: dict[str, int]
    comment: Optional[str] = None


def _validate_responses(resp: dict[str, int]) -> None:
    if set(resp.keys()) != set(DIM_KEYS):
        missing = set(DIM_KEYS) - set(resp.keys())
        extra = set(resp.keys()) - set(DIM_KEYS)
        raise HTTPException(status_code=400,
                            detail=f"responses keys mismatch (missing={sorted(missing)}, extra={sorted(extra)})")
    for k, v in resp.items():
        if not isinstance(v, int) or v < SCALE_MIN or v > SCALE_MAX:
            raise HTTPException(status_code=400,
                                detail=f"responses[{k}]={v} out of range [{SCALE_MIN},{SCALE_MAX}]")


# ---------------------------------------------------------------- helpers
def _hash(value: str) -> str:
    return hashlib.sha256(f"{value}|{_SALT}".encode("utf-8")).hexdigest()


def _get_or_make_sid(request: Request, response: Response) -> str:
    sid = request.cookies.get(COOKIE_NAME)
    if sid and len(sid) <= 64:
        return sid
    sid = uuid.uuid4().hex
    response.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return sid


# ---------------------------------------------------------------- endpoints
@app.post("/api/verify/submit")
@limiter.limit("60/minute")
def submit(
    request: Request,
    response: Response,
    body: SubmitBody,
    x_qabench_client: Optional[str] = Header(None, alias="X-QAbench-Client"),
):
    if x_qabench_client != CLIENT_HEADER_VALUE:
        raise HTTPException(status_code=400, detail="missing X-QAbench-Client header")
    if (body.paper_slug, body.question_id) not in VALID:
        raise HTTPException(status_code=404, detail="unknown (paper_slug, question_id)")
    _validate_responses(body.responses)
    if body.comment is not None and len(body.comment) > COMMENT_MAX:
        raise HTTPException(status_code=400, detail=f"comment too long (>{COMMENT_MAX})")

    # salt may have rotated; refresh so hashes stay stable within a day
    global _SALT
    _SALT = _db.read_or_rotate_salt()

    sid = _get_or_make_sid(request, response)
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")

    row = {
        "paper_slug": body.paper_slug,
        "question_id": body.question_id,
        "responses": json.dumps(body.responses, ensure_ascii=False, sort_keys=True),
        "comment": body.comment,
        "submitted_at": _db.iso_utc(),
        "ip_hash": _hash(ip),
        "ua_hash": _hash(ua),
        "session_token": sid,
    }
    _db.upsert_submission(_CONN, row)
    return {"status": "ok"}


@app.get("/api/health")
def health():
    return {"status": "ok",
            "n_valid_questions": len(VALID),
            "questionnaire_dimensions": DIM_KEYS,
            "scale": [SCALE_MIN, SCALE_MAX]}


# ---------------------------------------------------------------- static files
# Mounted LAST so API routes above take precedence. In production nginx
# serves these directly; the mount is a harmless no-op there (nginx intercepts
# before uvicorn sees the request). Lets `uvicorn backend.app:app` be a
# single-port dev setup: open http://host:port/verify.html, /api works
# same-origin, no CORS.
if DATA_DIR.exists():
    app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
