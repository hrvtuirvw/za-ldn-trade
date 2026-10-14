"""Pia 6.40 Session Update wire reader (main+0x241B080/0x2406164).

An on-wire type 5 is a *fragment*, including for a one-fragment Update.
The fragment header is not part of the reconstructed station table.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionStation:
    identity: bytes
    index: int
    generation: int


@dataclass(frozen=True, slots=True)
class SessionUpdate:
    sequence: int
    host_identity: bytes
    generation: int
    stations: tuple[SessionStation, ...]


def parse_update(data: bytes) -> SessionUpdate | None:
    """Read the bounded station/player table before committing any state.

    Layout: 0x24057C0 writer; 0x2406164, 0x2406D50, 0x2407310
    readers. IPv6 flags are a raw little-endian 32-bit bitset, not BE32.
    Pending station records have their own fixed 49-byte prefix.
    """
    if len(data) < 19 or data[0] != 5 or data[13] > 24 or data[14] > 72:
        return None
    offset = 19

    def take(size):
        nonlocal offset
        end = offset + size
        if end > len(data):
            raise ValueError("truncated Session Update")
        value = data[offset:end]
        offset = end
        return value

    def players(count):
        for _ in range(count):
            prefix = take(21)  # PlayerId16, BE32 name byte length, encoding
            size = int.from_bytes(prefix[16:20], "big")
            if size > 0x50:
                raise ValueError("oversized PlayerInfo name")
            take(size)

    stations = []
    try:
        count = data[13]
        ipv6 = int.from_bytes(take(4 if count else 0), "little")
        for i in range(count):
            identity = take(10)
            take(18 if ipv6 & (1 << i) else 6)  # StationAddress without kind
            fixed = take(41)  # index, generation, NAT flags, token, counts
            if fixed[0] >= 32:
                return None
            stations.append(SessionStation(
                identity, fixed[0], int.from_bytes(fixed[1:3], "big")))
            players(fixed[37])
        for _ in range(data[14]):
            pending = take(49)
            players(pending[15])
    except ValueError:
        return None
    if (offset != len(data)
            or len({s.identity for s in stations}) != len(stations)
            or len({s.index for s in stations}) != len(stations)):
        return None
    return SessionUpdate(int.from_bytes(data[1:3], "big"), data[3:13],
                         int.from_bytes(data[15:17], "big"), tuple(stations))


class UpdateAssembler:
    """One bounded in-flight Update; never ACK partial/conflicting chunks.

    Native completion uses an 8-bit received-chunk mask. Our additional
    contiguous/non-overlap check avoids treating zero-filled holes as state.
    """

    def __init__(self):
        self.sequence = None
        self.count = 0
        self.parts = {}

    def feed(self, payload: bytes) -> bytes | None:
        if len(payload) < 8 or payload[0] != 5:
            return None
        sequence = int.from_bytes(payload[1:3], "big")
        count, index = payload[3:5]
        offset = int.from_bytes(payload[5:7], "big")
        part = payload[7:]
        if (sequence == 0xFFFF or not 1 <= count <= 8 or index >= count
                or offset < 3 or offset + len(part) > 65535):
            return None
        if self.sequence is not None and sequence < self.sequence:
            return None
        if self.sequence != sequence or count != self.count:
            # ROM 0x241B0F4..0x241B140 restarts even at the same sequence
            # when the chunk count changes. Drop every old part/mask bit;
            # otherwise a re-fragmented Update leaves the joiner waiting.
            self.sequence, self.count, self.parts = sequence, count, {}
        if index in self.parts and self.parts[index] != (offset, part):
            return None
        self.parts[index] = (offset, part)
        if len(self.parts) != count:
            return None
        result = bytearray(payload[:3])
        for position in range(count):
            start, chunk = self.parts[position]
            if start != len(result):
                return None
            result.extend(chunk)
        return bytes(result)
