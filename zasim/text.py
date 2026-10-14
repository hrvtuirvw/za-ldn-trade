"""Safe, visible rendering for untrusted protocol and save-data text."""

from __future__ import annotations

import unicodedata


def display_safe(value: object, *, limit: int = 512) -> str:
    """Return text that cannot inject controls or invalid Unicode into a UI."""

    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("display limit must be a positive integer")
    source = str(value)
    clipped = len(source) > limit
    source = source[:limit]
    rendered: list[str] = []
    for character in source:
        codepoint = ord(character)
        category = unicodedata.category(character)
        bidi = unicodedata.bidirectional(character)
        if category in {"Cc", "Cf", "Cs"} or bidi in {
            "LRE",
            "RLE",
            "LRO",
            "RLO",
            "PDF",
            "LRI",
            "RLI",
            "FSI",
            "PDI",
            "BN",
        }:
            rendered.append(
                f"\\u{codepoint:04X}"
                if codepoint <= 0xFFFF
                else f"\\U{codepoint:08X}"
            )
        else:
            rendered.append(character)
    if clipped:
        rendered.append("…")
    return "".join(rendered)


__all__ = ["display_safe"]
