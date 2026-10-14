"""Opt-in, per-session ownership of the experimental rtw88 BSS helper.

Nothing loads on import or bundle validation. The caller must own a dedicated
single-interface radio, acquire this global module lease before touching it,
and close the lease AFTER the connection worker has stopped. No driver swap,
autoload, forced unload, privilege escalation, or authentication bypass.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile

SOURCE_SHA256 = "ba5df6f016107f5cec356680919f3a8b759cb5ae179b34c48655a064ba786d9a"
MODULE_NAME = "ldn_bss_seed"
MAX_MODULE_BYTES = 16 * 1024 * 1024
MODULE_SYSFS = Path("/sys/module") / MODULE_NAME
LOCK_PATH = Path("/run/za-trade-ldn-bss-seed.lock")
NET_SYSFS = Path("/sys/class/net")


def regular_bytes(path: Path, limit: int) -> bytes:
    """Bound reads and reject special files, final symlinks and concurrent edits."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("BSS helper input must be a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(data) > limit or any(getattr(before, f) != getattr(after, f) for f in fields):
            raise ValueError("BSS helper input changed during inspection")
        return data
    finally:
        os.close(fd)


def module_info(data: bytes) -> dict[str, str]:
    """Read ELF .modinfo without transforming the module or executing a tool."""
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("BSS helper must be a little-endian ELF64 kernel module")
    kind, machine = struct.unpack_from("<HH", data, 16)
    expected = {"x86_64": 62, "aarch64": 183}.get(platform.machine())
    if kind != 1 or expected is None or machine != expected:
        raise ValueError("BSS helper ELF architecture/type does not match this PC")
    offset = struct.unpack_from("<Q", data, 40)[0]
    size, count, strings = struct.unpack_from("<HHH", data, 58)
    if size != 64 or not 1 <= count <= 4096 or strings >= count or offset + count * size > len(data):
        raise ValueError("invalid BSS helper ELF section table")
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, offset + i * size) for i in range(count)]
    def content(section):
        start, length = section[4:6]
        if start + length > len(data):
            raise ValueError("invalid BSS helper ELF section bounds")
        return data[start:start + length]
    names = content(sections[strings])
    matches = []
    for section in sections:
        start = section[0]
        end = names.find(b"\0", start)
        if start >= len(names) or end < 0:
            raise ValueError("invalid BSS helper ELF section name")
        if names[start:end] == b".modinfo":
            matches.append(content(section))
    if len(matches) != 1:
        raise ValueError("BSS helper has no unique module metadata")
    info = {}
    for item in matches[0].split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and key in (b"name", b"vermagic", b"license"):
            label = key.decode("ascii")
            if label in info:
                raise ValueError("duplicate BSS helper module identity")
            info[label] = value.decode("ascii")
    return info


@dataclass(frozen=True)
class Bundle:
    directory: Path
    image: bytes
    kernel_release: str
    sha256: str


def validate_bundle(directory: str | Path) -> Bundle:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("BSS helper is Linux-only; configure it on the Linux relay, not Windows")
    path = Path(directory).expanduser().absolute()
    # Do not turn a symlink selection into an apparently regular trusted bundle.
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError("BSS helper bundle path must not contain symlinks")
    raw = regular_bytes(path / "BUILD.json", 4096)
    try:
        receipt = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid BSS helper build receipt") from exc
    release = platform.release()
    if not isinstance(receipt, dict) or type(receipt.get("schema")) is not int or receipt.get("schema") != 1:
        raise ValueError("invalid BSS helper build receipt schema")
    if receipt.get("kernel_release") != release:
        raise ValueError("BSS helper kernel mismatch; rebuild it for the running kernel")
    if receipt.get("source_sha256") != SOURCE_SHA256:
        raise ValueError("BSS helper source version mismatch; rebuild with this application")
    image = regular_bytes(path / (MODULE_NAME + ".ko"), MAX_MODULE_BYTES)
    digest = hashlib.sha256(image).hexdigest()
    if digest != receipt.get("module_sha256"):
        raise ValueError("BSS helper module checksum mismatch; rebuild the bundle")
    info = module_info(image)
    if (info.get("name") != MODULE_NAME or info.get("license") != "GPL"
            or not info.get("vermagic", "").startswith(release + " ")):
        raise ValueError("BSS helper module identity/kernel mismatch")
    return Bundle(path, image, release, digest)


def _tool(name: str) -> str:
    # Never select a user PATH executable while loading a kernel module.
    for base in ("/usr/bin", "/usr/sbin", "/bin", "/sbin"):
        candidate = Path(base) / name
        path = candidate.resolve()
        try:
            meta = path.stat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(meta.st_mode) and meta.st_uid == 0 and not meta.st_mode & 0o022 and os.access(path, os.X_OK):
            return str(candidate)  # keep argv[0]: insmod/rmmod are kmod applets
    raise RuntimeError(f"BSS helper requires the OS-provided {name} executable")


