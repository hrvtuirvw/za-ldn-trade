"""Public payload necessity checks, not evidence of physical exchange."""
import importlib
import sys
import pytest
from test_release_polish import gate

builder = sys.modules['build_za_release']
LEGACY = 'barrier basestats block charmap crypto exception_metadata gbaframe linkplayer linkstate mon ni reliable rfu sim stats trade'.split()
FORBIDDEN = ['frlgsim/' + name + '.py' for name in LEGACY] + ['zasim/rejection_probe.py']
SHARED = ['bss_seed', 'ldn_name', 'ldn_single', 'pia_connect', 'transport']


@pytest.mark.parametrize('name', FORBIDDEN)
def test_unneeded_module_is_not_a_public_distribution_input(name):
    assert name not in builder.FILES


def test_required_shared_modules_and_package_declarations_agree():
    from zasim import ldn
    assert set(builder.MODULES['zasim.ldn']) == {'__init__', *SHARED}
    assert set(ldn.__all__) == set(SHARED)
    for name in SHARED:
        assert 'zasim/ldn/' + name + '.py' in builder.FILES


@pytest.mark.parametrize('module,symbol', [
    ('zasim.user_data', 'make_lab_user_data'),
    ('zasim.client', '_is_final_agreement_outbound'),
    ('zasim.client', '_outbound_trade_command_id'),
    ('zasim.handshake', '_session_join_with_station_port'),
])
def test_removed_test_only_or_unreferenced_private_helpers(module, symbol):
    assert not hasattr(importlib.import_module(module), symbol)


def test_obsolete_unbound_scroll_callback_is_not_a_runtime_api():
    from zasim.gui import _DesktopApp
    assert not hasattr(_DesktopApp, '_editor_mousewheel')
    assert callable(_DesktopApp._mousewheel)
