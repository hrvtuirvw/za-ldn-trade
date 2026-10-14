"""Host-acknowledged Z-A Pia Net/Session/RTT handshake.

Based on kinnay's NintendoClients documentation and reference wire data.
zasim.session supplies the complete protocol table, advertised version,
player identity and validated station updates explicitly. A successful
handshake is not a completed trade. Variable ids are local session values;
constant ids use the LDN MAC permutation.
"""

import secrets
import time
from dataclasses import dataclass

from zasim.ldn.ldn_name import validate_ldn_nickname

# Proto ids (Pia 6.32+)
PROTO_NET = 1
PROTO_RTT = 3
PROTO_RELIABLE = 10
PROTO_SESSION = 13

# The reserved session pseudo-station var-id. RTT (proto 3) + session-control ride header dst=0x0001
# (NOT the peer var); the footer recipient stays the host var. (observed: RTT dst=0x0001 both ways)
SESSION_VAR = 0x0001
RTT_ORIGINATE_PERIOD = 10    # VBlanks between guest-originated RTT requests (~6/s; reference capture ~5.9/s)
# ROM main+0x240ff0c / 0x2410278: elapsed 500 ms, never VBlank-quantized.
SESSION_JOIN_RETRY_SECONDS = 0.5


@dataclass(frozen=True)
class JoinTimingSelector:
    """One native selector object, shared by Join, extension and Update.

    ROM main+0x24b2c6c: high32(random32 * 28), then random32 >> 31.
    The injectable RNG supplies uint32 draws, not guessed timer jitter. The
    default entropy source is not a byte-exact emulation of nn's tick PRNG.
    """
    selector: int
    bit: int

    @classmethod
    def sample(cls, random32):
        return cls((random32() * 28) >> 32, random32() >> 31)

    @property
    def join_seconds(self):
        # main+0x240f870..8f0; LocalFacade's mesh addition is zero.
        return (7.0 if self.bit == 0 else 9.0) if self.selector == 16 else 8.0

    @property
    def update_seconds(self):
        # main+0x24102dc..330; reuse, do not resample the selector.
        return (9.0 if self.bit == 0 else 13.0) if self.selector == 26 else 11.0


class SessionPhaseTimeout(TimeoutError):
    """Terminal local JoinMesh phase expiry (not a host rejection reason)."""

    def __init__(self, phase):
        self.phase = phase
        super().__init__(f"Session {phase} phase deadline expired")


# Net message types.  The legacy names are retained for callers; 0x11/0x12 are
# Pia's update-network-connection-status broadcast and acknowledgement.
NET_CONN_REQUEST = 0x11      # host -> joiner
NET_CONN_RESPONSE = 0x12     # joiner -> host
NET_KEEPALIVE_REQUEST = 0x40 # host -> joiner while the Net connection is pending/live
NET_KEEPALIVE_ACK = 0x41     # joiner -> host, carries network + local station identity
NET_UPDATE_PROPERTY = 0x50   # host -> joiner (mid-session "update network property", retransmit-until-acked)
NET_UPDATE_PROPERTY_ACK = 0x51   # joiner -> host

# Session(new) message types
SESSION_JOIN_REQUEST = 0
SESSION_UPDATE = 5
SESSION_LEFT_SYNC = 7

def ldn_constant_id(mac):
    """Return Pia 6.x's 8-byte LDN constant id for a six-byte MAC.

    Pia serializes the MAC octets as ``2, 4, 5, 3, 1, 0`` and reserves the
    low sixteen bits.  This is observable in Run29, where BSSID
    ``bc:89:a6:cc:23:55`` becomes ``a62355cc89bc0000`` on the wire.
    """

    raw = bytes(mac)
    if len(raw) != 6:
        raise ValueError("an LDN MAC address must contain exactly 6 bytes")
    return bytes((raw[2], raw[4], raw[5], raw[3], raw[1], raw[0], 0, 0))


def _constant_id(value):
    """Normalize a legacy six-byte wire prefix or a complete constant id."""

    raw = bytes(value)
    if len(raw) == 6:
        return raw + b"\x00\x00"
    if len(raw) == 8:
        return raw
    raise ValueError("a Pia constant id must contain 6 or 8 bytes")


def _ip4(ip):
    return bytes(int(x) for x in ip.split("."))


