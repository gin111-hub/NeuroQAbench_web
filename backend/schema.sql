-- QAbench verify submissions schema (configurable-questionnaire version).
--
-- Each row = one expert's response to one question. The questionnaire structure
-- lives in data/questionnaire.json; `responses` holds a JSON object keyed by
-- dimension (e.g. {"q1_clarity":5,"q2_answer":4,...}). Changing the
-- questionnaire does NOT require a schema migration — scripts/show_submissions.py
-- just reads the current config and iterates over whatever dimensions it finds.
--
-- Query recipes (sqlite3 backend/db.sqlite):
--
--   -- raw rows for one paper
--   SELECT * FROM verify_submissions WHERE paper_slug='nsd-dataset';
--
--   -- extract a single dimension inline via json_extract
--   SELECT question_id,
--          json_extract(responses, '$.q4_overall') AS q4
--     FROM verify_submissions
--     WHERE json_extract(responses, '$.q4_overall') <= 3;
--
-- For aggregated views the companion script is scripts/show_submissions.py.

CREATE TABLE IF NOT EXISTS verify_submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_slug      TEXT NOT NULL,
    question_id     TEXT NOT NULL,
    responses       TEXT NOT NULL,                         -- JSON blob
    comment         TEXT,
    submitted_at    TEXT NOT NULL,                         -- ISO8601 UTC
    ip_hash         TEXT NOT NULL,                         -- SHA256(ip + daily_salt)
    ua_hash         TEXT NOT NULL,                         -- SHA256(ua + daily_salt)
    session_token   TEXT NOT NULL                          -- UUID in SameSite=Lax HttpOnly cookie
);

CREATE INDEX  IF NOT EXISTS idx_qid        ON verify_submissions(question_id);
CREATE INDEX  IF NOT EXISTS idx_slug       ON verify_submissions(paper_slug);
CREATE UNIQUE INDEX IF NOT EXISTS uq_session_q
  ON verify_submissions(session_token, question_id);
