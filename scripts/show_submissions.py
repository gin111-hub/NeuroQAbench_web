#!/usr/bin/env python3
"""Dump verify_submissions to text files VSCode can open directly.

Config-driven: reads data/questionnaire.json for dimension list, scale, and
bad_threshold. Changing the questionnaire does NOT require editing this file.

Zero arguments. Writes to data/exports/:
  * submissions.csv     — one row per submission (raw dump)
  * by_question.txt     — per-question aggregated summary (plain text)
  * by_paper.txt        — per-paper rollup (plain text)
  * summary.txt         — overall quality snapshot (printed to stdout too)

"bad" is defined by questionnaire.json → scale.bad_threshold + direction:
- higher-is-better + bad_threshold=3 → score <= 3 counts as "bad"
- lower-is-better  + bad_threshold=3 → score >= 3 counts as "bad"
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB = ROOT / "backend" / "db.sqlite"
OUT_DIR = ROOT / "data" / "exports"
QUESTIONNAIRE_PATH = ROOT / "data" / "questionnaire.json"


# ---------------------------------------------------------------- config load
def _load_config() -> dict:
    if not QUESTIONNAIRE_PATH.exists():
        raise SystemExit(f"missing {QUESTIONNAIRE_PATH}")
    return json.loads(QUESTIONNAIRE_PATH.read_text())


def _is_bad(score: int, scale: dict) -> bool:
    """True if `score` crosses the bad_threshold in the configured direction."""
    th = scale.get("bad_threshold")
    if th is None:
        return False
    if scale.get("direction") == "higher-is-better":
        return score <= th        # low score = bad
    return score >= th            # high score = bad


# ---------------------------------------------------------------- data access
def _fetch_rows() -> list[dict]:
    if not DB.exists():
        return []
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows_raw = list(conn.execute(
            "SELECT * FROM verify_submissions ORDER BY paper_slug, question_id, submitted_at"
        ))
    except sqlite3.OperationalError:
        return []
    rows = []
    for r in rows_raw:
        d = dict(r)
        try:
            d["responses_parsed"] = json.loads(d.get("responses") or "{}")
        except json.JSONDecodeError:
            d["responses_parsed"] = {}
        rows.append(d)
    conn.close()
    return rows


# ---------------------------------------------------------------- writers
def _write_csv(rows: list[dict], dim_keys: list[str], path: Path) -> None:
    base = ["id", "paper_slug", "question_id"]
    tail = ["comment", "submitted_at", "ip_hash", "ua_hash", "session_token"]
    cols = base + dim_keys + tail
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {c: r.get(c) for c in base + tail}
            for k in dim_keys:
                flat[k] = r["responses_parsed"].get(k)
            w.writerow(flat)


def _render_votes_bar(votes: dict[int, int], scale_min: int, scale_max: int) -> str:
    total = sum(votes.values()) or 1
    parts = []
    for n in range(scale_min, scale_max + 1):
        v = votes.get(n, 0)
        bar = "█" * round(v / total * 10)
        parts.append(f"{n:>2d}: {v:2d} {bar}")
    return "   ".join(parts)


def _summarise_question(qrows: list[dict], config: dict) -> dict:
    scale = config["scale"]
    dims = [d["key"] for d in config["dimensions"]]
    out = {"n": len(qrows), "comments": [r["comment"] for r in qrows if r.get("comment")]}
    for k in dims:
        vals = [r["responses_parsed"].get(k) for r in qrows]
        vals = [v for v in vals if isinstance(v, int)]
        if not vals:
            out[k] = {"mean": None, "votes": {}, "bad_rate": None, "n": 0}
            continue
        votes: dict[int, int] = defaultdict(int)
        for v in vals:
            votes[v] += 1
        bad_n = sum(1 for v in vals if _is_bad(v, scale))
        out[k] = {
            "mean": round(mean(vals), 2),
            "votes": dict(votes),
            "bad_rate": round(bad_n / len(vals), 2),
            "n": len(vals),
        }
    # headline "any-dim-bad" per submission
    any_bad = sum(
        1 for r in qrows
        if any(_is_bad(r["responses_parsed"].get(k, 999), scale) for k in dims
               if isinstance(r["responses_parsed"].get(k), int))
    )
    out["any_dim_bad_rate"] = round(any_bad / len(qrows), 2)
    return out


def _write_by_question(rows: list[dict], config: dict, path: Path, also_stdout: bool) -> None:
    scale = config["scale"]
    dims = config["dimensions"]
    dim_keys = [d["key"] for d in dims]
    prompts = {d["key"]: d["prompt"] for d in dims}
    th = scale.get("bad_threshold")
    direction = "<= " if scale.get("direction") == "higher-is-better" else ">= "
    bad_desc = f"score {direction}{th}"

    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        buckets[(r["paper_slug"], r["question_id"])].append(r)
    summaries = {k: _summarise_question(v, config) for k, v in buckets.items()}

    lines = []
    lines.append(f"# per-question summary — {len(buckets)} questions, "
                 f"{len(rows)} total submissions")
    lines.append(f"# scale {scale['min']}-{scale['max']}, "
                 f"direction={scale.get('direction')}, "
                 f'"bad" = {bad_desc}')
    lines.append("")

    # sort by any_dim_bad_rate desc so the most worrying questions bubble up
    for (slug, qid), s in sorted(summaries.items(),
                                 key=lambda kv: (-kv[1]["any_dim_bad_rate"], kv[0][0])):
        lines.append(f"── {slug} · {qid}")
        lines.append(f"   n_submissions     = {s['n']}")
        lines.append(f"   any_dim_bad_rate  = {s['any_dim_bad_rate']}")
        for k in dim_keys:
            d = s[k]
            if d["n"] == 0:
                lines.append(f"   {k:16s} (no data)")
                continue
            lines.append(f"   {k:16s} mean={d['mean']}  bad_rate={d['bad_rate']}  "
                         f"— {prompts[k]}")
            lines.append(f"           votes: {_render_votes_bar(d['votes'], scale['min'], scale['max'])}")
        if s["comments"]:
            lines.append(f"   comments ({len(s['comments'])}):")
            for c in s["comments"]:
                lines.append(f"     · {c.strip()[:300]}")
        lines.append("")

    text = "\n".join(lines)
    path.write_text(text)
    if also_stdout:
        print(text)


def _write_by_paper(rows: list[dict], config: dict, path: Path) -> None:
    scale = config["scale"]
    dim_keys = [d["key"] for d in config["dimensions"]]
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[r["paper_slug"]].append(r)

    lines = [f"# per-paper rollup — {len(buckets)} papers\n"]
    for slug, rs in sorted(buckets.items()):
        qids = {r["question_id"] for r in rs}
        lines.append(f"── {slug}")
        lines.append(f"   n_submissions = {len(rs)}")
        lines.append(f"   n_questions   = {len(qids)}")
        for k in dim_keys:
            vals = [r["responses_parsed"].get(k) for r in rs]
            vals = [v for v in vals if isinstance(v, int)]
            if not vals:
                continue
            bad_n = sum(1 for v in vals if _is_bad(v, scale))
            lines.append(f"   avg {k:14s} = {round(mean(vals), 2)}   "
                         f"bad_rate = {round(bad_n / len(vals), 2)}")
        lines.append("")
    path.write_text("\n".join(lines))


def _write_summary(rows: list[dict], config: dict, path: Path, also_stdout: bool) -> None:
    scale = config["scale"]
    dims = config["dimensions"]
    th = scale.get("bad_threshold")
    direction = "<=" if scale.get("direction") == "higher-is-better" else ">="

    # unique questions grouped by (slug, qid)
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        buckets[(r["paper_slug"], r["question_id"])].append(r)

    lines = []
    lines.append("# overall summary")
    lines.append(f"n_submissions = {len(rows)}")
    lines.append(f"n_questions_reviewed = {len(buckets)}")
    lines.append(f"n_papers = {len({r['paper_slug'] for r in rows})}")
    lines.append("")
    lines.append(f'## 各维度得分 {direction} {th} 的题目数量')
    lines.append('(a question counts if ≥1 expert gave it a "bad" score on that dim)')
    for d in dims:
        n_q_bad = 0
        for (slug, qid), qrows in buckets.items():
            if any(_is_bad(r["responses_parsed"].get(d["key"], 999), scale)
                   for r in qrows
                   if isinstance(r["responses_parsed"].get(d["key"]), int)):
                n_q_bad += 1
        lines.append(f"  {d['key']:16s} : {n_q_bad} / {len(buckets)} questions "
                     f"— {d['prompt']}")
    lines.append("")

    text = "\n".join(lines)
    path.write_text(text)
    if also_stdout:
        print(text)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    config = _load_config()
    dim_keys = [d["key"] for d in config["dimensions"]]

    rows = _fetch_rows()
    if not rows:
        msg = f"(no submissions yet in {DB})"
        print(msg)
        (OUT_DIR / "submissions.csv").write_text("")
        (OUT_DIR / "by_question.txt").write_text(msg + "\n")
        (OUT_DIR / "by_paper.txt").write_text(msg + "\n")
        (OUT_DIR / "summary.txt").write_text(msg + "\n")
        return 0

    _write_csv(rows, dim_keys, OUT_DIR / "submissions.csv")
    _write_by_paper(rows, config, OUT_DIR / "by_paper.txt")
    _write_by_question(rows, config, OUT_DIR / "by_question.txt", also_stdout=False)
    _write_summary(rows, config, OUT_DIR / "summary.txt", also_stdout=True)
    print(f"\nwrote: {OUT_DIR}/submissions.csv")
    print(f"wrote: {OUT_DIR}/by_question.txt")
    print(f"wrote: {OUT_DIR}/by_paper.txt")
    print(f"wrote: {OUT_DIR}/summary.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
