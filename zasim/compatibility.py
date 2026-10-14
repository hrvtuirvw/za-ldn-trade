"""Public title compatibility predicate, independent of the LDN app version.

Static startup/validator audit: 1.0.0 and 1.0.1 expect LE16=1; authentic
2.0.2 expects LE16=6. See docs/ZA_TITLE_COMPATIBILITY_20260912.md.
This is not a credential or proof of live admission.
"""
from __future__ import annotations

from types import MappingProxyType


DEFAULT_ZA_TITLE_VERSION = "2.0.2"
ZA_TITLE_COMPATIBILITY_TAGS = MappingProxyType({"1.0.0": 1, "1.0.1": 1, "2.0.2": 6})
SUPPORTED_ZA_TITLE_VERSIONS = tuple(ZA_TITLE_COMPATIBILITY_TAGS)


def identification_token_for_title(
    title_version: str = DEFAULT_ZA_TITLE_VERSION,
    identification_token: bytes | bytearray | memoryview | None = None,
) -> bytes:
    """Resolve a supported title version; an explicit 32-byte token wins.

    Unknown versions always fail closed (even with an override). Overrides,
    including all-zero tokens, are copied byte-exactly, never re-tagged.
    Do not pass an LDN application communication version as title_version.
    """
    if not isinstance(title_version, str) or title_version not in ZA_TITLE_COMPATIBILITY_TAGS:
        raise ValueError("unsupported Z-A title_version; choose " + ", ".join(SUPPORTED_ZA_TITLE_VERSIONS))
    if identification_token is None:
        return ZA_TITLE_COMPATIBILITY_TAGS[title_version].to_bytes(2, "little") + bytes(30)
    if not isinstance(identification_token, (bytes, bytearray, memoryview)):
        raise TypeError("identification_token must be bytes-like")
    token = bytes(identification_token)
    if len(token) != 32:
        raise ValueError("identification_token must contain exactly 32 bytes")
    return token


# Historical import name retained; the normal/default title is now 2.0.2.
ZA_IDENTIFICATION_TOKEN = identification_token_for_title()
