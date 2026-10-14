"""Inherited/shared-code contracts; all construction and file I/O are synthetic."""
from __future__ import annotations

import builtins
import json
from types import SimpleNamespace

import pytest

from zasim.ldn import transport as shared
from zasim import client, handshake, live, relay
from zasim.gui_controller import ConnectionSettings
from zasim.json_limits import validate_json_depth
from zasim.profile import ZA_PROFILE
from test_relay import _link_application_data
from test_za_client import _occupied_party


@pytest.mark.parametrize('route', ['trade', 'probe', 'handshake', 'relay'])
@pytest.mark.parametrize('helper', [None, 'synthetic-bundle'])
def test_every_local_entry_pins_za_instead_of_inheriting_frlg(monkeypatch, tmp_path, route, helper):
    captured = []
    real_constructor = shared.LiveTransport

    class ReachedConstructor(BaseException):
        pass

    def construct(**options):
        captured.append(options)
        # Exercise the genuine constructor too when it needs no bundle I/O.
        # start() is never called, so no keys/device/socket work is possible.
        if helper is None:
            instance = real_constructor(**options)
            assert instance.LOCAL_COMMUNICATION_ID == ZA_PROFILE.title_id_int
            assert instance.SCENE_ID == 1
            assert instance.password == ZA_PROFILE.ldn_passphrase
            assert instance.app_data_decoder(b'synthetic', lambda _: None) == b'synthetic'
            assert instance.require_exact_network and instance.auto_app_version
            assert instance.expected_max_participants == 2
            assert instance.max_datagram_size == ZA_PROFILE.max_packet_size
            assert instance._thread is None and instance._tx is None and instance._rx is None
        raise ReachedConstructor

    # Change only each adapter's platform view, not global os.name/Path semantics.
    for module in (client, live, handshake, relay):
        monkeypatch.setattr(module, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(client, 'LiveTransport', construct)
    monkeypatch.setattr(live, 'LiveTransport', construct)
    monkeypatch.setattr(handshake, 'LiveTransport', construct)
    monkeypatch.setattr(shared, 'LiveTransport', construct)
    logs = []
    common = dict(keys_path='synthetic.keys', nickname='Audit', phyname='phy-test',
                  ifname='wlan-test', single_interface=True, bss_helper=helper,
                  log=logs.append)
    with pytest.raises(ReachedConstructor):
        if route == 'trade':
            client.run_live_trade(_occupied_party(), link_code='12345678',
                                  confirmation_callback=lambda _: False, **common)
        elif route == 'probe':
            live.run_passive_probe(output=tmp_path/'synthetic.jsonl',
                                   link_code='12345678', **common)
        elif route == 'handshake':
            handshake.run_handshake_diagnostic(link_code='12345678', **common)
        else:
            server = relay.ZARelayServer(**{k:v for k,v in common.items() if k!='nickname'})
            server._local_transport(relay.RelayStartRequest(
                'Audit', ZA_PROFILE.title_id_int, 1, 2, '12345678'))
    assert len(captured) == 1
    options = captured[0]
    assert set(options) == {
        'password', 'nickname', 'keys_path', 'local_comm_id', 'scene_id', 'phyname',
        'ifname', 'log', 'game_name', 'app_data_decoder', 'auto_app_version',
        'require_exact_network', 'expected_max_participants', 'application_data_matcher',
        'single_interface', 'preferred_scan_channel', 'max_datagram_size',
        *({'bss_helper'} if helper is not None else set()),
    }
    expected = dict(password=ZA_PROFILE.ldn_passphrase, nickname='Audit',
                    keys_path='synthetic.keys', local_comm_id=ZA_PROFILE.title_id_int,
                    scene_id=1, phyname='phy-test', ifname='wlan-test', log=logs.append,
                    game_name=ZA_PROFILE.name, auto_app_version=True,
                    require_exact_network=True, expected_max_participants=2,
                    single_interface=True, preferred_scan_channel=None,
                    max_datagram_size=ZA_PROFILE.max_packet_size)
    assert all(options[name] == value for name, value in expected.items())
    if helper is not None:
        assert options['bss_helper'] == helper
    matcher = options['application_data_matcher']
    assert matcher(_link_application_data())
    assert not matcher(_link_application_data('87654321'))
    assert not matcher(b'12345678')
    assert not matcher(_link_application_data()+b'extra')
    logs.clear()
    for value in (None, b'', bytearray(b'abc')):
        assert options['app_data_decoder'](value, logs.append) == bytes(value or b'')
    assert bool(logs) is (route == 'probe')
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('route', ['probe', 'relay'])
def test_explicit_nondefault_profile_fields_are_not_lost(monkeypatch, tmp_path, route):
    captured = {}
    marker = object()
    class ReachedConstructor(BaseException):
        pass
    def construct(**options):
        captured.update(options)
        if route == 'probe':
            raise ReachedConstructor
        return marker
    if route == 'probe':
        monkeypatch.setattr(live, 'os', SimpleNamespace(name='posix'))
        monkeypatch.setattr(live, 'LiveTransport', construct)
        with pytest.raises(ReachedConstructor):
            live.run_passive_probe(output=tmp_path/'capture.jsonl', keys_path='synthetic.keys',
                                   comm_id=123, scene_id=7, link_code='12345678')
        assert captured['expected_max_participants'] == 2
    else:
        monkeypatch.setattr(shared, 'LiveTransport', construct)
        server = relay.ZARelayServer(keys_path=None, transport_factory=lambda _: None)
        assert server._local_transport(relay.RelayStartRequest(
            'Audit', 123, 7, 4, '12345678')) is marker
        assert captured['expected_max_participants'] == 4
    assert captured['local_comm_id'] == 123 and captured['scene_id'] == 7


@pytest.mark.parametrize('depth', [0, 1, 31, 32, 33, 64, 65])
def test_json_nesting_shared_semantics_preserve_independent_caps(depth):
    # Escaped quotes/backslashes and brackets inside strings are not nesting.
    leaf = json.dumps({'text': '"\\[]{}'}, separators=(',', ':')).encode()
    raw = b'[' * depth + leaf + b']' * depth
    actual_depth = depth + 1
    if actual_depth > relay.MAX_RELAY_JSON_DEPTH:
        with pytest.raises(relay.RelayProtocolError,
                           match=r'^relay sent malformed JSON \(nesting limit exceeded\)$'):
            relay._validate_json_nesting(raw)
    else:
        relay._validate_json_nesting(raw)
    if actual_depth > 64:
        with pytest.raises(ValueError, match='JSON nesting exceeds 64'):
            validate_json_depth(raw)
    else:
        validate_json_depth(raw)
    assert json.loads(raw) is not None


def test_relay_depth_guard_uses_the_common_scanner(monkeypatch):
    import zasim.json_limits as limits
    calls = []
    original = limits.validate_json_depth
    def counted(raw, *, max_depth=64):
        calls.append((raw, max_depth))
        return original(raw, max_depth=max_depth)
    # Resolve either qualified or direct imported form at its real boundary.
    monkeypatch.setattr(limits, 'validate_json_depth', counted)
    if hasattr(relay, 'validate_json_depth'):
        monkeypatch.setattr(relay, 'validate_json_depth', counted)
    relay._validate_json_nesting(b'{"text":"[\\\"}"}')
    assert calls == [(b'{"text":"[\\\"}"}', 32)]




@pytest.mark.parametrize('once', [False, True])
def test_relay_service_preserves_result_without_unread_counter_work(monkeypatch, once):
    calls = []
    logs = []
    conversions = []
    result = dict(packets_to_windows=3, packets_from_windows=4, listen='127.0.0.1:1')
    class Server:
        def __init__(self, **kwargs):
            self.count = 0
        def open(self):
            return '127.0.0.1:1'
        def serve_once(self):
            self.count += 1
            calls.append('session')
            if self.count == 3:
                raise KeyboardInterrupt
            return result
        def close(self):
            calls.append('close')
    def counted_int(value):
        conversions.append(value)
        return builtins.int(value)
    monkeypatch.setattr(relay, 'ZARelayServer', Server)
    monkeypatch.setattr(relay, 'int', counted_int, raising=False)
    if once:
        assert relay.serve_za_relay(keys_path='synthetic.keys', once=True, log=logs.append) is result
        assert calls == ['session', 'close']
    else:
        with pytest.raises(KeyboardInterrupt):
            relay.serve_za_relay(keys_path='synthetic.keys', log=logs.append)
        assert calls == ['session']*3 + ['close']
        assert any('session 1 finished' in s for s in logs)
        assert any('session 2 finished' in s for s in logs)
    assert conversions == []


@pytest.mark.parametrize('nickname', ['A'*20, 'あ'*10, '\U0001f600'*8])
def test_gui_connection_validates_nickname_once_per_direct_validation(nickname):
    lengths = []
    class CountedName(str):
        def __len__(self):
            lengths.append(True)
            return super().__len__()
    settings = ConnectionSettings(backend='direct', nickname=CountedName(nickname))
    assert settings.validate(os_name='posix', sys_platform='linux') is settings
    # One truth-value test and one character-count check in the shared validator.
    assert len(lengths) == 2


@pytest.mark.parametrize('nickname', ['', 'A'*21, 'あ'*11, '\U0001f600'*9, '\ud800', None])
def test_gui_connection_still_rejects_all_ldn_name_boundaries(nickname):
    from zasim.ldn.ldn_name import validate_ldn_nickname
    with pytest.raises((TypeError, ValueError)) as expected:
        validate_ldn_nickname(nickname)
    with pytest.raises(type(expected.value)) as actual:
        ConnectionSettings(backend='direct', nickname=nickname).validate(
            os_name='posix', sys_platform='linux')
    assert str(actual.value) == str(expected.value)


@pytest.mark.parametrize('name,value', [
    ('password', b'wrong-title'), ('game_name', 'FRLG'),
    ('require_exact_network', False), ('auto_app_version', False),
    ('preferred_scan_channel', 6), ('max_datagram_size', 65507),
])
def test_central_setup_cannot_override_fixed_title_guards(name, value):
    from zasim.transport_setup import make_local_transport
    calls = []
    with pytest.raises(TypeError):
        make_local_transport(lambda **kw: calls.append(kw), link_code='12345678',
                             **{name: value})
    assert calls == []


def test_central_setup_keeps_independent_rooms_and_factory_failure_identity():
    from zasim.transport_setup import make_local_transport
    options = []
    marker = object()
    def construct(**kw):
        options.append(kw)
        return marker
    assert make_local_transport(construct, link_code='12345678') is marker
    assert make_local_transport(construct, link_code='87654321') is marker
    first, second = (o['application_data_matcher'] for o in options)
    assert first is not second
    assert first(_link_application_data()) and not first(_link_application_data('87654321'))
    assert second(_link_application_data('87654321')) and not second(_link_application_data())
    error = OSError('synthetic constructor failure')
    calls = []
    def fail(**kw):
        calls.append(kw)
        raise error
    with pytest.raises(OSError) as caught:
        make_local_transport(fail, link_code='12345678')
    assert caught.value is error and len(calls) == 1


def test_historical_import_aliases_keep_their_original_objects():
    from zasim.ldn import ldn_name, pia_connect
    from zasim import compatibility, session
    assert shared.validate_ldn_nickname is ldn_name.validate_ldn_nickname
    assert shared.MAX_LDN_NICKNAME_BYTES == ldn_name.MAX_LDN_NICKNAME_BYTES
    assert shared.MAX_LDN_NICKNAME_CHARACTERS == ldn_name.MAX_LDN_NICKNAME_CHARACTERS
    assert session.ZA_IDENTIFICATION_TOKEN is compatibility.ZA_IDENTIFICATION_TOKEN
    assert (pia_connect.ST_NET, pia_connect.ST_FINALIZE, pia_connect.ST_CONNECTED) == (
        "net", "finalize", "connected")
    assert not any(hasattr(pia_connect, name) for name in ("NET_WAIT", "SESSION_WAIT", "CONNECTED"))
