"""Relevance scoring used to resolve a free text game name to a Steam appid."""

from __future__ import annotations

import re

from .models import GameCandidate

_CJK = r"\u3400-\u9fff"
_NON_WORD = re.compile(rf"[^0-9a-z{_CJK}]+")
_WORD_SPLIT = re.compile(rf"[^0-9a-z{_CJK}]+")
_CJK_CHAR = re.compile(rf"[{_CJK}]")

# Scores assigned to each kind of name match.
_EXACT = 100.0
_PREFIX = 85.0
_CONTAINS = 70.0
_SUBSEQUENCE = 60.0
_TOKEN_BASE = 40.0

# Guards for the subsequence tier. CJK is written without spaces, so a query
# and the store's title for the same game can differ by characters inserted in
# the middle, which no prefix or substring test can bridge: ``荒野大镖客2`` is
# the natural way to ask for ``荒野大镖客：救赎2``. Subsequence matching covers
# that, but it is loose by nature, so it needs both a length floor and a
# coverage floor, and it only applies when the shorter side contains CJK.
_MIN_SUBSEQUENCE_LEN = 3
_MIN_SUBSEQUENCE_RATIO = 0.5


def _is_subsequence(shorter: str, longer: str) -> bool:
    """Report whether ``shorter`` appears in ``longer`` in order, gaps allowed.

    Args:
        shorter: The string that must be consumed.
        longer: The string to consume it from.

    Returns:
        True when every character of ``shorter`` occurs in ``longer`` in order.
    """
    remaining = iter(longer)
    return all(char in remaining for char in shorter)


def _subsequence_score(key: str, target: str) -> float:
    """Score a gapped in-order match, or zero when it does not qualify.

    Args:
        key: Normalized query.
        target: Normalized candidate name.

    Returns:
        :data:`_SUBSEQUENCE` when the shorter side is a CJK-bearing
        subsequence covering enough of the longer side, otherwise 0.
    """
    shorter, longer = (key, target) if len(key) <= len(target) else (target, key)
    if len(shorter) < _MIN_SUBSEQUENCE_LEN or not longer:
        return 0.0
    # Latin-only subsequences match far too much (``gtav`` inside ``grand theft
    # auto v``), and the problem this solves is specific to CJK.
    if not _CJK_CHAR.search(shorter):
        return 0.0
    if len(shorter) / len(longer) < _MIN_SUBSEQUENCE_RATIO:
        return 0.0
    return _SUBSEQUENCE if _is_subsequence(shorter, longer) else 0.0


def normalize(text: str) -> str:
    """Fold a game name into a comparable key.

    Removes punctuation, spaces and trademark symbols, and lowercases ASCII
    letters while keeping CJK characters intact.

    Args:
        text: Raw game name.

    Returns:
        The normalized comparison key.
    """
    return _NON_WORD.sub("", (text or "").casefold())


def tokenize(text: str) -> set[str]:
    """Split a game name into lowercased word tokens.

    Args:
        text: Raw game name.

    Returns:
        The set of word tokens.
    """
    return {part for part in _WORD_SPLIT.split((text or "").casefold()) if part}


def score_candidate(query: str, *names: str) -> float:
    """Score how well any of a candidate's names matches the user query.

    A game is known by several names: Heybox returns the localized title while
    Steam returns its own storefront title. Matching against both is what lets
    an English query such as ``Terraria`` resolve to appid 105600 even though
    Heybox calls that game ``泰拉瑞亚``.

    Args:
        query: What the user typed.
        *names: Every known name for the candidate. Empty values are ignored.

    Returns:
        The best score found, where 100 is an exact match and 0 means no relation.
    """
    key = normalize(query)
    if not key:
        return 0.0
    best = 0.0
    query_tokens = tokenize(query)
    for name in names:
        target = normalize(name)
        if not target:
            continue
        if key == target:
            return _EXACT
        if target.startswith(key):
            best = max(best, _PREFIX)
            continue
        if key in target:
            best = max(best, _CONTAINS)
            continue
        # Gapped in-order match, for titles that insert characters mid-name.
        subsequence = _subsequence_score(key, target)
        if subsequence:
            best = max(best, subsequence)
            continue
        target_tokens = tokenize(name)
        if query_tokens and target_tokens:
            overlap = len(query_tokens & target_tokens) / len(query_tokens)
            if overlap >= 0.5:
                best = max(best, _TOKEN_BASE + overlap * 20.0)
    return best


def rank_candidates(
    query: str,
    candidates: list[GameCandidate],
    limit: int = 8,
) -> list[GameCandidate]:
    """Score provider candidates and order them best first.

    Candidates keep a small score even when no name matches, because the caller
    may still want to offer them as choices.

    Args:
        query: What the user typed.
        candidates: Raw candidates from a provider.
        limit: Maximum number of candidates to keep.

    Returns:
        Candidates sorted best first, each carrying its score.
    """
    scored: list[GameCandidate] = []
    seen: set[int] = set()
    for candidate in candidates:
        if candidate.appid in seen:
            continue
        seen.add(candidate.appid)
        score = score_candidate(query, candidate.name, candidate.steam_name)
        scored.append(
            GameCandidate(
                appid=candidate.appid,
                name=candidate.name,
                steam_name=candidate.steam_name,
                source=candidate.source,
                score=score,
                popularity=candidate.popularity,
            )
        )
    scored.sort(key=lambda item: (-item.score, -item.popularity, len(item.display_name)))
    return scored[:limit]


def is_confident(ranked: list[GameCandidate]) -> bool:
    """Decide whether the best candidate can be used without asking the user.

    An exact name match always wins outright. A prefix match wins when it beats
    the runner up, or when it is far more followed than the runner up, which is
    what separates a real game from its soundtrack or DLC entries.

    Args:
        ranked: Output of :func:`rank_candidates`.

    Returns:
        True when the top candidate should be selected automatically.
    """
    if not ranked:
        return False
    if len(ranked) == 1:
        return True
    top, second = ranked[0], ranked[1]
    if top.score >= _EXACT:
        return True
    if top.score < _PREFIX:
        return False
    if top.score > second.score:
        return True
    return top.popularity >= max(second.popularity, 1) * 3
