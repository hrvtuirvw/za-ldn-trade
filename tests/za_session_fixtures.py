"""Synthetic ROM-layout acceptance fixtures, never native captured traffic."""


def acceptance(our_identity, host_identity, *, sequence=2, generation=1,
               index=1, update_generation=1):
    assignment = (b"\x02\0\0\x01" + bytes(8) + host_identity + our_identity
                  + bytes([index]) + generation.to_bytes(2, "big")
                  + sequence.to_bytes(2, "big"))

    def station(identity, slot, gen):
        # IPv4 endpoint, index/generation, NAT + private flags, token,
        # one player + one participant, station counter, PlayerInfo.
        return (identity + bytes([169, 254, 1, slot + 1, 0x30, 0x39])
                + bytes([slot]) + gen.to_bytes(2, "big") + bytes(34)
                + b"\x01\x01\0\0" + bytes(16) + b"\0\0\0\x01\x01X")

    body = (b"\x05" + sequence.to_bytes(2, "big") + host_identity
            + b"\x02\0" + update_generation.to_bytes(2, "big") + bytes(2)
            + bytes(4) + station(host_identity, 0, 0)
            + station(our_identity, index, generation))
    wire = body[:3] + b"\x01\0\0\x03" + body[3:]
    return assignment, wire


def session_acceptance(session, **kwargs):
    host = session.manager.host_mac.ljust(8, b"\0") + session.host_var.to_bytes(2, "big")
    ours = session.manager.our_mac + session.our_var.to_bytes(2, "big")
    return acceptance(ours, host, **kwargs)
