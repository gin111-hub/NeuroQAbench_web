#!/usr/bin/env python3
"""Build the website's data/ products from everything under data/incoming/.

Zero arguments. Scans every subdirectory of data/incoming/ (each is a batch,
typically a symlink to a savaal output dir), joins the MCQ bank with all
per-model answers, and emits:

  * data/verify/papers/<slug>/{meta,questions}.json
  * data/verify/index.json
  * data/hard/hard_index.json
  * data/hard/models.json

Dedup checks that abort the build with a loud error:
  1. Two batch subdirectories resolving to the same filesystem path.
  2. Same question `id` appearing in multiple batches.
  3. Same `source_paper` appearing in multiple batches
     (user rule: each paper lives in exactly one batch).

Design notes:
  * `distractor_rationale` is stripped from both verify and hard outputs.
  * `retrieved_passages` and `evidence_from_source_paper` are stripped from the
    hard output only (verify needs them for the expert's evidence panel).
  * The hard output only includes questions missed by ALL reference models.
"""
from __future__ import annotations

import json
import os
import re
import sys
import datetime as dt
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
INCOMING = DATA / "incoming"
VERIFY_OUT = DATA / "verify"
HARD_OUT = DATA / "hard"
PAPERS_FILE = DATA / "papers.json"

REFERENCE_MODELS = [
    "azure-gpt-5.5",
    "deepseek-v4-flash",
    "glm-5.1",
    "deepseek-v4-pro",
]

# Fields kept in verify's per-question output (distractor_rationale stripped).
VERIFY_KEEP_FIELDS = [
    "id",
    "source_section",
    "bloom_level",
    "knowledge_level",
    "qa_type",
    "question_stem",
    "options",
    "answer_index",
    "evidence_from_source",
    "retrieved_passages",
]

# Fields kept in hard's per-question output (no source passages, no rationales).
HARD_KEEP_FIELDS = [
    "id",
    "source_section",
    "bloom_level",
    "knowledge_level",
    "qa_type",
    "question_stem",
    "options",
    "answer_index",
]

# Pattern for per-model answers file name.
# e.g. all_rounds.combined.deepseek-v4-flash.direct.answers.json
ANSWERS_RE = re.compile(r"^all_rounds\.combined\.(?P<model>.+)\.direct\.answers\.json$")


class BuildError(RuntimeError):
    pass


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_papers_json() -> dict:
    if not PAPERS_FILE.exists():
        die(f"missing {PAPERS_FILE}")
    doc = json.loads(PAPERS_FILE.read_text())
    papers = doc.get("papers", {})
    # key by source_paper_key for fast lookup
    by_key = {}
    for slug, meta in papers.items():
        if meta.get("slug") != slug:
            die(f"papers.json entry for {slug!r} has mismatched slug field {meta.get('slug')!r}")
        key = meta.get("source_paper_key")
        if not key:
            die(f"papers.json entry for {slug!r} missing source_paper_key")
        if key in by_key:
            die(f"papers.json has duplicate source_paper_key {key!r}")
        by_key[key] = meta
    return by_key


def discover_batches() -> list[tuple[str, Path]]:
    """Return (batch_id, path) list, with path resolved via realpath."""
    if not INCOMING.exists():
        die(f"{INCOMING} does not exist")
    out = []
    seen_real = {}
    for child in sorted(INCOMING.iterdir()):
        if child.name.startswith("."):
            continue
        if not child.is_dir():
            # follow symlink; is_dir() follows by default
            continue
        real = child.resolve()
        if real in seen_real:
            die(
                f"batch {child.name!r} and {seen_real[real]!r} both resolve to {real}\n"
                f"  → remove one of them from {INCOMING}"
            )
        seen_real[real] = child.name
        out.append((child.name, real))
    if not out:
        die(f"no batches found under {INCOMING}")
    return out


