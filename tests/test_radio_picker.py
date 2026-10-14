"""Selection-first UI; all radios/commands are synthetic, never hardware."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import threading

import pytest

from zasim import radio_picker as p
from zasim import wireless as w
from zasim.control import OperationCancelled
from zasim.gui_controller import ConnectionSettings, GuiController, ProbeRequest, ControllerState


@pytest.fixture
def radios(tmp_path, monkeypatch):
    # Model symlink resolution with real directory/stat objects, also on Windows
    # where creating symlinks normally requires extra privileges.
    net = tmp_path / 'net'; net.mkdir()
    phys = tmp_path / 'ieee80211'; phys.mkdir()
    aliases = {}
    original_resolve, original_stat = Path.resolve, Path.stat
    monkeypatch.setattr(Path, 'resolve', lambda self, *a, **kw:
                        aliases[self] if self in aliases else original_resolve(self, *a, **kw))
    monkeypatch.setattr(Path, 'stat', lambda self, *a, **kw:
                        original_stat(aliases.get(self, self), *a, **kw))
    def add(name='wlan7', phy='phy7', driver='mt76x0u', carrier='0'):
        entry = net / name; entry.mkdir()
        radio = phys / phy; radio.mkdir(exist_ok=True)
        (radio / 'index').write_text(phy[3:])
        (entry / 'phy80211').mkdir()
        aliases[entry / 'phy80211'] = radio
        module = tmp_path / 'module' / driver; module.mkdir(parents=True, exist_ok=True)
        (entry / 'device/driver/module').mkdir(parents=True)
        aliases[entry / 'device/driver/module'] = module
        (entry / 'carrier').write_text(carrier)
        (entry / 'flags').write_text('0x0')
        return entry
    first = add()
    monkeypatch.setattr(p, 'SYSFS', net)
    monkeypatch.setattr(p.sys, 'platform', 'linux')
    return SimpleNamespace(net=net, phys=phys, first=first, add=add, aliases=aliases)


def test_inventory_binds_real_sysfs_identities_and_does_not_certify(radios):
    snapshot = p.read_inventory()
    assert snapshot.status == 'ok' and len(snapshot.radios) == 1
    choice = snapshot.radios[0]
    assert (choice.interface, choice.phy, choice.driver) == ('wlan7', 'phy7', 'mt76x0u')
    assert len(choice.identity) == 6 and choice.state == 'unchecked'
    assert p.resolve_choice(choice, snapshot) == choice
    with pytest.raises(ValueError):
        p.resolve_choice(None, snapshot)


@pytest.mark.parametrize('platform', ['win32', 'darwin'])
def test_non_linux_never_enumerates_local_or_remote_devices(monkeypatch, platform):
    monkeypatch.setattr(p, 'inventory_radios', lambda *_: pytest.fail('must not enumerate'))
    assert p.read_inventory(platform=platform).status == 'not_linux'


def test_missing_inventory_is_unknown_but_readable_empty_is_empty(tmp_path):
    assert p.read_inventory(sysfs=tmp_path / 'missing', platform='linux').status == 'unknown'
    result = p.read_inventory(sysfs=tmp_path, platform='linux')
    assert result.status == 'ok' and not result.radios


@pytest.mark.parametrize('reason', ['carrier', 'soft', 'hard'])
def test_busy_or_disabled_sibling_blocks_the_entire_physical_radio(radios, reason):
    sibling = radios.add('wlan7-extra')
    if reason == 'carrier':
        (sibling / 'carrier').write_text('1')
    else:
        rf = radios.phys / 'phy7/rfkill0'; rf.mkdir()
        # glob traverses the fake alias directory, so add the same state there.
        for entry in (radios.first, sibling):
            target = entry / 'phy80211/rfkill0'; target.mkdir()
            (target / 'soft').write_text('1' if reason == 'soft' else '0')
            (target / 'hard').write_text('1' if reason == 'hard' else '0')
    snapshot = p.read_inventory()
    assert len(snapshot.radios) == 2
    for choice in snapshot.radios:
        assert choice.state == ('in_use' if reason == 'carrier' else 'blocked')
        with pytest.raises(ValueError):
            p.resolve_choice(choice, snapshot)


def test_label_escapes_product_text_and_does_not_expose_private_identity(radios):
    (radios.first / 'device/product').write_text('Adapter\x1b fake\nline')
    choice = p.read_inventory().radios[0]
    label = choice.label()
    assert '\x1b' not in label and '\n' not in label
    assert '\\u001B' in label and 'wlan7' in label
    assert str(radios.net) not in label and str(choice.identity) not in label


def test_changed_identity_and_reused_names_are_not_the_selected_device(radios):
    choice = p.read_inventory().radios[0]
    new = replace(choice, identity=(*choice.identity[:-1], choice.identity[-1] + 1))
    with pytest.raises(ValueError):
        p.resolve_choice(choice, p.RadioInventory('ok', (new,)))
    with pytest.raises(ValueError):
        p.resolve_choice(choice, p.RadioInventory('ok', (choice, choice)))
    with pytest.raises(ValueError):
        p.resolve_choice(choice, p.RadioInventory('ok'))


def test_inventory_failure_and_mid_scan_hotplug_fail_closed(radios, monkeypatch):
    real = p.inventory_radios
    count = 0
    def change(path):
        nonlocal count
        count += 1
        return real(path) if count == 1 else []
    monkeypatch.setattr(p, 'inventory_radios', change)
    assert p.read_inventory().status == 'unknown'


@pytest.fixture
def diagnostic(radios, monkeypatch):
    from test_wireless import IW
    calls = []
    def command(argv):
        calls.append(argv)
        if argv == ['iw', 'phy', 'phy7', 'info']:
            return IW
        if argv == ['ip', '-j', 'address', 'show']:
            return json.dumps([{'ifname': r.interface, 'addr_info': []}
                               for r in p.read_inventory().radios])
        if argv in (['ip', '-j', '-4', 'route', 'show', 'table', 'all'],
                    ['ip', '-j', '-6', 'route', 'show', 'table', 'all']):
            return '[]'
        pytest.fail('not an expected read-only query: ' + repr(argv))
    monkeypatch.setattr(w, '_command', command)
    monkeypatch.setattr(w, 'read_control_port', lambda *_: {
        'custom_control_port_ethertype': True, 'control_port_over_nl80211': True})
    return SimpleNamespace(calls=calls, command=command, choice=p.read_inventory().radios[0])


def test_real_preflight_queries_only_selected_phy_and_preserves_files(radios, diagnostic):
    before = {str(f): f.read_bytes() for f in radios.net.rglob('*') if f.is_file()}
    radios.add('wlan8', 'phy8')
    p.preflight_choice(diagnostic.choice)
    assert diagnostic.calls[0] == ['iw', 'phy', 'phy7', 'info']
    assert len(diagnostic.calls) == 4
    assert not any('phy8' in call for call in diagnostic.calls)
    assert all(Path(name).read_bytes() == data for name, data in before.items())


@pytest.mark.parametrize('fault', ['default-route', 'address', 'feature', 'unknown', 'changed-after'])
def test_actual_diagnostic_refuses_unsafe_or_unconfirmed_selection(radios, diagnostic, monkeypatch, fault):
    def command(argv):
        if fault == 'default-route' and argv[:3] == ['ip', '-j', '-6']:
            return '[{"dst":"default","dev":"wlan7"}]'
        if fault == 'address' and argv == ['ip', '-j', 'address', 'show']:
            return '[{"ifname":"wlan7","addr_info":[{"local":"192.0.2.2"}]}]'
        if fault == 'unknown':
            raise OSError('synthetic unavailable')
        text = diagnostic.command(argv)
        if fault == 'feature' and argv[0] == 'iw':
            return text.replace('* monitor', '* AP')
        if fault == 'changed-after' and argv == ['ip', '-j', 'address', 'show']:
            # New object with the same interface/PHY strings after diagnostics.
            device = radios.first / 'device'
            device.rename(radios.first / 'old-device')
            device.mkdir(); (device / 'driver/module').mkdir(parents=True)
        return text
    monkeypatch.setattr(w, '_command', command)
    with pytest.raises(ValueError):
        p.preflight_choice(diagnostic.choice)


@pytest.mark.parametrize('when', ['before', 'during'])
def test_cancel_during_preflight_never_allows_transport(radios, diagnostic, monkeypatch, when):
    cancel = threading.Event()
    if when == 'before':
        cancel.set()
    else:
        def command(argv):
            cancel.set()
            return diagnostic.command(argv)
        monkeypatch.setattr(w, '_command', command)
    with pytest.raises(OperationCancelled):
        p.preflight_choice(diagnostic.choice, cancel_event=cancel)
    if when == 'before':
        assert not diagnostic.calls


def test_connection_maps_selection_and_reuse_without_overriding_other_backends(radios, diagnostic):
    choice = diagnostic.choice
    normal = ConnectionSettings(backend='direct', phyname=choice.phy, selected_radio=choice)
    kwargs = normal.live_kwargs(os_name='posix')
    assert kwargs['phyname'] == 'phy7' and kwargs['ifname'] == 'ldnclient'
    assert 'single_interface' not in kwargs and 'selected_radio' not in kwargs
    reuse = replace(normal, single_interface=True, ifname='wlan7')
    kwargs = reuse.live_kwargs(os_name='posix')
    assert kwargs['single_interface'] is True and kwargs['ifname'] == 'wlan7'
    for bad in (replace(normal, phyname='phy0'), replace(normal, backend='relay'),
                replace(normal, ifname='wlan7'), replace(reuse, ifname='wlan8')):
        with pytest.raises(ValueError):
            bad.validate(os_name='posix')


def test_worker_preflight_is_off_ui_thread_and_failed_check_never_calls_runner(radios, diagnostic, tmp_path, monkeypatch):
    from test_gui_controller import _wait_for, _events_by_kind
    entered, release = threading.Event(), threading.Event()
    def command(argv):
        assert threading.current_thread() is not threading.main_thread()
        entered.set(); assert release.wait(2)
        raise OSError('synthetic read-only failure')
    monkeypatch.setattr(w, '_command', command)
    controller = GuiController(os_name='posix', probe_runner=lambda **kw: pytest.fail('no transport on failed check'))
    request = ProbeRequest(connection=ConnectionSettings(backend='direct', phyname='phy7', selected_radio=diagnostic.choice),
                           output=tmp_path/'capture.jsonl', link_code='12345678', duration=1)
    controller.start_probe(request)
    try:
        assert entered.wait(1) and controller.is_busy
    finally:
        release.set()
        _wait_for(lambda: not controller.is_busy)
    assert controller.state == ControllerState.FAILED
    assert 'failed' in _events_by_kind(controller)
    assert not request.output.exists()


def test_connection_has_readonly_local_radio_picker(monkeypatch, radios):
    from test_gui_layout import make_app, cleanup, settle
    root, app, *_ = make_app(monkeypatch)
    try:
        app.direct_radio.invoke(); settle(root)
        assert str(app.radio_combo.cget('state')) == 'readonly'
        assert app.radio_combo.winfo_viewable()
        assert not app._selected_radio  # one adapter is not implicit consent
        app.radio_combo.current(0); app.radio_combo.event_generate('<<ComboboxSelected>>')
        settle(root)
        connection = app._connection()
        assert connection.phyname == 'phy7' and connection.selected_radio.interface == 'wlan7'
        assert not app.phy_entry.winfo_viewable() and not app.interface_entry.winfo_viewable()
    finally:
        cleanup(root)


def test_relay_and_phy_are_not_normal_connection_fields(monkeypatch, radios):
    from test_gui_layout import make_app, cleanup, settle
    root, app, *_ = make_app(monkeypatch)
    try:
        settle(root)
        assert not app.relay_entry.winfo_viewable()
        assert 'Relay' not in app.relay_radio.cget('text')
        app.connection_advanced_check.invoke(); settle(root)
        assert app.relay_entry.winfo_viewable()
        app.connection_advanced_check.invoke()
        app.direct_radio.invoke(); settle(root)
        assert not app.phy_entry.winfo_viewable()
        app.connection_advanced_check.invoke(); settle(root)
        assert app.phy_entry.winfo_viewable() and str(app.phy_entry.cget('state')) == 'readonly'
    finally:
        cleanup(root)


def test_refresh_language_and_backend_preserve_only_exact_hardware_choice(monkeypatch, radios):
    from test_gui_layout import make_app, cleanup, settle
    root, app, errors, _, callbacks = make_app(monkeypatch)
    try:
        app.direct_radio.invoke(); app.radio_combo.current(0); app._radio_selected()
        selected = app._selected_radio
        radios.add('wlan1', 'phy1')  # sorting changes the combobox position
        app.radio_refresh_button.invoke()
        assert app._selected_radio.key == selected.key and app.radio_combo.current() == 1
        for language in ('en', 'ja', 'en'):
            assert app.change_language(language)
            settle(root)
            assert app._selected_radio.key == selected.key
            assert app._connection().phyname == 'phy7'
        app.relay_radio.invoke()
        assert app._connection().selected_radio is None and not app.radio_panel.winfo_viewable()
        app.direct_radio.invoke()
        assert app._selected_radio.key == selected.key
        old = radios.first / 'device'; old.rename(radios.first / 'old-device')
        old.mkdir(); (old / 'driver/module').mkdir(parents=True)
        app.radio_refresh_button.invoke()
        assert app._selected_radio is None and not app.phy_var.get()
        with pytest.raises(ValueError): app._connection()
        assert not errors and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize('status', ['ok', 'unknown', 'not_linux'])
def test_empty_unavailable_and_non_linux_are_distinct_and_not_selectable(monkeypatch, status):
    from test_gui_layout import make_app, cleanup
    monkeypatch.setattr('zasim.gui.read_inventory', lambda: p.RadioInventory(status))
    root, app, *_ = make_app(monkeypatch, language='en')
    try:
        app.backend_var.set('direct'); app._backend_selected()
        notice = app.radio_notice.cget('text')
        assert ('No Wi-Fi' if status == 'ok' else 'unavailable' if status == 'unknown' else 'not available') in notice
        with pytest.raises(ValueError): app._connection()
    finally:
        cleanup(root)


def test_busy_controls_and_stale_callback_cannot_change_selection(monkeypatch, radios):
    from test_gui_layout import make_app, cleanup
    root, app, *_ = make_app(monkeypatch)
    try:
        radios.add('wlan8', 'phy8')
        app.direct_radio.invoke(); app.radio_combo.current(0); app._radio_selected()
        selected = app._selected_radio
        monkeypatch.setattr(type(app.controller), 'is_busy', property(lambda _: True))
        app._set_live_busy(True)
        assert str(app.radio_combo.cget('state')) == 'disabled'
        assert 'disabled' in app.radio_refresh_button.state()
        app.radio_combo.current(1); app._radio_selected(); app._refresh_radios()
        assert app._selected_radio == selected
        assert app.radio_combo.current() == 0
    finally:
        cleanup(root)


@pytest.mark.parametrize('language', ['ja', 'en'])
@pytest.mark.parametrize('scale,size', [(1.3333, '1080x820'), (2.0, '800x600'), (2.6667, '640x480')])
def test_selection_controls_are_reachable_and_operation_settings_are_real(monkeypatch, radios, scale, size, language):
    from test_gui_layout import make_app, cleanup, settle, reveal, visible
    root, app, errors, _, callbacks = make_app(monkeypatch, scale, language)
    try:
        root.geometry(size)
        radios.add('wlan8', 'phy8')
        app.direct_radio.invoke(); settle(root)
        for control in (app.radio_combo, app.radio_refresh_button, app.keys_button, app.connection_advanced_check):
            reveal(app, control); visible(control, app.body.canvas)
        app.radio_combo.current(1); app.radio_combo.event_generate('<<ComboboxSelected>>')
        settle(root)
        assert app._connection().phyname == 'phy8'
        app.connection_advanced_check.invoke()
        app.single_interface_combo.current(1)
        assert app._connection().ifname == 'wlan8' and app._connection().single_interface is True
        assert not errors and not callbacks
    finally:
        cleanup(root)


def test_app_variables_bind_to_its_root_not_an_unrelated_default_root(monkeypatch):
    import tkinter as tk
    from test_gui_layout import make_app, cleanup, settle
    try:
        other = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(str(exc))
    root = None
    try:
        root, app, *_ = make_app(monkeypatch)
        assert root.tk is not other.tk
        assert app.backend_var._tk is root.tk
        # Windows deliberately disables Direct Linux. Exercise an enabled
        # radio's real variable transition without enabling a forbidden mode.
        direct_enabled = 'disabled' not in app.direct_radio.state()
        app.backend_var.set('unselected')
        (app.direct_radio if direct_enabled else app.relay_radio).invoke()
        settle(root)
        assert app.backend_var.get() == ('direct' if direct_enabled else 'relay')
        assert app.change_language('en')
        assert app.radio_var._tk is root.tk
        assert app.editor_notice_var._tk is root.tk
        assert all(var._tk is root.tk for var in app.editor_vars.values())
    finally:
        if root is not None:
            cleanup(root)
        cleanup(other)


@pytest.mark.parametrize('name', ['wlan7', 'eth0', '../wlan7', 'verylonginterface123', '-bad'])
def test_virtual_interface_names_cannot_target_an_existing_or_invalid_device(radios, diagnostic, name):
    (radios.net / 'eth0').mkdir()
    with pytest.raises(ValueError):
        p.preflight_choice(diagnostic.choice, ifname=name, reuse=False)
    assert not diagnostic.calls


def test_new_sibling_during_diagnostic_invalidates_positive_result(radios, diagnostic, monkeypatch):
    def command(argv):
        text = diagnostic.command(argv)
        if argv == ['ip', '-j', 'address', 'show']:
            radios.add('wlan7-extra')
        return text
    monkeypatch.setattr(w, '_command', command)
    with pytest.raises(ValueError):
        p.preflight_choice(diagnostic.choice)


def test_successful_worker_handoff_uses_selected_physical_radio_not_list_index(radios, diagnostic, tmp_path):
    from test_gui_controller import _wait_for
    received = []
    def runner(**kwargs):
        assert len(diagnostic.calls) == 4  # actual read-only checks ran first
        received.append(kwargs)
        return {'packets': 0}
    connection = ConnectionSettings(backend='direct', phyname='phy7', ifname='wlan7',
                                    single_interface=True, selected_radio=diagnostic.choice)
    controller = GuiController(os_name='posix', probe_runner=runner)
    request = ProbeRequest(connection=connection, output=tmp_path/'capture.jsonl',
                           link_code='12345678', duration=1)
    controller.start_probe(request)
    _wait_for(lambda: not controller.is_busy)
    assert controller.state == ControllerState.SUCCEEDED
    assert len(received) == 1
    assert received[0]['phyname'] == 'phy7' and received[0]['ifname'] == 'wlan7'
    assert received[0]['single_interface'] is True and 'selected_radio' not in received[0]


def test_cli_default_is_platform_choice_not_an_implicit_phy_zero():
    from zatrade import build_parser
    args = build_parser().parse_args(['gui'])
    assert args.backend is None and args.phy == ''


@pytest.mark.parametrize('language', ['ja', 'en'])
def test_normal_local_view_shows_primary_action_without_expert_fields(monkeypatch, radios, language):
    from test_gui_layout import make_app, cleanup, settle, visible
    root, app, _, _, callbacks = make_app(monkeypatch, language=language)
    try:
        root.geometry('1080x820')
        app.direct_radio.invoke();settle(root)
        app.body.canvas.yview_moveto(0);settle(root)
        visible(app.radio_combo, app.body.canvas)
        # The primary action is pinned outside the form, including Direct mode.
        visible(app.trade_button, root)
        assert not app.body.contains(app.trade_button)
        for widget in (app.phy_entry, app.interface_entry, app.nickname_entry, app.title_version_combo):
            assert not widget.winfo_viewable()
        assert not callbacks
    finally:
        cleanup(root)
