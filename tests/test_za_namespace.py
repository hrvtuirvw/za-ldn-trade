"""Z-A-only public package and explicit handshake-profile contracts."""
import ast
import importlib
import inspect
from pathlib import Path
import sys

import pytest

from zasim import ldn
from zasim.ldn import pia_connect, transport
from zasim.profile import ZA_PROFILE, ZA_BOX_TRADE_SCENE_ID
from test_release_polish import gate, artifacts, wheel_files, zip_blob, sdist_blob, VERSION, PAYLOAD


@pytest.mark.parametrize('module', ldn.__all__)
def test_shared_modules_are_owned_by_the_za_namespace(module):
    item = importlib.import_module('zasim.ldn.' + module)
    assert Path(item.__file__).parent == Path(ldn.__file__).parent
    assert item.__name__ == 'zasim.ldn.' + module


@pytest.mark.parametrize('symbol', ['ReplayTransport', '_b85_decode', '_frlg_name',
                                   '_dump_beacon', 'GBA_APP_PASSPHRASE', '_PIA_HDR'])
def test_old_title_implementation_is_not_in_public_transport(symbol):
    assert not hasattr(transport, symbol)


@pytest.mark.parametrize('symbol', ['DEFAULT_APP_VER', 'DEFAULT_PROTOCOLS',
                                   'DEFAULT_PLAYER_ID', 'DEFAULT_IDENTIFICATION_TOKEN',
                                   'NET_WAIT', 'SESSION_WAIT', 'CONNECTED'])
def test_old_builder_defaults_and_aliases_are_absent(symbol):
    assert not hasattr(pia_connect, symbol)


def test_bare_constructor_is_za_and_cannot_invoke_an_old_beacon_decoder():
    instance = transport.LiveTransport(log=lambda _: None)
    assert instance.LOCAL_COMMUNICATION_ID == ZA_PROFILE.title_id_int
    assert instance.SCENE_ID == ZA_BOX_TRADE_SCENE_ID
    assert instance.password == ZA_PROFILE.ldn_passphrase
    assert instance.game_name == ZA_PROFILE.name
    assert instance.nickname == 'ZA-Trade'
    for raw in (None, b'', bytearray(b'synthetic')):
        assert instance.app_data_decoder(raw, lambda _: pytest.fail('unexpected log')) == bytes(raw or b'')
    assert instance._thread is instance._tx is instance._rx is None


@pytest.mark.parametrize('function,required', [
    (pia_connect.build_session_join, ['app_ver', 'protocols', 'player_id', 'identification_token']),
    (pia_connect.ConnectionManager, ['our_var', 'player_name', 'app_ver', 'protocols', 'player_id', 'identification_token']),
    (pia_connect.build_session_finalize, ['sequence', 'generation']),
    (pia_connect.ConnectionManager.acknowledge_session_update, ['sequence', 'generation']),
])
def test_profile_and_validated_update_fields_are_mandatory(function, required):
    params = inspect.signature(function).parameters
    assert all(params[name].default is inspect.Parameter.empty for name in required)
    assert all(params[name].kind is inspect.Parameter.KEYWORD_ONLY for name in required)


def test_unvalidated_session_update_does_not_auto_finalize():
    manager = pia_connect.ConnectionManager(bytes(8), bytes(8), '169.254.1.2', '169.254.1.1',
        our_var=2, player_name='Test', app_ver=b'\x00\x01', protocols=((1, 0),),
        player_id=bytes(16), identification_token=bytes(32), random32=lambda: 0)
    manager.host_var = 3
    manager.on_message(pia_connect.PROTO_SESSION, b'\x05\x00\x01')
    assert manager.drain() == [] and manager.state == pia_connect.ST_NET
    with pytest.raises(TypeError):
        manager.acknowledge_session_update()


def test_public_runtime_imports_do_not_depend_on_the_archived_namespace():
    root = Path(ldn.__file__).parents[1]
    for path in root.rglob('*.py'):
        tree = ast.parse(path.read_bytes())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [n.name for n in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            else:
                continue
            assert not any(n == 'frlgsim' or n.startswith('frlgsim.') for n in names), path


@pytest.mark.parametrize('name', ['frlgsim/__init__.py', 'frlgsim/transport.py',
                               'frlgtrade.py', 'development/compatibility/replay.py'])
def test_export_rejects_archived_inputs_before_any_content_read(tmp_path, monkeypatch, name):
    builder = sys.modules['build_za_release']
    monkeypatch.setattr(builder, 'FILES', (name,))
    monkeypatch.setattr(builder, 'read_regular_file', lambda *a, **k: pytest.fail('content read'))
    with pytest.raises(ValueError, match='archived compatibility'):
        builder.public_payload(tmp_path)


@pytest.mark.parametrize('kind', ['wheel', 'sdist', 'zip'])
def test_final_artifact_gate_rejects_reintroduced_old_modules(tmp_path, monkeypatch, kind):
    extra = {'frlgsim/transport.py': b'# excluded synthetic sentinel\n'}
    if kind == 'wheel':
        files = wheel_files(); files.update(extra)
        with pytest.raises(ValueError, match='member set'):
            gate.check_wheel(zip_blob(files), VERSION, PAYLOAD)
    elif kind == 'sdist':
        with pytest.raises(ValueError, match='member set'):
            gate.check_sdist(sdist_blob(extra=extra), VERSION, PAYLOAD)
    else:
        out = artifacts(tmp_path, monkeypatch)
        (out / f'za-ldn-trade-{VERSION}.zip').write_bytes(gate.archive_bytes(VERSION, {**PAYLOAD, **extra}))
        with pytest.raises(ValueError, match='exact deterministic'):
            gate.check_release(tmp_path, out)
