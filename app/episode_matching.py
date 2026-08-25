"""Reconhecimento compartilhado de temporada/episódio em release names."""

from __future__ import annotations

import re

_EXPLICIT_EPISODE_PATTERNS = (
    re.compile(
        r"(?<![a-z0-9])s0*(\d{1,2})e0*(\d{1,3})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)0*(\d{1,2})x0*(\d{1,3})(?!\d)", re.IGNORECASE),
    re.compile(
        r"(?<![a-z0-9])t0*(\d{1,2})e0*(\d{1,3})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(
        r"temporada\s*0*(\d{1,2}).{0,20}?epis[oó]dio\s*0*(\d{1,3})(?!\d)",
        re.IGNORECASE,
    ),
)


def extract_explicit_episode(text: str) -> tuple[int, int] | None:
    """Retorna (temporada, episódio) apenas quando há marcador explícito."""
    for pattern in _EXPLICIT_EPISODE_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def has_explicit_episode_marker(text: str) -> bool:
    return extract_explicit_episode(text) is not None


def matches_explicit_episode(text: str, season: int, episode: int) -> bool:
    return extract_explicit_episode(text) == (season, episode)
