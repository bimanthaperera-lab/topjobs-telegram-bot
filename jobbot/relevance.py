"""Rank a site's results by how well each job title matches the user's keyword.

Job sites match loosely: TopJobs returns any job containing *any* of the words
(466 results for "software engineer", only 8 with both words in the title).
This module keeps real matches, best first, and drops the rest.

Rules
- The keyword is split into words; filler words ("and", "jobs", ...) are ignored.
- The *job word* is the last word ("software ENGINEER", "web DEVELOPER"). A title
  must contain it, or one of its synonyms, to be shown at all.
- Tiers, best first:
    1. the job word and every other word
    2. the job word and other words worth at least half of the other words'
       weight (rare words weigh more: "software" outweighs "senior")
    3. the job word only - used only when tiers 1 and 2 are empty
       ("web developer" has no such titles, so plain "Developer" jobs are shown)
- Inside a tier: direct matches before synonyms, then the site's own order
  (newest first).
- Word forms: engineer matches engineers / engineering, but only through real
  endings (-s, -ing, -er, ...), so "web" doesn't match "Weboda".
- Strict (category buttons): tier 1 only, so "technical support" never falls back
  to every "Support" title, customer support included.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence

from .models import Job

SYNONYM_CREDIT = 0.8
TIER2_MIN_SHARE = 0.5

_WORD = re.compile(r"[a-z0-9+#]+")

STOP_WORDS = frozenset(
    {"a", "an", "and", "or", "the", "of", "in", "for", "to", "at", "with", "job", "jobs",
     "vacancy", "vacancies", "position", "positions", "work", "role"}
)

# Endings that turn one form of a word into another (engineer -> engineering).
_ENDINGS = ("s", "es", "ing", "ings", "er", "ers", "ed", "or", "ors", "ment", "ments", "ion", "ions", "al")

# One-way: a search for the key also accepts these words.
# ("software engineer" should find developers; "web developer" should not find civil engineers.)
SYNONYMS: dict[str, frozenset[str]] = {
    "engineer": frozenset({"developer", "programmer"}),
    "developer": frozenset({"programmer"}),
    "programmer": frozenset({"developer"}),
    "accountant": frozenset({"accounting", "accounts"}),
    "qa": frozenset({"tester"}),
    "tester": frozenset({"qa"}),
    "driver": frozenset({"chauffeur"}),
    "nurse": frozenset({"nursing"}),
    "teacher": frozenset({"tutor", "lecturer", "instructor"}),
    "lecturer": frozenset({"teacher", "instructor"}),
    "salesman": frozenset({"sales"}),
}


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def keyword_terms(keyword: str) -> list[str]:
    """Distinct meaningful words of the keyword, in order."""
    seen: list[str] = []
    for word in words(keyword):
        if word not in STOP_WORDS and word not in seen:
            seen.append(word)
    return seen


def word_matches(title_word: str, term: str) -> bool:
    """Same word, or the same word with a common ending. Short terms (it, qa) must match exactly."""
    if title_word == term:
        return True
    if len(term) < 3 or len(title_word) < 3:
        return False
    if title_word.startswith(term):
        return title_word[len(term):] in _ENDINGS
    if term.startswith(title_word):
        return term[len(title_word):] in _ENDINGS
    return False


def _strength(title_words: Sequence[str], term: str) -> float:
    if any(word_matches(w, term) for w in title_words):
        return 1.0
    if any(word_matches(w, syn) for syn in SYNONYMS.get(term, ()) for w in title_words):
        return SYNONYM_CREDIT
    return 0.0


def rank_jobs(jobs: Sequence[Job], keyword: str, *, strict: bool = False) -> list[Job]:
    """Relevant jobs only, best match first (site order kept within equal scores).
    strict: only titles with every word of the keyword (or its synonyms)."""
    terms = keyword_terms(keyword)
    if not terms or not jobs:
        return list(jobs)

    strengths = [[_strength(words(job.title), term) for term in terms] for job in jobs]
    hits = [sum(1 for row in strengths if row[i] > 0) for i in range(len(terms))]
    # Rare words carry more meaning (IDF over this result set).
    weights = [math.log((len(jobs) + 1) / (h + 1)) + 1 for h in hits]

    # The job word is the last word, unless it appears in no title at all
    # (e.g. a place name: "accountant colombo"); then use the most common word.
    head = len(terms) - 1
    if hits[head] == 0:
        head = max(range(len(terms)), key=lambda i: hits[i])
    others = [i for i in range(len(terms)) if i != head]
    others_weight = sum(weights[i] for i in others)

    tiers: dict[int, list[tuple[float, int, Job]]] = {1: [], 2: [], 3: []}
    for index, (job, row) in enumerate(zip(jobs, strengths)):
        if row[head] == 0:
            continue
        matched = sum(weights[i] for i in others if row[i] > 0)
        if not others or all(row[i] > 0 for i in others):
            tier = 1
        elif matched >= TIER2_MIN_SHARE * others_weight:
            tier = 2
        elif matched == 0:
            tier = 3
        else:
            continue  # only weak extra words matched: noise like "Senior Civil Engineer"
        score = sum(s * w for s, w in zip(row, weights))
        tiers[tier].append((score, index, job))

    if strict:
        chosen = tiers[1]
    else:
        chosen = tiers[1] + tiers[2] if tiers[1] or tiers[2] else tiers[3]
    tier_of = {id(item): t for t in (1, 2, 3) for item in tiers[t]}
    chosen.sort(key=lambda item: (tier_of[id(item)], -round(item[0], 9), item[1]))
    return [job for _, _, job in chosen]



def merge_by_priority(lists: Iterable[Sequence[Job]]) -> list[Job]:
    """Concatenate ranked lists, best list first, dropping jobs already taken.
    A category lists its most wanted kind of job first (e.g. "data engineer" before
    "analytics"), so its best matches fill the first page."""
    merged: list[Job] = []
    seen: set[str] = set()
    for jobs in lists:
        for job in jobs:
            if job.url not in seen:
                seen.add(job.url)
                merged.append(job)
    return merged