# --- Net protocol (proto 1) -----------------------------------------------------------------
def parse_net(payload):
    """Return (version, type, body) inside one decoded compact message.

    Net's size field describes the variable payload, not all bytes following
    the common header. For example, a status message has 26 fixed bytes before
    its NetStation array; an ACK has size zero but still carries a sequence.
    The enclosing Pia compact-message length already supplies the boundary.
    Type-specific readers must validate their fixed fields separately.
    """
    if len(payload) < 4:
        return None
    return payload[0], payload[1], bytes(payload[4:])


def build_net_response(seqid=2):
    """Serialize Net 0x12 with the caller's accepted status sequence (BE32).

    A fresh accepted 0x11 advances this value; stale/equal statuses ACK the
    stored value, not an older incoming sequence (main+0x23BBEC4..0x23BC084).
    """
    return bytes([0x01, NET_CONN_RESPONSE, 0x00, 0x00]) + (seqid & 0xFFFFFFFF).to_bytes(4, "big")


def build_net_property_ack(seqid):
    """Net 0x51 'Update network property' ACK (observed: 01 51 00 00 | <seqid:u32 BE>), echoing
    the host's 0x50 seqid. The host retransmits its 0x50 every 500ms until it sees this ack."""
    return bytes([0x01, NET_UPDATE_PROPERTY_ACK, 0x00, 0x00]) + (seqid & 0xFFFFFFFF).to_bytes(4, "big")


def build_net_keepalive_ack(network_id, local_constant_id, local_var):
    """Build Pia 6.40 NetKeepAliveNetworkAck (type 0x41).

    Static analysis of the native serializer at ``main+0x23C8164`` / body
    serializer ``main+0x23C6ED4`` establishes the exact 22-byte shape:
    ``01 41 00 00`` followed by the 8-byte network id, the local 8-byte
    constant id, and the local 16-bit variable id, all in network byte order.
    The Net header's size field is zero, matching other fixed Net messages.
    """
    raw_network_id = bytes(network_id)
    if len(raw_network_id) != 8:
        raise ValueError("network_id must contain exactly 8 bytes")
    constant_id = _constant_id(local_constant_id)
    variable_id = _vid(local_var)
    if not 0 <= variable_id <= 0xFFFF:
        raise ValueError("local_var must fit in 16 bits")
    return (
        bytes([0x01, NET_KEEPALIVE_ACK, 0x00, 0x00])
        + raw_network_id
        + constant_id
        + variable_id.to_bytes(2, "big")
    )


def parse_net_network_id(payload):
    """Return the 8-byte network id carried by a Net 0x11 status message."""
    n = parse_net(payload)
    if not n or n[0] != 1 or n[1] != NET_CONN_REQUEST or len(n[2]) < 22:
        return None
    return bytes(n[2][14:22])


def parse_net_conn_request(payload):
    """Pull the host identity from a Net 0x11 status broadcast.

    The body begins with the sequence id, host variable id, and the complete
    eight-byte constant id.  LDN constant ids reserve their final two bytes, so
    this compatibility API returns the six-byte on-wire prefix.  Learn that
    prefix from the authenticated message: a participant-list MAC is in raw
    order and must not be substituted directly.
    """
    n = parse_net(payload)
    if not n or n[0] != 1 or n[1] != NET_CONN_REQUEST or len(n[2]) < 12:
        return None
    body = n[2]
    # body[0:4] = status seqid (echoed in 0x12); body[4:6] = host var;
    # body[6:14] = host constant id.  Its low two LDN bytes are zero.
    return int.from_bytes(body[4:6], "big"), bytes(body[6:12]), int.from_bytes(body[0:4], "big")


# --- RTT protocol (proto 3) -----------------------------------------------------------------
def parse_rtt(payload):
    """Read type byte 0 (request=0/response=1), version byte 3 and timestamp.

    Bytes 8:16 hold system time; a 21-byte request also carries the subject
    variable id at 19:21. Version byte 3 must not become part of the type.
    """
    if len(payload) < 16:
        return None
    return {"type": payload[0],                          # 0=request, 1=response (single byte)
            "version": payload[3],                       # RTT proto version (3 here); MUST be preserved
            "systime": payload[8:16],
            "subject": payload[19:21] if len(payload) >= 21 else b""}


def build_rtt_response(request):
    """Set only the type byte to 1; echo version, timestamp and host subject."""
    b = bytearray(request[:21].ljust(21, b"\x00"))
    b[0] = 1
    return bytes(b)


