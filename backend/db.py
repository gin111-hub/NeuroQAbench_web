"""Thin SQLite wrapper for the verify-submission backend.

- Opens the db in WAL mode so writers don't block readers (the admin runs
  ad-hoc `sqlite3 ...` queries while uvicorn is live).
- Loads schema.sql on first connection.
- `daily_salt` lives in a sibling file next to the db; rotated at 00:00 UTC.
"""
from __future__ import annotations

import os
import sqlite3
import datetime as dt
import secrets
from pathlib import Path
from typing import Optional

BACKEND_DIR = Path(__file__).resolve().parent
DB_PATH = BACKEND_DIR / "db.sqlite"
SCHEMA_PATH = BACKEND_DIR / "schema.sql"
SALT_PATH = BACKEND_DIR / ".daily_salt"


def connect() -> sqlite3.Connection:
    """Open (and if needed create) the SQLite db."""
    first_time = not DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    if first_time:
        conn.executescript(SCHEMA_PATH.read_text())
    else:
        # idempotent: run the schema anyway so new installs get indexes etc.
        conn.executescript(SCHEMA_PATH.read_text())
    return conn


def _today_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def read_or_rotate_salt() -> str:
    """Return today's salt (bytes-hex). Rotates when stored date != today-UTC.

    Format of SALT_PATH:   <YYYY-MM-DD>\n<hex-salt>\n
    """
    today = _today_utc()
    if SALT_PATH.exists():
        try:
            line_date, line_salt = SALT_PATH.read_text().strip().splitlines()[:2]
            if line_date == today and line_salt:
                return line_salt
        except Exception:
            pass
    new = secrets.token_hex(32)
    SALT_PATH.write_text(f"{today}\n{new}\n")
    try:
        os.chmod(SALT_PATH, 0o600)
    except OSError:
        pass
    return new


def iso_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def upsert_submission(conn: sqlite3.Connection, row: dict) -> None:
    """Insert or replace a submission by (session_token, question_id).

    Uses ON CONFLICT so retries from the same browser overwrite the prior answer
    rather than piling duplicate rows.
    """
    conn.execute(
        """
        INSERT INTO verify_submissions
            (paper_slug, question_id, responses, comment,
             submitted_at, ip_hash, ua_hash, session_token)
        VALUES
            (:paper_slug, :question_id, :responses, :comment,
             :submitted_at, :ip_hash, :ua_hash, :session_token)
        ON CONFLICT(session_token, question_id) DO UPDATE SET
            paper_slug    = excluded.paper_slug,
            responses     = excluded.responses,
            comment       = excluded.comment,
            submitted_at  = excluded.submitted_at,
            ip_hash       = excluded.ip_hash,
            ua_hash       = excluded.ua_hash
        """,
        row,
    )