def _run(argv, *, timeout=2, pass_fds=()):
    # No peer-bearing argv or raw stderr in application logs/errors.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                argv, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.DEVNULL, timeout=timeout, pass_fds=pass_fds,
                check=False, env={"PATH": "/usr/bin:/usr/sbin", "LC_ALL": "C"},
            )
        except subprocess.TimeoutExpired:
            # TimeoutExpired.__str__ includes argv (room SSID/BSSID). Suppress
            # its exception chain too: the transport formats full tracebacks.
            raise RuntimeError("BSS helper command timed out; cleanup is still required") from None
        except OSError:
            raise RuntimeError("BSS helper command could not start") from None
        output.seek(0)
        data = output.read(262145)
    if len(data) > 262144:
        raise RuntimeError("BSS helper command output exceeded its bound")
    return result.returncode, data.decode("utf-8", "replace")


def exact_bss(text: str, bssid: str, ssid: str, frequency: int) -> bool:
    """BSSID, SSID and frequency must belong to the same iw BSS record."""
    records = re.split(r"(?m)^BSS ", text)
    for record in records[1:]:
        rows = record.splitlines()
        if not rows or rows[0].split("(", 1)[0].strip().lower() != bssid:
            continue
        fields = {r.strip() for r in rows[1:]}
        if f"SSID: {ssid}" in fields and ({f"freq: {frequency}", f"freq: {frequency}.0"} & fields):
            return True
    return False