def build_rtt_request(template, systime):
    """Originate a guest type-0 RTT request (~5.9/s on dst=0x0001). Clone the host's last request
    `template` (so the layout/subject are byte-faithful) and set type=0 (LE) with a fresh incrementing
    `systime`. The host echoes it in a type-1 response; matching the echoed systime to the send tick
    gives the round-trip that drives the reliable RTO (and keeps our slot live in the host's RTT layer)."""
    b = bytearray(template[:21].ljust(21, b"\x00"))
    b[0] = 0                                          # type = request (BYTE 0); preserve the template's
    b[8:16] = (systime & ((1 << 64) - 1)).to_bytes(8, "little")   # version byte [3] + layout, fresh systime
    return bytes(b)


# --- Session protocol (proto 13) ------------------------------------------------------------
def build_session_join(src_mac, src_var, src_ip, dst_mac, dst_var, player_name,
                       random4, *, src_port=12345, app_ver, protocols, player_id,
                       identification_token):
    """Build a Session(new) join request.

    The title profile and player identity must be supplied explicitly.
    Constant ids accept six-byte wire prefixes or normalized eight-byte ids.
    """
    random4 = bytes(random4)
    app_ver = bytes(app_ver)
    player_id = bytes(player_id)
    identification_token = bytes(identification_token)
    if len(random4) != 4:
        raise ValueError("random4 must contain exactly 4 bytes")
    if len(app_ver) != 2:
        raise ValueError("app_ver must contain exactly 2 bytes")
    if len(player_id) != 16:
        raise ValueError("player_id must contain exactly 16 bytes")
    if len(identification_token) != 32:
        raise ValueError("identification_token must contain exactly 32 bytes")
    player_name = validate_ldn_nickname(player_name)
    protocols = tuple(protocols)
    out = bytearray([SESSION_JOIN_REQUEST, len(protocols)])
    for pid, ver in protocols:
        out += bytes([pid, ver])
    out += app_ver
    out += random4                                   # 4-byte random nonce
    out += _constant_id(src_mac)                       # source constant id (8)
    out += bytes(src_var)                            # source variable id (2)
    out += bytes([0, 0])                             # NAT mapping, is-private-IPv6
    out += identification_token                       # identification token
    out += _constant_id(dst_mac)                       # dest constant id (8)
    out += bytes(dst_var)                            # dest variable id (2)
    out += bytes([1, 1])                             # num players, num participants
    out += bytes([0]) + _ip4(src_ip) + src_port.to_bytes(2, "big")   # StationAddress (IPv4)
    # Both Pia's character limit and LDN's UTF-8 field width were validated
    # above.  Never truncate a user-supplied identity on the wire.
    nm = player_name.encode("utf-8")                 # PlayerInfo
    out += player_id + len(nm).to_bytes(4, "big") + bytes([1]) + nm
    return bytes(out)


def parse_session(payload):
    """Light parse: type and Join's protocol count, never an Update table count.

    Type 5 bytes 1..2 are its sequence; the station count exists only in the
    reconstructed table. This helper neither validates nor reassembles it.
    """
    if not payload:
        return None
    t = payload[0]
    rec = {"type": t}
    if t == SESSION_JOIN_REQUEST and len(payload) > 2:
        rec["count"] = payload[1]
    return rec


# --- connection state machine (HOST-ACK-GATED; loss-tolerant) -------------------------------
# Header var-id (dst,src) progression observed in the reference capture:
#   net 0x12 -> (0, 0)   |   session join -> (0, our_var)   |   finalize/reliable -> (host_var, our_var)
# We never advance on our own send; we advance when the HOST acknowledges.  It
# retransmits 0x11 only until it accepts our Net 0x12, so Session Join needs its
# own retry timer after that point.  Outbox entries carry their own routing and
# framing requirements.
ST_NET, ST_FINALIZE, ST_CONNECTED = "net", "finalize", "connected"


def build_session_finalize(our_mac, *, sequence, generation):
    """Type 6: local constant, BE32 Update sequence, BE16 generation.

    Pia 6.40 main+0x24069D4 serializes the received runtime values. The
    caller must supply the validated Update sequence and generation.
    """

    return (bytes([6]) + _constant_id(our_mac)
            + sequence.to_bytes(4, "big") + generation.to_bytes(2, "big"))


