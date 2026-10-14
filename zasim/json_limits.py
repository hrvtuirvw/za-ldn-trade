"""Deterministic JSON nesting limits independent of Python's recursion setting."""
from __future__ import annotations


def validate_json_depth(raw: bytes, *, max_depth: int = 64) -> None:
    """Bound containers outside quoted strings; json.loads still checks syntax."""
    depth = 0
    quoted = False
    escaped = False
    for value in raw:
        if quoted:
            if escaped:
                escaped = False
            elif value == 0x5C:
                escaped = True
            elif value == 0x22:
                quoted = False
        elif value == 0x22:
            quoted = True
        elif value in (0x7B, 0x5B):
            depth += 1
            if depth > max_depth:
                raise ValueError(f"JSON nesting exceeds {max_depth} containers")
        elif value in (0x7D, 0x5D):
            depth -= 1
