"""Canonical BitTorrent v1 info-hash handling."""

import base64
import binascii
import re
from urllib.parse import parse_qs, urlsplit

_HEX_HASH = re.compile(r"^[0-9a-fA-F]{40}$")
_BASE32_HASH = re.compile(r"^[A-Z2-7]{32}$", re.IGNORECASE)


def normalize_info_hash(raw: object) -> str | None:
    """Return canonical lowercase 40-hex BTIH, accepting hex or Base32."""
    value = str(raw or "").strip()
    if _HEX_HASH.fullmatch(value):
        return value.lower()
    if not _BASE32_HASH.fullmatch(value):
        return None
    try:
        decoded = base64.b32decode(value.upper(), casefold=True)
    except (binascii.Error, ValueError):
        return None
    return decoded.hex() if len(decoded) == 20 else None


def info_hash_from_magnet(magnet: object) -> str | None:
    """Extract and normalize an exact ``xt=urn:btih:`` parameter."""
    try:
        parts = urlsplit(str(magnet or ""))
    except ValueError:
        return None
    if parts.scheme.lower() != "magnet":
        return None
    for xt in parse_qs(parts.query, keep_blank_values=True).get("xt", []):
        prefix = "urn:btih:"
        if xt.lower().startswith(prefix):
            normalized = normalize_info_hash(xt[len(prefix):])
            if normalized:
                return normalized
    return None