class SeedLease:
    def __init__(self, bundle: Bundle, *, log=print):
        self.bundle = bundle
        self.log = log
        self.fd = None
        self.ifname = None
        self.ifindex = None
        self.device_identity = None
        self.owned_identity = None
        self.parameters = None
        self.insert_attempted = False
        self.uncertain = False

    @property
    def active(self):
        return self.fd is not None

    def acquire(self, ifname: str):
        import fcntl
        if self.active:
            raise RuntimeError("previous BSS helper lease still needs cleanup")
        if os.geteuid() != 0:
            raise PermissionError("using the BSS helper requires Linux administrator privileges")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", ifname) is None:
            raise ValueError("invalid BSS helper interface")
        device = NET_SYSFS / ifname
        driver = (device / "device/driver/module").resolve().name
        if driver != "rtw88_8812au":
            raise ValueError("BSS helper supports only AWUS036ACH / rtw88_8812au, not legacy 88XXau or ACHM")
        index = socket.if_nametoindex(ifname)
        identity = device.stat()
        fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_nlink != 1 or meta.st_mode & 0o077:
                raise RuntimeError("unsafe BSS helper ownership lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if MODULE_SYSFS.exists():
                raise RuntimeError("BSS helper already loaded; refusing to borrow or unload another owner's module")
            _tool("insmod"); _tool("rmmod"); _tool("iw")
        except BaseException:
            os.close(fd)
            raise
        self.fd, self.ifname, self.ifindex = fd, ifname, index
        self.device_identity = (identity.st_dev, identity.st_ino)
        self.insert_attempted = False

    def _check_device(self):
        device = NET_SYSFS / self.ifname
        meta = device.stat()
        if ((meta.st_dev, meta.st_ino) != self.device_identity
                or socket.if_nametoindex(self.ifname) != self.ifindex
                or (device / "device/driver/module").resolve().name != "rtw88_8812au"):
            raise RuntimeError("BSS helper radio identity changed; refusing insertion")

    def _module_identity(self):
        meta = MODULE_SYSFS.stat()
        return meta.st_dev, meta.st_ino, meta.st_ctime_ns

    def _parameters_match(self):
        return self.parameters is not None and all(
            (MODULE_SYSFS / "parameters" / key).read_text(encoding="ascii").strip() == value
            for key, value in self.parameters.items())

    def insert(self, bssid: str, ssid: str, channel: int):
        import fcntl
        if not self.active or self.insert_attempted:
            raise RuntimeError("BSS helper insertion requires a fresh exclusive lease")
        self._check_device()
        if MODULE_SYSFS.exists():
            raise RuntimeError("BSS helper appeared before insertion; refusing foreign module")
        self.insert_attempted = True
        self.parameters = {"ifname": self.ifname, "bssid": bssid, "ssid": ssid, "channel": str(channel)}
        fd = os.memfd_create("za-bss-seed", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(self.bundle.image)
                stream.flush()
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                code, _ = _run([_tool("insmod"), f"/proc/self/fd/{fd}",
                               *(f"{k}={v}" for k, v in self.parameters.items())], pass_fds=(fd,))
            finally:
                # Handles interruption/timeout after finit_module succeeded.
                if MODULE_SYSFS.exists():
                    self.uncertain = True
                    if self._parameters_match():
                        self.owned_identity = self._module_identity()
                        self.uncertain = False
            if code or self.owned_identity is None:
                raise RuntimeError("BSS helper load failed; check matching headers, dependencies and module-signing policy (no automatic fallback)")
            self.log("[helper] owned temporary BSS module loaded")
        finally:
            os.close(fd)

    def close(self):
        if not self.active:
            return
        if self.uncertain:
            raise RuntimeError("BSS helper ownership is uncertain; lease retained, no forced unload")
        if self.owned_identity is not None:
            if MODULE_SYSFS.exists():
                if self._module_identity() != self.owned_identity or not self._parameters_match():
                    raise RuntimeError("BSS helper ownership changed; refusing to unload, lease retained")
                code, _ = _run([_tool("rmmod"), MODULE_NAME])
                if code or MODULE_SYSFS.exists():
                    raise RuntimeError("BSS helper cleanup failed; lease retained; stop again after resolving module use")
            self.owned_identity = None
            self.log("[helper] owned temporary BSS module removed")
        os.close(self.fd)
        self.fd = None

    def _cache_matches(self, bssid, ssid, frequency):
        code, text = _run([_tool("iw"), "dev", self.ifname, "scan", "dump"])
        if code:
            raise RuntimeError("BSS helper could not inspect the selected interface BSS cache")
        return exact_bss(text, bssid, ssid, frequency)

    async def prime(self, factory, station, ssid, channel, bssid):
        """Seed only after an exact kernel scan-start event, then verify cache."""
        import trio
        if (not self.active or factory.ifname != self.ifname
                or re.fullmatch(r"[0-9a-f]{32}", ssid) is None
                or type(channel) is not int or channel not in (1, 6, 11)
                or not isinstance(bssid, bytes) or len(bssid) != 6
                or bssid == bytes(6) or bssid[0] & 1):
            raise ValueError("BSS helper requires the exact selected LDN identity on channel 1, 6 or 11")
        self._check_device()
        peer = ":".join(f"{v:02x}" for v in bssid)
        frequency = 2407 + 5 * channel
        nl = factory._nl80211
        await station.up()
        if await trio.to_thread.run_sync(self._cache_matches, peer, ssid, frequency):
            self.log("[helper] exact native BSS already available; no module loaded")
            return
        send, receive = trio.open_memory_channel(1)
        async with nl.connect() as backend:
            backend.add_membership("scan")
            async def observe(*, task_status=trio.TASK_STATUS_IGNORED):
                task_status.started()
                started = False
                while True:
                    message = await backend.receive()
                    a = getattr(message, "attributes", None)
                    if not isinstance(a, dict):
                        continue
                    frequencies = a.get(nl.NL80211_ATTR_SCAN_FREQUENCIES)
                    ssids = a.get(nl.NL80211_ATTR_SCAN_SSIDS)
                    if (a.get(nl.NL80211_ATTR_IFINDEX) != self.ifindex
                            or not isinstance(frequencies, (list, tuple)) or tuple(frequencies) != (frequency,)
                            or not isinstance(ssids, (list, tuple)) or tuple(ssids) != (ssid.encode("ascii"),)):
                        continue
                    if message.type == nl.NL80211_CMD_TRIGGER_SCAN:
                        if started:
                            raise RuntimeError("duplicate BSS helper scan-start event")
                        started = True
                        await trio.to_thread.run_sync(self.insert, peer, ssid, channel)
                    elif started and message.type in (nl.NL80211_CMD_NEW_SCAN_RESULTS, nl.NL80211_CMD_SCAN_ABORTED):
                        await send.send(message.type == nl.NL80211_CMD_NEW_SCAN_RESULTS)
                        return
            with trio.fail_after(8):
                async with trio.open_nursery() as nursery:
                    await nursery.start(observe)
                    try:
                        await backend.request(nl.NL80211_CMD_TRIGGER_SCAN, {
                            nl.NL80211_ATTR_IFINDEX: self.ifindex,
                            nl.NL80211_ATTR_SCAN_FREQUENCIES: [frequency],
                            nl.NL80211_ATTR_SCAN_SSIDS: [ssid.encode("ascii")],
                            nl.NL80211_ATTR_MEASUREMENT_DURATION: 3000,
                            nl.NL80211_ATTR_MEASUREMENT_DURATION_MANDATORY: True,
                        })
                        if not await receive.receive():
                            raise RuntimeError("BSS helper scan aborted; no connection attempted")
                    finally:
                        nursery.cancel_scope.cancel()
        if not await trio.to_thread.run_sync(self._cache_matches, peer, ssid, frequency):
            raise RuntimeError("BSS helper did not publish the exact BSSID/SSID/channel")
        self.log("[helper] exact BSS verified; normal LDN authentication follows")
