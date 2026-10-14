"""Title-specific constants for Pokemon Legends: Z-A's public LDN profile.

The values in this module come from the public NintendoClients wiki.  Keeping
them in a profile makes the Pia transport reusable without baking a title key
into packet parsing code.
"""

from __future__ import annotations

from dataclasses import dataclass


ZA_TITLE_ID = "0100F43008C44000"
ZA_BOX_TRADE_SCENE_ID = 1
ZA_BOX_TRADE_MAX_PARTICIPANTS = 2
ZA_LDN_PASSPHRASE = (
    b"BM7cXkadR9ugiXdHiurkiyhrQwcR3rMgCM5BF47dranKXWAGpGEA9z3ncXRnPjCX"
)
ZA_GAME_KEY = b"p3bwdaSsywFXUkDu"


@dataclass(frozen=True)
class PiaProfile:
    """The small set of title/version inputs needed by the generic Pia core."""

    name: str
    title_id: str
    pia_version: str
    header_version: int
    max_packet_size: int
    ldn_passphrase: bytes
    game_key: bytes
    zstd_level: int = 4

    def __post_init__(self) -> None:
        title_id = self.title_id.upper()
        if len(title_id) != 16:
            raise ValueError("title_id must contain exactly 16 hexadecimal characters")
        try:
            int(title_id, 16)
        except ValueError as exc:
            raise ValueError("title_id must be hexadecimal") from exc
        if not 0 <= self.header_version <= 0x7F:
            raise ValueError("header_version must fit in seven bits")
        if self.max_packet_size < 29:
            raise ValueError("max_packet_size is smaller than a Pia header")
        if len(self.ldn_passphrase) != 64:
            raise ValueError("an LDN passphrase must be exactly 64 bytes")
        if len(self.game_key) != 16:
            raise ValueError("a Pia AES game key must be exactly 16 bytes")
        if self.zstd_level <= 0:
            raise ValueError("zstd_level must be positive")

        # Normalize mutable byte-like inputs and hexadecimal casing even for
        # frozen profiles created by callers.
        object.__setattr__(self, "title_id", title_id)
        object.__setattr__(self, "ldn_passphrase", bytes(self.ldn_passphrase))
        object.__setattr__(self, "game_key", bytes(self.game_key))

    @property
    def title_id_int(self) -> int:
        return int(self.title_id, 16)


ZA_PROFILE = PiaProfile(
    name="Pokemon Legends: Z-A",
    title_id=ZA_TITLE_ID,
    pia_version="6.40",
    header_version=16,
    max_packet_size=1452,
    ldn_passphrase=ZA_LDN_PASSPHRASE,
    game_key=ZA_GAME_KEY,
)