def load_batch(batch_id: str, path: Path) -> dict:
    """Return {questions: [...], models: {model_name: {qid: is_correct}}, metadata: ...}"""
    combined = path / "all_rounds.combined.json"
    if not combined.exists():
        die(f"batch {batch_id}: missing {combined}")
    doc = json.loads(combined.read_text())
    questions = doc.get("data", [])
    if not questions:
        die(f"batch {batch_id}: no questions in {combined}")

    # discover per-model answers files
    models: dict[str, dict[str, bool]] = {}
    for f in sorted(path.iterdir()):
        m = ANSWERS_RE.match(f.name)
        if not m:
            continue
        model = m.group("model")
        adoc = json.loads(f.read_text())
        qmap = {}
        for item in adoc.get("data", []):
            qid = item["id"]
            # The eval pipeline stores model_prediction as a dict with
            # is_correct / predicted_index / raw_response etc. We trust the
            # pipeline's is_correct flag; missing or unparseable prediction
            # counts as wrong (matches the project's "incorrect" bucket).
            pred = item.get("model_prediction") or {}
            qmap[qid] = bool(pred.get("is_correct"))
        models[model] = qmap

    missing = [m for m in REFERENCE_MODELS if m not in models]
    if missing:
        die(f"batch {batch_id}: missing reference model answers: {missing}\n"
            f"  looked under {path} for all_rounds.combined.<model>.direct.answers.json")

    return {
        "batch_id": batch_id,
        "path": path,
        "questions": questions,
        "models": models,
        "metadata": doc.get("metadata", {}),
    }


def cross_batch_dedup(batches: list[dict]) -> None:
    """Enforce: no question id or source_paper appears in >1 batch."""
    seen_qid: dict[str, str] = {}
    seen_paper: dict[str, str] = {}
    errors = []
    for b in batches:
        for q in b["questions"]:
            qid = q["id"]
            if qid in seen_qid and seen_qid[qid] != b["batch_id"]:
                errors.append(
                    f"  question id {qid!r} appears in both "
                    f"{seen_qid[qid]!r} and {b['batch_id']!r}"
                )
            seen_qid.setdefault(qid, b["batch_id"])
            sp = q.get("source_paper", "")
            if sp in seen_paper and seen_paper[sp] != b["batch_id"]:
                errors.append(
                    f"  source_paper {sp!r} appears in both "
                    f"{seen_paper[sp]!r} and {b['batch_id']!r}"
                )
            seen_paper.setdefault(sp, b["batch_id"])
    # dedup error lines
    errors = sorted(set(errors))
    if errors:
        die("cross-batch dedup failed:\n" + "\n".join(errors[:50])
            + ("\n  ..." if len(errors) > 50 else ""))


def intra_batch_dedup(batches: list[dict]) -> None:
    """Within a batch, question id should be unique. Warn + drop extras."""
    for b in batches:
        seen = set()
        kept = []
        dups = []
        for q in b["questions"]:
            if q["id"] in seen:
                dups.append(q["id"])
                continue
            seen.add(q["id"])
            kept.append(q)
        if dups:
            print(f"  warning: batch {b['batch_id']}: dropped {len(dups)} intra-batch "
                  f"duplicate ids (first few: {dups[:3]})", file=sys.stderr)
        b["questions"] = kept


def write_verify(batches: list[dict], paper_by_key: dict) -> dict:
    """Write per-paper verify JSON. Return {paper_slug: {title, n_questions, batch_id}}."""
    VERIFY_OUT.mkdir(parents=True, exist_ok=True)
    (VERIFY_OUT / "papers").mkdir(exist_ok=True)

    # group questions by paper
    by_paper: dict[str, list[dict]] = defaultdict(list)
    paper_batch: dict[str, str] = {}
    for b in batches:
        for q in b["questions"]:
            sp = q.get("source_paper", "")
            if sp not in paper_by_key:
                die(f"papers.json has no entry for source_paper_key {sp!r}\n"
                    f"  (saw it in batch {b['batch_id']!r})\n"
                    f"  → add it to {PAPERS_FILE}")
            by_paper[sp].append(q)
            paper_batch[sp] = b["batch_id"]

    index: dict[str, dict] = {}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    for sp, qs in by_paper.items():
        meta = paper_by_key[sp]
        slug = meta["slug"]
        pdir = VERIFY_OUT / "papers" / slug
        pdir.mkdir(parents=True, exist_ok=True)

        questions_out = [{k: q.get(k) for k in VERIFY_KEEP_FIELDS} for q in qs]
        doc = {
            "paper_slug": slug,
            "paper_title": meta["title"],
            "source_paper_key": sp,
            "batch_id": paper_batch[sp],
            "generated_at": now,
            "questions": questions_out,
        }
        (pdir / "questions.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2)
        )
        (pdir / "meta.json").write_text(
            json.dumps({"paper_slug": slug, "paper_title": meta["title"],
                        "n_questions": len(qs), "batch_id": paper_batch[sp]},
                       ensure_ascii=False, indent=2)
        )
        index[slug] = {
            "slug": slug,
            "title": meta["title"],
            "n_questions": len(qs),
            "batch_id": paper_batch[sp],
        }

    # verify index: sorted by title
    index_doc = {
        "generated_at": now,
        "papers": sorted(index.values(), key=lambda e: e["title"].lower()),
    }
    (VERIFY_OUT / "index.json").write_text(
        json.dumps(index_doc, ensure_ascii=False, indent=2)
    )
    return index


