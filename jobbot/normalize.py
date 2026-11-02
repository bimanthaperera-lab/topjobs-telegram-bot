"""Keyword normalization."""

from __future__ import annotations

import re

MAX_KEYWORD_LENGTH = 100

_WHITESPACE = re.compile(r"\s+")


def normalize_keyword(raw: str | None) -> str:
    """Trim, lowercase and collapse inner whitespace so equivalent searches share a cache entry.

    Returns an empty string when there is nothing to search for.
    """
    if not raw:
        return ""
    return _WHITESPACE.sub(" ", raw).strip().lower()
