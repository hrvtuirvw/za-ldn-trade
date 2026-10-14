"""Validation for the display name shared by LDN and Pia session setup."""

MAX_LDN_NICKNAME_CHARACTERS = 20
MAX_LDN_NICKNAME_BYTES = 32


def validate_ldn_nickname(nickname: str) -> str:
    """Return *nickname* only when it fits both native LDN limits.

    The limits apply to different representations: Pia permits at most 20
    Unicode code points, while LDN authentication stores no more than 32
    UTF-8 bytes.  Rejecting before serialization avoids both character and
    byte slicing, either of which could silently change the advertised name.
    """

    if not isinstance(nickname, str):
        raise TypeError("LDN nickname must be a string")
    if not nickname:
        raise ValueError("LDN nickname cannot be empty")
    if len(nickname) > MAX_LDN_NICKNAME_CHARACTERS:
        raise ValueError(
            "LDN nickname must contain at most "
            f"{MAX_LDN_NICKNAME_CHARACTERS} Unicode characters"
        )
    try:
        encoded = nickname.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("LDN nickname must be valid UTF-8 text") from exc
    if len(encoded) > MAX_LDN_NICKNAME_BYTES:
        raise ValueError(
            f"LDN nickname must fit in {MAX_LDN_NICKNAME_BYTES} UTF-8 bytes"
        )
    return nickname
