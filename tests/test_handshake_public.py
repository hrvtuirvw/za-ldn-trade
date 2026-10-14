"""Synthetic generic handshake with real crypto/compression and no radio/keys.

The peer state is a model, not a console. No private development module or raw
historical capture is an input to these public CI regressions.
"""
from collections import deque
from itertools import count
import threading

import pytest

import zasim.handshake as handshake
from zasim.ldn.pia_connect import ldn_constant_id
from zasim.pia import PiaCrypto, PiaHeader, build_message, decode_datagram, encode_datagram
from zasim.session import ZASession
from za_session_fixtures import acceptance

OUR_IP, HOST_IP = '169.254.7.2', '169.254.7.1'
OUR_MAC, HOST_MAC = bytes.fromhex('020000000001'), bytes.fromhex('020000000002')
OUR_VAR, HOST_VAR = 0x2345, 0x3456


class Clock:
    def __init__(self): self.now = 0.0
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds


@pytest.mark.parametrize('compressed', [False, True])
@pytest.mark.parametrize('fault', [None, 'receive', 'stop', 'both'])
def test_real_generic_handshake_never_sends_trade_and_preserves_errors(monkeypatch, compressed, fault):
    clock = Clock()
    crypto = PiaCrypto(bytes(range(16)), game_key=bytes(reversed(range(16))))
    assignment, update = acceptance(
        ldn_constant_id(OUR_MAC) + OUR_VAR.to_bytes(2, 'big'),
        ldn_constant_id(HOST_MAC) + HOST_VAR.to_bytes(2, 'big'))
    fixed = ((9).to_bytes(4, 'big') + HOST_VAR.to_bytes(2, 'big')
             + ldn_constant_id(HOST_MAC) + bytes.fromhex('0000000011223344')
             + bytes((1, 0, 0, 0)))
    rtt = bytearray(21); rtt[3] = 5
    rtt[8:16] = (123456).to_bytes(8, 'little')
    rtt[19:21] = HOST_VAR.to_bytes(2, 'big')
    messages = deque([(1, bytes.fromhex('01110000') + fixed, 0),
                      (13, update, OUR_VAR), (3, bytes(rtt), 1)])
    protocols = []; nonce = count(1); accepted = []
    receive_error = RuntimeError('synthetic receive fault')
    stop_error = RuntimeError('synthetic cleanup fault')

    class Transport:
        our_ip, host_ip = OUR_IP, HOST_IP
        broadcast = '169.254.7.255'
        our_mac, host_mac = OUR_MAC, HOST_MAC
        ssid = bytes(range(16))
        app_data = bytes.fromhex('005c160006') + bytes(0x5C - 5)
        starts = stops = 0
        def start(self, cancel_event=None): self.starts += 1
        def recv(self):
            clock.now += .1
            if not messages:
                if fault in ('receive', 'both'): raise receive_error
                return []
            protocol, body, destination = messages.popleft()
            number = next(nonce)
            header = PiaHeader(dst=destination, src=HOST_VAR, packet_id=number,
                               nonce=number.to_bytes(8, 'big'), flags=2 if protocol == 1 else 0)
            compact = build_message(protocol, body)
            if protocol == 13: compact = build_message(13, assignment) + compact
            return [(encode_datagram(compact, HOST_IP, header, crypto=crypto,
                                     compress=compressed), HOST_IP)]
        def send(self, raw, destination):
            assert destination == HOST_IP
            packet = decode_datagram(raw, OUR_IP, crypto=crypto)
            protocols.extend(message.proto for message in packet.messages)
            assert set(protocols) <= {1, 3, 13}
        def stop(self):
            self.stops += 1
            if fault in ('stop', 'both'): raise stop_error

    transport = Transport(); randoms = count(100)
    monkeypatch.setattr(handshake, 'time', clock)
    monkeypatch.setattr(handshake, 'PiaCrypto', lambda *a, **k: crypto)
    monkeypatch.setattr(handshake, 'ZASession', lambda **kw: ZASession(
        **kw, our_var=OUR_VAR, random4=b'TEST', player_id=bytes(range(16)),
        clock=clock.monotonic, random32=lambda: next(randoms)))
    def log(message):
        if message == '[handshake] Net/Session/RTT accepted; no Reliable or BoxTrade payload was sent':
            accepted.append(True)
    def run():
        return handshake.run_handshake_diagnostic(transport_factory=lambda: transport,
                    timeout=2, settle_grace=.25, log=log, cancel_event=threading.Event())
    if fault is None:
        report = run()
        assert report['completed'] and report['handshake_only']
        assert report['reliable_messages_sent'] == report['boxtrade_commands_sent'] == 0
    else:
        with pytest.raises(RuntimeError) as caught: run()
        assert caught.value is (stop_error if fault in ('stop', 'both') else receive_error)
        if fault == 'both': assert caught.value.__context__ is receive_error
    assert accepted == [True]
    assert transport.starts == transport.stops == 1
    assert set(protocols) == {1, 3, 13}


def test_documented_handshake_output_passes_real_cli_preflight(tmp_path):
    from pathlib import Path
    import re
    import shlex
    from zatrade import build_parser
    root = Path(handshake.__file__).parents[1]
    text = (root / 'README_ZA.md').read_text(encoding='utf-8')
    blocks = re.findall(r'^```(?:sh|bash)\r?\n(.*?)^```[ \t]*$', text, re.S | re.M)
    checked = 0
    for block in blocks:
        if 'zatrade.py handshake' not in block:
            continue
        words = shlex.split(block.replace('\\\n', ' '))
        start = words.index('zatrade.py') + 1
        args = build_parser().parse_args(words[start:])
        assert args.func.__name__ == 'cmd_handshake'
        target, _ = handshake._capture_target(tmp_path / Path(args.output).name,
                     overwrite=False, expected_output_identity=None)
        assert target.suffix == '.jsonl'
        checked += 1
    assert checked == 1
