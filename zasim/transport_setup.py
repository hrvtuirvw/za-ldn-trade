"""Z-A configuration for the shared LDN transport, without starting a radio.

Trade, capture, handshake and the relay server must pin the same Z-A title,
decoder and room guards. Admission, construction seams and cleanup remain
with their callers; this module only assembles the title-specific options.
"""
from __future__ import annotations

from .link_code import advertised_link_code_matches
from .profile import ZA_BOX_TRADE_MAX_PARTICIPANTS, ZA_BOX_TRADE_SCENE_ID, ZA_PROFILE


def _application_bytes(data, _log):
    return bytes(data or b"")


def make_local_transport(
    factory, *, link_code, communication_id=None,
    scene_id=ZA_BOX_TRADE_SCENE_ID,
    expected_max_participants=ZA_BOX_TRADE_MAX_PARTICIPANTS,
    app_data_decoder=_application_bytes, **settings,
):
    """Construct exactly once; never start, read keys or operate a device here.

    The factory stays injectable at each caller's existing boundary. A fresh
    matcher closes over this room's code, not mutable global or shared state.
    Fixed title options cannot be overridden through ``settings``.
    """
    return factory(
        password=ZA_PROFILE.ldn_passphrase,
        local_comm_id=(ZA_PROFILE.title_id_int if communication_id is None else communication_id),
        scene_id=scene_id,
        game_name=ZA_PROFILE.name,
        app_data_decoder=app_data_decoder,
        auto_app_version=True,
        require_exact_network=True,
        expected_max_participants=expected_max_participants,
        application_data_matcher=lambda data: advertised_link_code_matches(data, link_code),
        # Automatic stock LDN hosts may use any of channels 1/6/11.
        preferred_scan_channel=None,
        max_datagram_size=ZA_PROFILE.max_packet_size,
        **settings,
    )
