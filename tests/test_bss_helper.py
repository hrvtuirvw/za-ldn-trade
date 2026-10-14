"""Offline tests: never load modules, read keys or operate a real radio."""
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import sys
from types import SimpleNamespace

import pytest

from zasim.ldn import bss_seed as seed
from zasim import bss_helper as builder

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux kernel helper")


def elf_image(*, name="ldn_bss_seed", release=None, machine=None, license="GPL"):
    """Small independent ELF64 fixture; actual Kbuild is a separate smoke."""
    names = b"\0.shstrtab\0.modinfo\0"
    info = f"name={name}\0vermagic={release or platform.release()} SMP\0license={license}\0".encode()
    raw = bytearray(64 + 3 * 64)
    raw[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", raw, 16, 1, machine or {"x86_64": 62, "aarch64": 183}.get(platform.machine(), 62))
    struct.pack_into("<Q", raw, 40, 64)
    struct.pack_into("<HHH", raw, 58, 64, 3, 1)
    for i, (offset, data) in enumerate(((1, names), (11, info)), 1):
        struct.pack_into("<IIQQQQIIQQ", raw, 64 + i * 64, offset, 1, 0, 0, len(raw), len(data), 0, 0, 1, 0)
        raw.extend(data)
    return bytes(raw)


def bundle_at(path, image=None, **receipt_changes):
    path.mkdir()
    image = image or elf_image()
    (path / "ldn_bss_seed.ko").write_bytes(image)
    receipt = dict(schema=1, kernel_release=platform.release(), source_sha256=seed.SOURCE_SHA256,
                   module_sha256=hashlib.sha256(image).hexdigest())
    receipt.update(receipt_changes)
    (path / "BUILD.json").write_text(json.dumps(receipt))
    return path


def test_source_is_the_exact_historical_helper():
    source = builder.files("zasim").joinpath("data/bss_seed/ldn_bss_seed.c").read_bytes()
    assert hashlib.sha256(source).hexdigest() == seed.SOURCE_SHA256
    assert b"ieee80211_rx_irqsafe(hw, skb)" in source
    assert b"cfg80211_unlink_bss" in source


def test_validate_bundle_does_not_run_any_command(tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "_run", lambda *a, **k: pytest.fail("not read-only"))
    bundle = seed.validate_bundle(bundle_at(tmp_path / "bundle"))
    assert bundle.sha256 == hashlib.sha256(bundle.image).hexdigest()


@pytest.mark.parametrize("receipt", [{"schema": 2}, {"kernel_release": "other"},
    {"source_sha256": "0" * 64}, {"module_sha256": "0" * 64}])
def test_bad_receipt_rejected(tmp_path, receipt):
    with pytest.raises(ValueError):
        seed.validate_bundle(bundle_at(tmp_path / "bad", **receipt))


@pytest.mark.parametrize("image", [b"", b"not ELF", elf_image(name="foreign"),
    elf_image(release="other"), elf_image(license="Proprietary"), elf_image(machine=3),
    elf_image()[:80]])
def test_bad_image_rejected(tmp_path, image):
    path = bundle_at(tmp_path / "bad", image=image or b"bad")
    with pytest.raises(ValueError):
        seed.validate_bundle(path)


@pytest.mark.parametrize("raw", [b"[]", b"null", b"{", b"[" * 1500 + b"]" * 1500])
def test_invalid_json_rejected(tmp_path, raw):
    path = bundle_at(tmp_path / "bad")
    (path / "BUILD.json").write_bytes(raw)
    with pytest.raises(ValueError):
        seed.validate_bundle(path)


def test_final_and_ancestor_links_rejected(tmp_path):
    original = bundle_at(tmp_path / "original")
    link = tmp_path / "link"; link.symlink_to(original, target_is_directory=True)
    with pytest.raises(ValueError): seed.validate_bundle(link)
    (original / "BUILD.json").rename(original / "real.json")
    (original / "BUILD.json").symlink_to(original / "real.json")
    with pytest.raises(OSError): seed.validate_bundle(original)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO")
def test_fifo_and_size_are_bounded(tmp_path):
    fifo = tmp_path / "fifo"; os.mkfifo(fifo)
    with pytest.raises(ValueError): seed.regular_bytes(fifo, 100)
    big = tmp_path / "big"; big.write_bytes(b"x" * 101)
    with pytest.raises(ValueError): seed.regular_bytes(big, 100)


def test_exact_bss_cannot_mix_records():
    peer = "02:11:22:33:44:55"; ssid = "a" * 32
    assert seed.exact_bss(f"BSS {peer}(on fake)\n\tfreq: 2437\n\tSSID: {ssid}\n", peer, ssid, 2437)
    assert seed.exact_bss(f"BSS {peer}(on fake)\n\tfreq: 2437.0\n\tSSID: {ssid}\n", peer, ssid, 2437)
    assert not seed.exact_bss(f"BSS {peer}(on fake)\n freq: 2437\nBSS 02:00:00:00:00:01(on fake)\n SSID: {ssid}\n", peer, ssid, 2437)
    assert not seed.exact_bss(f"BSS {peer}(on fake)\n freq: 2412\n SSID: {ssid}\n", peer, ssid, 2437)


@pytest.fixture
def lease(tmp_path, monkeypatch):
    module = tmp_path / "sysmodule"
    monkeypatch.setattr(seed, "MODULE_SYSFS", module)
    monkeypatch.setattr(seed, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(seed, "NET_SYSFS", tmp_path / "net")
    device = tmp_path / "net/fake0/device/driver"
    device.mkdir(parents=True)
    (device / "module").symlink_to(tmp_path / "rtw88_8812au")
    monkeypatch.setattr(seed.socket, "if_nametoindex", lambda name: 7)
    monkeypatch.setattr(seed.os, "geteuid", lambda: 0)
    # Preserve actual fcntl locking but model root-owned /run metadata.
    real_fstat = os.fstat
    def fstat(fd):
        meta = real_fstat(fd)
        return SimpleNamespace(st_mode=meta.st_mode, st_uid=0, st_nlink=meta.st_nlink,
                               st_size=meta.st_size, st_dev=meta.st_dev, st_ino=meta.st_ino,
                               st_ctime_ns=meta.st_ctime_ns, st_mtime_ns=meta.st_mtime_ns)
    bundle = seed.validate_bundle(bundle_at(tmp_path / "bundle"))
    monkeypatch.setattr(seed.os, "fstat", fstat)
    monkeypatch.setattr(seed, "_tool", lambda name: name)
    owner = seed.SeedLease(bundle, log=lambda _: None)
    yield owner
    # Test-only disposal, never use it to claim runtime cleanup succeeded.
    if owner.fd is not None:
        os.close(owner.fd); owner.fd = None


def create_loaded(lease):
    (seed.MODULE_SYSFS / "parameters").mkdir(parents=True)
    for k, v in lease.parameters.items():
        (seed.MODULE_SYSFS / "parameters" / k).write_text(v)


def test_lease_is_exclusive_and_never_unlinks_lock(lease):
    lease.acquire("fake0")
    other = seed.SeedLease(lease.bundle)
    with pytest.raises(BlockingIOError): other.acquire("fake0")
    lease.close()
    assert seed.LOCK_PATH.exists()
    other.acquire("fake0"); other.close()


def test_reject_preexisting_module_without_unload(lease, monkeypatch):
    seed.MODULE_SYSFS.mkdir()
    monkeypatch.setattr(seed, "_run", lambda *a, **k: pytest.fail("foreign module touched"))
    with pytest.raises(RuntimeError, match="already loaded"): lease.acquire("fake0")
    assert not lease.active


def test_wrong_driver_rejected_before_lock(lease):
    p = seed.NET_SYSFS / "fake0/device/driver/module"
    p.unlink(); p.symlink_to(p.parent / "88xxau")
    with pytest.raises(ValueError, match="rtw88_8812au"): lease.acquire("fake0")
    assert not seed.LOCK_PATH.exists()


def test_insert_uses_sealed_verified_fd_and_cleanup(lease, monkeypatch):
    import fcntl
    calls = []
    def run(argv, **kwargs):
        calls.append(argv[0])
        if argv[0] == "insmod":
            fd, = kwargs["pass_fds"]
            assert os.pread(fd, len(lease.bundle.image), 0) == lease.bundle.image
            assert fcntl.fcntl(fd, fcntl.F_GET_SEALS) & fcntl.F_SEAL_WRITE
            create_loaded(lease)
        elif argv[0] == "rmmod":
            assert argv == ["rmmod", "ldn_bss_seed"]
            shutil.rmtree(seed.MODULE_SYSFS)
        return 0, ""
    monkeypatch.setattr(seed, "_run", run)
    lease.acquire("fake0"); lease.insert("02:11:22:33:44:55", "a" * 32, 6)
    with pytest.raises(RuntimeError, match="fresh"): lease.insert("02:11:22:33:44:55", "a" * 32, 6)
    lease.close(); lease.close()
    assert calls == ["insmod", "rmmod"] and not lease.active


def test_insert_timeout_after_load_still_owned(lease, monkeypatch):
    def run(argv, **kwargs):
        if argv[0] == "insmod":
            create_loaded(lease)
            raise TimeoutError("synthetic")
        shutil.rmtree(seed.MODULE_SYSFS)
        return 0, ""
    monkeypatch.setattr(seed, "_run", run)
    lease.acquire("fake0")
    with pytest.raises(TimeoutError): lease.insert("02:11:22:33:44:55", "a" * 32, 6)
    assert lease.owned_identity is not None
    lease.close(); assert not lease.active


def test_cleanup_failure_retains_lock_for_retry(lease, monkeypatch):
    def run(argv, **kwargs):
        if argv[0] == "insmod": create_loaded(lease); return 0, ""
        return 1, ""
    monkeypatch.setattr(seed, "_run", run)
    lease.acquire("fake0"); lease.insert("02:11:22:33:44:55", "a" * 32, 6)
    with pytest.raises(RuntimeError, match="cleanup failed"): lease.close()
    assert lease.active
    monkeypatch.setattr(seed, "_run", lambda *a, **kw: (shutil.rmtree(seed.MODULE_SYSFS) or 0, ""))
    lease.close(); assert not lease.active


def test_changed_owner_not_unloaded(lease, monkeypatch):
    def run(argv, **kwargs): create_loaded(lease); return 0, ""
    monkeypatch.setattr(seed, "_run", run)
    lease.acquire("fake0"); lease.insert("02:11:22:33:44:55", "a" * 32, 6)
    (seed.MODULE_SYSFS / "parameters/ssid").write_text("b" * 32)
    monkeypatch.setattr(seed, "_run", lambda *a, **k: pytest.fail("foreign module touched"))
    with pytest.raises(RuntimeError, match="ownership changed"): lease.close()
    assert lease.active


def test_replaced_device_cannot_load(lease, monkeypatch):
    lease.acquire("fake0")
    monkeypatch.setattr(seed.socket, "if_nametoindex", lambda _: 8)
    monkeypatch.setattr(seed, "_run", lambda *a, **k: pytest.fail("changed radio touched"))
    with pytest.raises(RuntimeError, match="identity changed"): lease.insert("02:11:22:33:44:55", "a" * 32, 6)
    lease.close()


def test_cli_and_connection_mapping():
    from zatrade import build_parser
    from zasim.gui_controller import ConnectionSettings
    p = build_parser()
    a = p.parse_args(["bss-helper", "build", "--output", "/some/new/bundle"])
    assert a.helper_action == "build"
    for name in ("trade", "handshake", "probe", "relay-serve"):
        assert "--bss-helper" in p._subparsers._group_actions[0].choices[name].format_help()
    ConnectionSettings(backend="direct", single_interface=True, bss_helper="/bundle").validate(os_name="posix")
    for kw in ({"backend": "relay", "single_interface": True}, {"backend": "direct", "single_interface": False}):
        with pytest.raises(ValueError): ConnectionSettings(bss_helper="/bundle", **kw).validate(os_name="posix")


def test_missing_or_mismatched_headers_never_build(tmp_path, monkeypatch):
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: pytest.fail("build started"))
    with pytest.raises(RuntimeError, match="headers"):
        builder.build_bundle(tmp_path / "out", kernel_build=tmp_path / "missing")
    headers = tmp_path / "headers"; (headers / "include/config").mkdir(parents=True)
    (headers / "include/config/kernel.release").write_text("wrong")
    (headers / ".config").write_text("CONFIG_CC_IS_CLANG=y\n")
    with pytest.raises(ValueError, match="do not match"):
        builder.build_bundle(tmp_path / "out", kernel_build=headers)
    assert not (tmp_path / "out").exists()


def test_existing_output_never_overwritten(tmp_path):
    output = tmp_path / "out"; output.mkdir(); keep = output / "keep"; keep.write_text("old")
    with pytest.raises(FileExistsError): builder.build_bundle(output)
    assert keep.read_text() == "old"


@pytest.mark.parametrize("outcome", ["normal", "abort", "duplicate", "insert_error", "foreign_then_normal", "tuple_arrays"])
def test_scan_handshake_order_and_terminal_gates(lease, monkeypatch, outcome):
    trio = pytest.importorskip("trio")
    events = []
    lease.acquire("fake0")
    monkeypatch.setattr(lease, "_cache_matches", lambda *a: bool(events and events[-1] == "terminal"))
    def insert(*args):
        events.append("insert")
        if outcome == "insert_error": raise RuntimeError("injected insertion failure")
    monkeypatch.setattr(lease, "insert", insert)
    nl = SimpleNamespace(**{name: i for i, name in enumerate((
        "NL80211_ATTR_IFINDEX", "NL80211_ATTR_SCAN_FREQUENCIES", "NL80211_ATTR_SCAN_SSIDS",
        "NL80211_ATTR_MEASUREMENT_DURATION", "NL80211_ATTR_MEASUREMENT_DURATION_MANDATORY",
        "NL80211_CMD_TRIGGER_SCAN", "NL80211_CMD_NEW_SCAN_RESULTS", "NL80211_CMD_SCAN_ABORTED"), 1)})
    async def scenario():
        send, receive = trio.open_memory_channel(10)
        class Backend:
            def add_membership(self, group): events.append("subscribe")
            async def request(self, cmd, attrs):
                events.append("request")
                assert events[:2] == ["subscribe", "request"]
                if outcome == "foreign_then_normal":
                    await send.send(SimpleNamespace(type=cmd, attributes={**attrs, nl.NL80211_ATTR_IFINDEX: 88}))
                if outcome == "tuple_arrays":
                    attrs = dict(attrs); attrs[nl.NL80211_ATTR_SCAN_FREQUENCIES] = tuple(attrs[nl.NL80211_ATTR_SCAN_FREQUENCIES]); attrs[nl.NL80211_ATTR_SCAN_SSIDS] = tuple(attrs[nl.NL80211_ATTR_SCAN_SSIDS])
                await send.send(SimpleNamespace(type=cmd, attributes=attrs))
                if outcome == "duplicate": await send.send(SimpleNamespace(type=cmd, attributes=attrs))
                terminal = nl.NL80211_CMD_SCAN_ABORTED if outcome == "abort" else nl.NL80211_CMD_NEW_SCAN_RESULTS
                await send.send(SimpleNamespace(type=terminal, attributes=attrs))
            async def receive(self):
                message = await receive.receive()
                if message.type in (nl.NL80211_CMD_NEW_SCAN_RESULTS, nl.NL80211_CMD_SCAN_ABORTED): events.append("terminal")
                return message
        @asynccontextmanager
        async def connect(): yield Backend()
        nl.connect = connect
        async def up(): pass
        await lease.prime(SimpleNamespace(ifname="fake0", _nl80211=nl), SimpleNamespace(up=up), "a" * 32, 6, bytes.fromhex("021122334455"))
    if outcome in ("abort", "duplicate", "insert_error"):
        with pytest.raises(BaseException): trio.run(scenario)
    else:
        trio.run(scenario)
        assert events == ["subscribe", "request", "insert", "terminal"]
    lease.close()


def test_native_bss_never_loads_or_starts_a_scan(lease, monkeypatch):
    trio = pytest.importorskip("trio")
    lease.acquire("fake0")
    monkeypatch.setattr(lease, "_cache_matches", lambda *a: True)
    monkeypatch.setattr(lease, "insert", lambda *a: pytest.fail("unnecessary load"))
    async def up(): pass
    async def scenario():
        await lease.prime(SimpleNamespace(ifname="fake0", _nl80211=object()), SimpleNamespace(up=up), "a" * 32, 6, bytes.fromhex("021122334455"))
    trio.run(scenario); lease.close()


def test_transport_option_requires_reuse_and_no_radio_on_construction(tmp_path, monkeypatch):
    from zasim.ldn.transport import LiveTransport
    bundle = bundle_at(tmp_path / "bundle")
    monkeypatch.setattr(seed.SeedLease, "acquire", lambda *a: pytest.fail("construction mutated host"))
    transport = LiveTransport(bss_helper=str(bundle), ifname="fake0")
    assert transport.single_interface is True and not transport._bss_seed.active
    with pytest.raises(ValueError, match="reuse"):
        LiveTransport(bss_helper=str(bundle), single_interface=False)


@pytest.mark.parametrize("scale,size", [(1.3333,"1080x820"),(2.6667,"640x480")])
def test_gui_picker_maps_bundle_and_physical_interface(monkeypatch, tmp_path, scale, size):
    tk = pytest.importorskip("tkinter")
    from zasim import gui
    from zasim.radio_picker import RadioInventory
    # No actual inventory or radio access from GUI construction.
    monkeypatch.setattr(gui, "read_inventory", lambda: RadioInventory("not_loaded"))
    root = tk.Tk(); root.tk.call("tk", "scaling", scale)
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    app = gui._DesktopApp(root, tk=tk, ttk=ttk, filedialog=filedialog, messagebox=messagebox, scrolledtext=scrolledtext, backend="direct", relay="127.0.0.1:24872", keys="", phyname="", ifname="ldnclient", nickname="TEST")
    try:
        root.geometry(size)
        app.connection_advanced_var.set(True); app._update_backend_fields()
        root.update_idletasks(); root.update()
        for widget in (app.bss_helper_entry, app.bss_helper_browse, app.bss_helper_clear):
            app.body.see(widget); root.update_idletasks(); root.update()
            c = app.body.canvas
            assert c.winfo_rootx() <= widget.winfo_rootx()
            assert widget.winfo_rootx()+widget.winfo_width() <= c.winfo_rootx()+c.winfo_width()
        # Exercise helper chooser, avoiding assumptions about sysfs fixture layout.
        bundle = bundle_at(tmp_path / "bundle")
        monkeypatch.setattr(app.filedialog, "askdirectory", lambda **k: str(bundle))
        app._browse_bss_helper()
        assert app.bss_helper_var.get() == str(bundle)
        assert app.single_interface_var.get() == gui.tr("はい")
        app.change_language("en")
        assert app.bss_helper_var.get() == str(bundle)
        app.close_when_idle = True
        app._clear_bss_helper()
        assert app.bss_helper_var.get() == str(bundle)
        app.close_when_idle = False
        app.bss_helper_clear.invoke()
        assert not app.bss_helper_var.get()
        assert app.single_interface_var.get() == gui.tr("自動")
    finally:
        root.destroy()


def test_kmod_applet_name_survives_symlink_resolution(monkeypatch):
    # Resolving /usr/bin/insmod to kmod would lose argv[0] dispatch. --version
    # is read-only; it proves the real OS tool path without loading anything.
    import subprocess
    original_stat = Path.stat
    def root_mapped_stat(path, *a, **kw):
        meta = original_stat(path, *a, **kw)
        if path.name == "kmod":
            # In an unprivileged bwrap user namespace host-root maps to
            # overflowuid; the real privileged application sees uid 0.
            return SimpleNamespace(st_mode=meta.st_mode, st_uid=0)
        return meta
    monkeypatch.setattr(Path, "stat", root_mapped_stat)
    for name in ("insmod", "rmmod"):
        try:
            tool = seed._tool(name)
        except RuntimeError:
            pytest.skip("OS kmod tools not available")
        assert Path(tool).name == name
        r = subprocess.run([tool, "--version"], capture_output=True, timeout=2)
        assert r.returncode == 0 and b"kmod" in r.stdout.lower()


def test_boolean_schema_is_not_an_integer_version(tmp_path):
    with pytest.raises(ValueError): seed.validate_bundle(bundle_at(tmp_path / "bad", schema=True))


def test_transport_does_not_release_helper_while_worker_alive(monkeypatch):
    from zasim.ldn.transport import LiveTransport
    transport = LiveTransport()
    joins = []
    transport._bss_seed = SimpleNamespace(close=lambda: pytest.fail("raced active worker"))
    transport._thread = SimpleNamespace(ident=123, join=lambda timeout: joins.append(timeout), is_alive=lambda: True)
    with pytest.raises(RuntimeError, match="did not stop"): transport.stop()
    assert joins == [8]


def test_transport_helper_failure_retains_radio_restore_state(monkeypatch):
    from zasim.ldn import transport as module
    transport = module.LiveTransport()
    transport._radio_restore_state = {"synthetic": {"up": False}}
    events = []
    def close(): events.append("helper-close"); raise RuntimeError("helper cleanup failed")
    def restore(*, prior_errors):
        events.append("restore")
        assert len(prior_errors) == 1
        raise prior_errors[0]
    transport._bss_seed = SimpleNamespace(close=close)
    monkeypatch.setattr(transport, "_restore_host_neighbor", lambda: None)
    monkeypatch.setattr(module, "light_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(transport, "_restore_radio_state", restore)
    with pytest.raises(RuntimeError, match="helper cleanup failed"): transport._teardown_radio()
    assert events == ["helper-close", "restore"] and transport._radio_restore_state


def test_transport_early_cancel_releases_helper_without_radio_mutation(monkeypatch):
    from zasim.ldn import transport as module
    import threading
    transport = module.LiveTransport()
    events = []
    transport._bss_seed = SimpleNamespace(acquire=lambda name: events.append("acquire"), close=lambda: events.append("close"))
    monkeypatch.setattr(transport, "_prepare_radio_path", lambda: None)
    monkeypatch.setattr(module, "free_radio", lambda *a, **k: pytest.fail("cancelled radio mutation"))
    cancelled = threading.Event(); cancelled.set()
    transport.start(cancel_event=cancelled)
    assert events == ["acquire", "close"] and transport._radio_teardown_complete


@pytest.mark.parametrize("helper_failure", [False, True])
def test_existing_interface_calls_helper_before_connect_and_always_cleans_up(helper_failure):
    trio = pytest.importorskip("trio")
    from zasim.ldn.ldn_single import ExistingInterfaceFactory
    events = []
    nl = SimpleNamespace(NL80211_ATTR_IFINDEX=1, NL80211_ATTR_MAC=2, NL80211_IFTYPE_STATION=3)
    class Station:
        def __init__(self, *args): pass
        @asynccontextmanager
        async def connect(self):
            events.append("connect")
            try: yield self
            finally: events.append("disconnect")
    wlan = SimpleNamespace(nl80211=nl, MACAddress=bytes, Station=Station)
    factory = ExistingInterfaceFactory(SimpleNamespace(trio=trio, wlan=wlan), SimpleNamespace(_wlan=object(), _router=object()), "fake0", log=lambda _: None)
    async def noop(*a, **k): pass
    async def attributes(*a): return {1: 7, 2: bytes.fromhex("020000000002")}
    async def prime(f, station, ssid, channel, bssid):
        events.append("helper")
        assert f is factory and ssid == "a" * 32 and channel == 6 and bssid == bytes.fromhex("021122334455")
        if helper_failure: raise RuntimeError("helper failed")
    async def down(*a): events.append("down")
    factory._attributes = attributes
    factory._require_ldn_control_port = factory._set_type = factory._close_socket_for_cleanup = noop
    factory._down_for_cleanup = down
    factory.bss_seed = SimpleNamespace(prime=prime)
    factory._prime_station_bss = lambda *a: pytest.fail("ran ordinary primer as well")
    async def run():
        async with factory.connect_network("fakephy", "fake0", "a" * 32, 6, b"key", bytes.fromhex("021122334455")):
            events.append("entered")
    if helper_failure:
        with pytest.raises(RuntimeError, match="helper failed"): trio.run(run)
        assert events == ["helper", "down"]
    else:
        trio.run(run)
        assert events == ["helper", "connect", "entered", "disconnect", "down"]


def test_no_exact_scan_start_times_out_without_insertion(lease, monkeypatch):
    trio = pytest.importorskip("trio")
    from trio.testing import MockClock
    lease.acquire("fake0")
    monkeypatch.setattr(lease, "_cache_matches", lambda *a: False)
    monkeypatch.setattr(lease, "insert", lambda *a: pytest.fail("inserted without exact start"))
    async def noop(*a): pass
    async def receive(): await trio.sleep_forever()
    @asynccontextmanager
    async def connect():
        yield SimpleNamespace(add_membership=lambda _: None, request=noop, receive=receive)
    nl = SimpleNamespace(connect=connect, **{name: i for i, name in enumerate((
        "NL80211_ATTR_IFINDEX", "NL80211_ATTR_SCAN_FREQUENCIES", "NL80211_ATTR_SCAN_SSIDS",
        "NL80211_ATTR_MEASUREMENT_DURATION", "NL80211_ATTR_MEASUREMENT_DURATION_MANDATORY", "NL80211_CMD_TRIGGER_SCAN"))})
    async def run():
        await lease.prime(SimpleNamespace(ifname="fake0", _nl80211=nl), SimpleNamespace(up=noop), "a" * 32, 6, bytes.fromhex("021122334455"))
    with pytest.raises(trio.TooSlowError): trio.run(run, clock=MockClock(autojump_threshold=0))
    lease.close()


def test_public_entry_points_reject_relay_client_helper_before_connection():
    from zasim.live import run_passive_probe
    from zasim.handshake import run_handshake_diagnostic
    from zasim.relay import ZARelayServer
    with pytest.raises(ValueError, match="local Linux"):
        run_passive_probe(output="/tmp/helper-test-no-write.jsonl", relay="127.0.0.1:24872", bss_helper="/bundle", link_code="12345678")
    with pytest.raises(ValueError, match="local Linux"):
        run_handshake_diagnostic(output="/tmp/helper-test-no-write.json", relay="127.0.0.1:24872", bss_helper="/bundle", link_code="12345678")
    with pytest.raises(ValueError, match="injected"):
        ZARelayServer(keys_path=None, bss_helper="/bundle", transport_factory=lambda _: None)
