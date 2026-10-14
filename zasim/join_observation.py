"""Bounded, payload-free observations of one ordinary Join run.

Only counters/classifications are retained, never message objects or bytes.
No clock, RNG, logger or I/O is called at a protocol boundary: emit after
transport cleanup. Existing timer sampling, retry policy and send order stay intact.
Send return is NOT kernel egress or host admission; use the independent egress
witness for that boundary. Event ordinals are local ordering, not packet IDs.
"""

import json


class JoinObservation:
    STAGES = ("build_started", "built", "enqueue_started", "queued",
              "encode_started", "encoded", "send_started", "send_returned")
    SESSION_KINDS = ("type_0", "type_1", "type_2", "type_5", "type_6", "other", "empty")

    def __init__(self):
        self.counts = dict.fromkeys(self.STAGES, 0)
        self.ordinal = 0
        self.first_rx = {}
        self.rx_counts = {1: 0, 3: 0, 13: 0}
        self.udp_host = self.udp_other = self.decode_started = self.decoded = 0
        self.session_census = {
            f"session_{stage}_{kind}": 0
            for stage in ("decoded", "prefeed") for kind in self.SESSION_KINDS
        }

    def _count_session(self, stage, message):
        if message.proto == 13:
            payload = message.payload
            kind = (f"type_{payload[0]}" if payload and payload[0] in (0, 1, 2, 5, 6)
                    else "other" if payload else "empty")
            self.session_census[f"session_{stage}_{kind}"] += 1

    def decoded_batch(self, messages):
        """Count the whole successfully decoded batch before any feed can raise.

        No identities/state/ordinals are sampled here. First-candidate snapshots
        remain at each actual pre-feed boundary, preserving their phase meaning.
        """
        for message in messages:
            self._count_session("decoded", message)

    def mark(self, stage):
        self.ordinal += 1
        self.counts[stage] += 1

    def receive(self, session, header, message):
        """Before feed/policy: a decoded candidate, never an acceptance marker."""
        proto = message.proto
        if proto not in self.rx_counts:
            return
        self.ordinal += 1
        self.rx_counts[proto] += 1
        self._count_session("prefeed", message)
        payload = message.payload
        # Bound cardinality and do not interpret arbitrary control body bytes
        # as metadata. Session's first byte is its public message discriminator.
        kind = (payload[0] if payload else -1) if proto == 13 else -1
        key = (proto, kind)
        if key in self.first_rx:
            return
        manager = session.manager
        response = proto == 13 and kind == 2 and len(payload) == 37
        host = manager.host_mac.ljust(8, b"\0")
        self.first_rx[key] = {
            "event": "first_rx_candidate", "ordinal": self.ordinal,
            "protocol": proto, "session_type": kind, "length": len(payload),
            "reason": payload[3] if response else -1,
            "exact_response_shape": response,
            "outer_source": ("unknown" if manager.host_var is None else
                             "host" if header.src == manager.host_var else "other"),
            "outer_destination": ("local" if header.dst == manager.our_var else
                                  "control" if header.dst in (0, 1) else "other"),
            "body_host_match": (payload[12:20] == host and
                                manager.host_var is not None and
                                int.from_bytes(payload[20:22], "big") == manager.host_var)
                               if response else None,
            "body_target_match": (payload[22:30] == manager.our_mac and
                                  int.from_bytes(payload[30:32], "big") == manager.our_var)
                                 if response else None,
            "phase": manager.join_phase,
            **self.counts,
        }

    def emit(self, log, session):
        """Best-effort terminal report; cannot mask failure or prevent cleanup."""
        manager = getattr(session, "manager", None)
        if manager is None:
            return  # No actual Session was initialized.
        summary = {
            "event": "outcome", "ordinal": self.ordinal,
            "phase": getattr(manager, "join_phase", "not_started"),
            "assignment_installed": getattr(session, "assignment_sequence", None) is not None,
            "update_installed": getattr(session, "update_sequence", None) is not None,
            "connected": bool(getattr(session, "connected", False)),
            "net_candidates": self.rx_counts[1], "rtt_candidates": self.rx_counts[3],
            "session_candidates": self.rx_counts[13],
            "udp_host": self.udp_host, "udp_other": self.udp_other,
            "decode_started": self.decode_started, "decoded": self.decoded,
            "kernel_egress": "unobserved", **self.counts, **self.session_census,
        }
        for record in (*self.first_rx.values(), summary):
            try:
                log("Pia Join observation " + json.dumps(record, sort_keys=True, separators=(",", ":")))
            except BaseException:
                pass