def _vid(x):
    return x if isinstance(x, int) else int.from_bytes(x, "big")


class ConnectionManager:
    """The S0 Pia connection as a host-ack-gated state machine. Outbox entries are
    (proto, payload, unicast, dst_var, src_var) - the var-ids change per stage."""

    def __init__(self, our_mac, host_mac, our_ip, host_ip, *, our_var,
                 player_name, app_ver, protocols, player_id, identification_token,
                 random4=None, log=lambda *a: None,
                 clock=None, random32=None, join_observation=None):
        self.join_observation = join_observation
        self.clock = time.monotonic if clock is None else clock
        self.random32 = (lambda: secrets.randbits(32)) if random32 is None else random32
        self.join_timing = JoinTimingSelector.sample(self.random32)
        self.our_mac = bytes(our_mac)
        self.host_mac = bytes(host_mac)
        self.our_ip = our_ip
        self.host_ip = host_ip
        self.our_var = _vid(our_var)
        self.host_var = None
        self.network_id = None
        # Pin the on-wire host identity only after the first valid status.
        # The participant MAC supplied at construction may use another order.
        self._net_host_constant_id = None
        self._net_status_sequence = None
        self.player_name = validate_ldn_nickname(player_name)
        self.app_ver = bytes(app_ver)
        if len(self.app_ver) != 2:
            raise ValueError("app_ver must contain exactly two network-order bytes")
        self.protocols = tuple((int(pid), int(ver)) for pid, ver in protocols)
        self.player_id = bytes(player_id)
        if len(self.player_id) != 16:
            raise ValueError("player_id must contain exactly 16 bytes")
        self.identification_token = bytes(identification_token)
        if len(self.identification_token) != 32:
            raise ValueError("identification_token must contain exactly 32 bytes")
        self._initial_random4 = None if random4 is None else bytes(random4)
        self.random4 = bytes(4) if random4 is None else bytes(random4)
        if len(self.random4) != 4:
            raise ValueError("random4 must contain exactly 4 bytes")
        self.log = log
        self.info = getattr(log, "info", log)   # clean milestone sink (default-mode narration)
        self.state = ST_NET
        self._outbox = []
        # Job phases are distinct from Net/Finalize/connected protocol gates.
        # First validated Net status supplies our equivalent of ready peer
        # state. No guessed keepalive gate or physical flush delay is added.
        self.join_phase = "setup"
        self._join_payload = None
        self._join_response_deadline = None
        self._join_phase_deadline = None
        self._join_extended = False
        self._join_accepted = False
        self._join_sent = False
        self._phase_error = None
        # RTT origination: clone the host's last type-0 request as our template, bump a
        # systime per originated probe. _rtt_orig_tick gates the ~6/s cadence.
        self._last_host_rtt = None
        self._rtt_systime = 0x10000
        self._rtt_orig_tick = -10 ** 9
        # RTT measurement: each originated request's systime -> the tick it was sent, so the matching
        # type-1 response yields a round-trip. Completed round-trips (in ticks) accumulate in rtt_samples
        # for the caller to drain and feed into the reliable layer's RTO; the reliable RTO is RTT-driven.
        self._rtt_pending = {}
        self.rtt_samples = []

    def maybe_originate_rtt(self, tick):
        """Once connected, ORIGINATE a type-0 RTT request every RTT_ORIGINATE_PERIOD VBlanks (a liveness
        probe the host expects to see from us; it responds type-1, which we ignore). No-op
        until we have seen a host request to clone the layout from."""
        if not self.connected or self._last_host_rtt is None or self.host_var is None:
            return
        if tick - self._rtt_orig_tick < RTT_ORIGINATE_PERIOD:
            return
        self._rtt_orig_tick = tick
        self._rtt_systime = (self._rtt_systime + 1) & ((1 << 64) - 1)
        # remember when we sent this systime so the host's type-1 echo gives us the round-trip
        self._rtt_pending[self._rtt_systime] = tick
        if len(self._rtt_pending) > 64:                     # bound it if responses are being lost
            for k in sorted(self._rtt_pending, key=self._rtt_pending.get)[:32]:
                del self._rtt_pending[k]
        self._q(PROTO_RTT, build_rtt_request(self._last_host_rtt, self._rtt_systime),
                SESSION_VAR, self.our_var, False, True, False, footer_var=self.host_var)

    def _discard_queued_joins(self):
        self._outbox = [item for item in self._outbox
                        if not (item["proto"] == PROTO_SESSION
                                and item["payload"][:1] == b"\x00")]

    def raise_if_session_failed(self):
        if self._phase_error is not None:
            raise self._phase_error

    def check_session_deadline(self, now=None):
        """Fail closed; never permit a new send after its phase budget.

        WaitResponse tests c0, then Send tests b8 (0x2410278/0x240fc2c).
        Thus scheduler delay may postpone terminal observation, not permit
        catch-up sends or shorten the 500 ms response wait.
        """
        self.raise_if_session_failed()
        now = self.clock() if now is None else now
        due = (self.join_phase == "wait_update" or
               (self.join_phase == "wait_response" and
                now >= self._join_response_deadline))
        if due and now >= self._join_phase_deadline:
            phase = "Update" if self.join_phase == "wait_update" else "Join"
            self._phase_error = SessionPhaseTimeout(phase)
            self.join_phase = "failed"
            self._join_payload = None
            self._join_response_deadline = None
            self._discard_queued_joins()
            self.log(
                f"Pia Session phase timeout phase={phase} "
                f"assignment_accepted={str(self._join_accepted).lower()} "
                f"late_by_s={now - self._join_phase_deadline:.6f}"
            )
            raise self._phase_error
        return now

    def _send_session_join(self, now, *, use_clock):
        # main+0x24024fc..250c constructs/generates inner random4 on EACH
        # SendJoinRequest. Explicit random4 is a first-send fixture override;
        # all later sends use the injected uint32 source. Stable fields and
        # the independent outer writer packet-id/nonce allocation stay intact.
        if self._join_sent or self._initial_random4 is None:
            self.random4 = self.random32().to_bytes(4, "big")
        observation = self.join_observation
        if observation is not None:
            observation.mark("build_started")
        self._join_payload = self._join()
        if observation is not None:
            observation.mark("built")
        self._join_sent = True
        if observation is not None:
            observation.mark("enqueue_started")
        self._q(PROTO_SESSION, self._join_payload, 0, self.our_var,
                True, False, True)
        if observation is not None:
            observation.mark("queued")
        self.join_phase = "wait_response"
        # Native arms c0 AFTER the builder returns (main+0x240ff00..48).
        armed_at = self.clock() if use_clock else now
        self._join_response_deadline = armed_at + SESSION_JOIN_RETRY_SECONDS

    def maybe_retry_session_join(self, *, now=None):
        use_clock = now is None
        now = self.check_session_deadline(now)
        if (self.state != ST_NET or self.join_phase != "wait_response"
                or self.host_var is None or self._join_payload is None
                or now < self._join_response_deadline):
            return
        # Exactly one scheduler action, no burst/catch-up loop.
        self._send_session_join(now, use_clock=use_clock)

    def receive_session_wait(self, payload):
        """Validate one type1 extension, leaving the response timer alone.

        main+0x2404438..44d0: exact 0x15 bytes; host at [1:11], local
        at [11:21]. main+0x240f98c..9ec adds T to b8 only once.
        """
        if (self.join_phase != "wait_response" or self._join_extended
                or len(payload) != 0x15 or payload[0] != 1
                or self.host_var is None):
            return False
        host = self.host_mac.ljust(8, b"\0") + self.host_var.to_bytes(2, "big")
        ours = self.our_mac.ljust(8, b"\0") + self.our_var.to_bytes(2, "big")
        if payload[1:11] != host or payload[11:21] != ours:
            return False
        self._join_extended = True
        self._join_phase_deadline += self.join_timing.join_seconds
        return True

    def acknowledge_session_assignment(self, *, now=None):
        """Stop Join retries after a title adapter validates native assignment.

        Pia 6.40 type 2/reason 1 completes the JoinMesh response job
        (main+0x2404EE0 -> 0x240F97C), before type-5 Update. It is not
        permission to Finalize or send title data. Return false for a stale
        response or when no Join is outstanding.
        """
        # Response success precedes c0 expiry in WaitResponse (0x2410254).
        self.raise_if_session_failed()
        now = self.clock() if now is None else now
        if (self.state != ST_NET or self.join_phase != "wait_response"
                or self._join_payload is None):
            return False
        self.join_phase = "wait_update"
        self._join_phase_deadline = now + self.join_timing.update_seconds
        self._join_accepted = True
        self._join_payload = None
        self._join_response_deadline = None
        # A timer-triggered retry may have been queued in the same batch
        # before the assignment was delivered. It must not escape afterward.
        self._discard_queued_joins()
        self.log("host assigned station (Session type 2) -> awaiting Session Update")
        return True

    @property
    def connected(self):
        return self.state == ST_CONNECTED

    def acknowledge_session_update(self, *, sequence, generation, now=None):
        """Queue a validated Update ACK; assignment alone must not call this.

        Updates/retransmissions still need ACKs after connection, otherwise
        the native sender's unacknowledged-recipient mask never clears.
        """
        # Valid Update completion precedes b8 expiry (0x24104f4/0x2410534).
        self.raise_if_session_failed()
        if self.host_var is None:
            return
        self.join_phase = "complete"
        self._join_phase_deadline = None
        self._q(PROTO_SESSION, build_session_finalize(
                    self.our_mac, sequence=sequence, generation=generation),
                self.host_var, self.our_var, False, True, False)
        if self.state == ST_NET:
            self.state = ST_FINALIZE
            self._join_payload = None
            self._join_response_deadline = None
            self._discard_queued_joins()
            self.log("host acked join (Session) -> FINALIZE")
            self.info("Host acknowledged our join.")

    @property
    def local_identity_announced(self):
        """Whether changing our id would invalidate an already queued Join."""
        return self._net_host_constant_id is not None or self.state != ST_NET

    def validate_net_status(self, payload):
        """Parse status identity without changing connection state or outbox.

        Keep the historical six-byte-prefix fixtures readable, but never
        manufacture a sequence for a truncated fixed header. Host migration
        is not implemented: a changed identity requires a new connection, not
        a cached Join with mismatched inner and outer station ids.
        """
        request = parse_net_conn_request(payload)
        if request is None:
            return None
        host_var, host_mac, sequence = request
        network_id = parse_net_network_id(payload)
        if host_var in (0, SESSION_VAR):
            raise ValueError("Net status contains a reserved host variable id")
        if self.host_var is not None and self.host_var != host_var:
            raise ValueError("authenticated Net status changed host variable id")
        if (self._net_host_constant_id is not None
                and self._net_host_constant_id != host_mac):
            raise ValueError("authenticated Net status changed host constant id")
        if (self.network_id is not None and network_id is not None
                and self.network_id != network_id):
            raise ValueError("authenticated Net 0x11 changed network id")
        return host_var, host_mac, sequence, network_id

    def learn_ids(self, our_var, host_var):
        if our_var is not None:
            self.our_var = _vid(our_var)
        if host_var is not None:
            self.host_var = _vid(host_var)

    def _join(self):
        return build_session_join(self.our_mac, self.our_var.to_bytes(2, "big"), self.our_ip,
                                  self.host_mac, (self.host_var or 0).to_bytes(2, "big"),
                                  self.player_name, self.random4,
                                  app_ver=self.app_ver, protocols=self.protocols,
                                  player_id=self.player_id,
                                  identification_token=self.identification_token)

    def _q(self, proto, payload, dst, src, compress, footer, establishing, pktid=None, footer_var=None):
        """Queue one outbox entry (a dict the sim frames per the wire rules). `pktid` overrides the
        writer's packet counter. Net 0x12 uses the connectionless sentinel
        id 0; Session Join and each retry use the next per-peer packet id.
        `footer_var` overrides the footer recipient when it differs from the header dst (RTT: header
        dst=0x0001 session channel, footer recipient=host var 0x7620)."""
        self._outbox.append({"proto": proto, "payload": payload, "dst": dst, "src": src,
                             "compress": compress, "footer": footer, "establishing": establishing,
                             "unicast": True, "pktid": pktid, "footer_var": footer_var})

    def on_message(self, proto, payload, tick=None, *, now=None):
        """React to a decoded IN Pia message; queue the ack-gated reply. The exact framing per stage:
          * Net 0x12   -> hdr(dst=0, src=0),                 establishing (pktid 0, flag 0x02), no footer, uncompressed.
          * Session join -> hdr(dst=0, src=OUR_var),         establishing, next per-peer pktid, no footer, zstd-COMPRESSED.
          * Session finalize -> hdr(dst=host_var, src=our_var), established, footer=host_var, uncompressed.
          * RTT response -> hdr(dst=host_var, src=our_var),  established, footer=host_var, uncompressed."""
        use_clock = now is None
        self.raise_if_session_failed()
        now = self.clock() if now is None else now
        if proto == PROTO_NET:
            n = parse_net(payload)
            if n is None or n[0] != 1:
                return
            if n[1] == NET_CONN_REQUEST:
                status = self.validate_net_status(payload)
                if status is None:
                    return
                host_var, host_mac, seqid, network_id = status
                # Commit only after every identity check has passed.
                self.host_var = host_var
                self.host_mac = host_mac
                self._net_host_constant_id = host_mac
                if network_id is not None:
                    self.network_id = network_id
                # main+0x23BBEC4/8 uses unsigned <=, not serial-number
                # wrap ordering. Stale/equal statuses ACK the last accepted
                # sequence (0x23BC028); fresh ones commit it (0x23BC2C4).
                # Keep identity validation above even for old statuses.
                if (self._net_status_sequence is None
                        or seqid > self._net_status_sequence):
                    self._net_status_sequence = seqid
                self._q(PROTO_NET, build_net_response(self._net_status_sequence),
                        0, 0, False, False, True, pktid=0)
                if self.state == ST_NET and self.join_phase == "setup":
                    self._join_phase_deadline = now + self.join_timing.join_seconds
                    self._send_session_join(now, use_clock=use_clock)
            elif n and n[1] == NET_KEEPALIVE_REQUEST:
                # The native 0x41 serializer includes the network id and local
                # station identity because this exchange is connectionless at
                # the outer Pia layer (dst/src/pktid are all zero).
                if self.network_id is not None:
                    self._q(
                        PROTO_NET,
                        build_net_keepalive_ack(
                            self.network_id, self.our_mac, self.our_var
                        ),
                        0,
                        0,
                        False,
                        False,
                        True,
                        pktid=0,
                    )
            elif n and n[1] == NET_UPDATE_PROPERTY:
                # Mid-session 'Update network property' (in the reference capture: at the lobby->trade
                # transition). The host RETRANSMITS it every 500ms until it sees our Net 0x51 ack -> an
                # unanswered 0x50 keeps the host's reliable SEND buffer full (a leading BufferIsFull cause).
                # Echo the 0x50's seqid (its body[0:4]) in a Net 0x51 on dst=0x0000, establishing flag set.
                # NOTE: the reference capture used a "running" establishing pktid (173); we send a non-zero
                # per-channel pktid (unverified - confirm the host accepts it / stops retransmitting 0x50).
                body = n[2]
                if len(body) < 4:
                    return
                seqid = int.from_bytes(body[0:4], "big")
                self._q(PROTO_NET, build_net_property_ack(seqid), 0, self.our_var, False, False, True)
        elif proto == PROTO_SESSION:
            if payload[:1] == b"\x01":
                self.receive_session_wait(payload)
                return
            # Assignment and fragmented Update admission belongs to ZASession.
            # Only its validated Update path may acknowledge a sequence/generation.
        elif proto in (PROTO_RTT, PROTO_RELIABLE):
            if self.state == ST_FINALIZE:
                self.state = ST_CONNECTED
                self.log("host live (RTT/Reliable) -> CONNECTED")
                self.info("Connection established.")
            # Respond only after a validated Session Update has been finalized.
            if proto == PROTO_RTT and self.state != ST_NET and self.host_var is not None:
                r = parse_rtt(payload)
                if r and r["type"] == 0:                  # host request -> respond
                    self._last_host_rtt = bytes(payload[:21])   # template for our own origination
                    self._q(PROTO_RTT, build_rtt_response(payload),
                            SESSION_VAR, self.our_var, False, True, False, footer_var=self.host_var)
                elif r and r["type"] == 1 and tick is not None:
                    # type-1 = the host echoing one of OUR originated requests. Match its systime to the
                    # tick we sent it and record the round-trip; this feeds the reliable RTO.
                    systime = int.from_bytes(r["systime"], "little")
                    sent = self._rtt_pending.pop(systime, None)
                    if sent is not None and tick >= sent:
                        self.rtt_samples.append(tick - sent)

    def drain(self):
        self.raise_if_session_failed()
        out, self._outbox = self._outbox, []
        return out
