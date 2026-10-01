"""Shared slug helpers for build.py.

Slugs are decided manually in `data/papers.json` (so the website keeps stable
URLs even if the raw source_paper string changes). This module only exposes
tiny helpers; it does NOT auto-slugify paper names.
"""
from __future__ import annotations

import re


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def is_valid_slug(s: str) -> bool:
    """Slug must be lowercase alnum + dash, 1-80 chars, no leading/trailing dash."""
    if not s or len(s) > 80:
        return False
    if s[0] == "-" or s[-1] == "-":
        return False
    return bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", s))


def normalise(s: str) -> str:
    """Lowercase + replace non-alnum runs with single dash. For comparison only;
    not used to pick final slugs — those live in papers.json."""
    s = s.strip().lower()
    s = _SLUG_RE.sub("-", s)
    return s.strip("-")