def write_hard(batches: list[dict], paper_by_key: dict) -> int:
    """Write hard_index.json. Returns the number of hard questions emitted."""
    HARD_OUT.mkdir(parents=True, exist_ok=True)

    all_models: set[str] = set()
    for b in batches:
        all_models.update(b["models"].keys())
    all_models_sorted = sorted(all_models)
    ref_set = set(REFERENCE_MODELS)
    missing_ref = ref_set - all_models
    if missing_ref:
        die(f"reference models missing from aggregated model set: {missing_ref}")

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    hard_questions = []
    for b in batches:
        model_maps = b["models"]
        for q in b["questions"]:
            qid = q["id"]
            # which models got it wrong?
            wrong_all = sorted(
                m for m, mp in model_maps.items() if mp.get(qid) is False
            )
            wrong_ref = sorted(set(wrong_all) & ref_set)
            if set(wrong_ref) != ref_set:
                continue  # not a hard question
            sp = q.get("source_paper", "")
            meta = paper_by_key[sp]
            card = {k: q.get(k) for k in HARD_KEEP_FIELDS}
            card["paper_slug"] = meta["slug"]
            card["paper_title"] = meta["title"]
            card["wrong_by_all_models"] = wrong_all
            card["wrong_by_reference"] = wrong_ref
            hard_questions.append(card)

    # stable sort: by paper then by id
    hard_questions.sort(key=lambda c: (c["paper_slug"], c["id"]))

    hard_doc = {
        "generated_at": now,
        "reference_models": REFERENCE_MODELS,
        "all_models": all_models_sorted,
        "n_total_questions": sum(len(b["questions"]) for b in batches),
        "questions": hard_questions,
    }
    (HARD_OUT / "hard_index.json").write_text(
        json.dumps(hard_doc, ensure_ascii=False, indent=2)
    )
    (HARD_OUT / "models.json").write_text(
        json.dumps({"all_models": all_models_sorted,
                    "reference_models": REFERENCE_MODELS},
                   ensure_ascii=False, indent=2)
    )
    return len(hard_questions)


def main() -> int:
    paper_by_key = load_papers_json()
    print(f"loaded papers.json: {len(paper_by_key)} paper entries")

    batches_raw = discover_batches()
    print(f"found {len(batches_raw)} batches under {INCOMING}:")
    for bid, path in batches_raw:
        print(f"  {bid} -> {path}")

    batches = [load_batch(bid, path) for bid, path in batches_raw]
    intra_batch_dedup(batches)
    cross_batch_dedup(batches)

    verify_index = write_verify(batches, paper_by_key)
    n_hard = write_hard(batches, paper_by_key)

    n_papers = len(verify_index)
    n_q = sum(e["n_questions"] for e in verify_index.values())
    print(f"\n{len(batches)} batches, {n_papers} papers, {n_q} questions, "
          f"{n_hard} hard-questions")
    print(f"wrote: {VERIFY_OUT}/index.json + {VERIFY_OUT}/papers/*/")
    print(f"wrote: {HARD_OUT}/hard_index.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
