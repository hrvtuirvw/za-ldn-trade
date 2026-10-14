"""LDN compatibility helpers for radios that cannot create virtual interfaces.

``ldn`` normally creates a short-lived monitor netdev for scanning and a new
station netdev for joining.  Some out-of-tree USB drivers (notably 88XXau) only
work reliably with their one existing netdev.  This module mirrors the small
public ``ldn.scan``/``ldn.connect`` wrappers while asking nl80211 to change the
type of that existing interface instead of issuing NEW_INTERFACE.

The caller owns NetworkManager/admin-state cleanup and final restoration.  The
helpers here never rename or delete the supplied interface.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import copy
import errno
import fcntl
import os
import secrets
import socket
import struct
import subprocess
import time
from typing import Any, AsyncIterator, Awaitable, Callable


# These out-of-tree Realtek drivers implement managed/monitor mode, but do not
# implement the nl80211 custom control-port API that LDN uses for its 0x88B7
# authentication frames.  They can therefore scan an LDN room but cannot join
# it.  In particular, dropping the unsupported CONNECT attributes is not a
# fallback: doing so would also remove the path that carries authentication.
LEGACY_CONTROL_PORT_INCOMPATIBLE_DRIVERS = frozenset({
    "88xxau",
    "8812au",
    "rtl8812au",
    "rtl88xxau",
})
CONTROL_PORT_OVER_NL80211_EXT_FEATURE_BIT = 26
_IW_CONTROL_PORT_OVER_NL80211 = "[ CONTROL_PORT_OVER_NL80211 ]"
# Upstream ldn 0.0.17 waits one second for the post-authentication host
# advertisement that assigns the station's 169.254 address.  Real Switch
# captures on the single-interface rtw88 path contain multi-second gaps even
# though the host normally advertises much more often.  Five seconds remains a
# small, bounded passive wait and does not send application or trade traffic.
_PARTICIPANT_ASSIGNMENT_TIMEOUT_SECONDS = 5.0
# ``netlink`` 0.0.10 leaves nl80211 requests unbounded.  A Switch may
# disassociate the station just before local shutdown, in which case the
# driver's acknowledgement for a redundant DISCONNECT can never arrive.
# Keep that best-effort request well inside LiveTransport's two-second worker
# join budget; the mandatory administrative DOWN below remains authoritative.
_STATION_DISCONNECT_TIMEOUT_SECONDS = 0.5
_STATION_DOWN_TIMEOUT_SECONDS = 0.75
_INTERFACE_SOCKET_CLOSE_TIMEOUT_SECONDS = 0.25
_INTERFACE_TYPE_RESTORE_TIMEOUT_SECONDS = 1.0
_MONITOR_READBACK_TIMEOUT_SECONDS = 0.1
_SIOCGIFFLAGS = 0x8913


def _interface_was_up(ifname: str) -> bool:
    """Read just the administrative UP bit before a temporary mode change."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            request = struct.pack("16sH", ifname.encode()[:15], 0)
            response = fcntl.ioctl(control.fileno(), _SIOCGIFFLAGS, request)
        _name, flags = struct.unpack("16sH", response[:18])
    except OSError:
        # Never invent an UP transition if inspection itself is unavailable.
        return False
    return bool(flags & 0x1)  # IFF_UP


async def _monitor_channel_readback(ldn_module, monitor, channel, *, log=print):
    """One bounded post-ACK kernel snapshot, NOT independent RF tuning proof.

    A packetless window has no Radiotap frequency evidence. GET_INTERFACE can
    distinguish a reported channel mismatch from absent packets, but drivers
    may return cached configuration or omit frequency on a monitor interface.
    This opt-in diagnostic never retunes, rejects a match, or shields itself
    against the scan's cancellation. No peer data or exception text is logged.
    """
    trio_module = _required(ldn_module, "trio")
    wlan = _required(ldn_module, "wlan")
    nl = getattr(wlan, "nl80211", None)
    backend = getattr(monitor, "_wlan", None)
    expected = getattr(wlan, "Channels", {}).get(channel)
    started = time.monotonic()
    status, actual, mode = "unsupported", None, "unknown"
    try:
        if nl is not None and backend is not None and callable(getattr(monitor, "index", None)):
            index = monitor.index()
            status = "timeout"
            with trio_module.move_on_after(_MONITOR_READBACK_TIMEOUT_SECONDS):
                messages = await backend.request(
                    nl.NL80211_CMD_GET_INTERFACE,
                    {nl.NL80211_ATTR_IFINDEX: index},
                )
                status = "unknown"
                # Never attribute an unrelated or ambiguous reply to this VIF.
                rows = [m.attributes for m in messages
                        if m.attributes.get(nl.NL80211_ATTR_IFINDEX) == index]
                if len(rows) == 1:
                    attributes = rows[0]
                    frequency = attributes.get(nl.NL80211_ATTR_WIPHY_FREQ)
                    if type(frequency) is int and 0 < frequency <= 65535:
                        actual = frequency
                    iftype = attributes.get(nl.NL80211_ATTR_IFTYPE)
                    if type(iftype) is int:
                        mode = "yes" if iftype == nl.NL80211_IFTYPE_MONITOR else "no"
                    if mode == "no" or (actual is not None and expected is not None and actual != expected):
                        status = "mismatch"
                    elif mode == "yes" and actual is not None and expected is not None:
                        status = "match"
    except Exception:
        status = "error"
    except BaseException:
        status = "interrupted"
        raise
    finally:
        log(
            "[diag] LDN channel readback phase=post-ack "
            f"requested_channel={channel} status={status} "
            f"expected_frequency={expected if expected is not None else 'unknown'} "
            f"kernel_frequency={actual if actual is not None else 'unknown'} "
            f"kernel_monitor={mode} "
            f"elapsed_s={max(0.0, time.monotonic() - started):.3f}"
        )
    return status


def _connect_event_diagnostic(nl: object, attributes: dict) -> str:
    """Summarize kernel outcome without disclosing peer, SSID, IEs or keys.

    STATUS_CODE=1 alone is ambiguous: mac80211 also synthesizes it on timeout.
    Keep the kernel timeout flag/reason rather than calling this a peer reject.
    Missing attributes are unknown, not evidence of a negative result.
    """
    def value(name: str):
        key = getattr(nl, name, None)
        return attributes.get(key) if key is not None else None

    status = value("NL80211_ATTR_STATUS_CODE")
    status_text = str(status) if type(status) is int and 0 <= status <= 65535 else "unknown"
    timed_out = value("NL80211_ATTR_TIMED_OUT")
    timeout_text = "yes" if timed_out is True else "no" if timed_out is False else "unknown"
    reason = value("NL80211_ATTR_TIMEOUT_REASON")
    reason_text = (
        {0: "unspecified", 1: "scan", 2: "auth", 3: "assoc"}.get(reason, "unknown")
        if type(reason) is int else "unknown"
    )
    return (
        f"[live] nl80211 CONNECT outcome status={status_text} "
        f"timed_out={timeout_text} timeout_reason={reason_text}"
    )


class UnsupportedLDNControlPortDriverError(RuntimeError):
    """The selected WLAN driver cannot carry LDN authentication."""


class NoMonitorFramesError(ConnectionError):
    """A complete monitor sweep received no raw 802.11 frame at all."""


class IncompleteScanError(ConnectionError):
    """A targeted miss with skipped RX windows, not evidence of radio silence."""


def _iw_has_control_port_over_nl80211(phyname: str) -> bool | None:
    """Read one Wiphy's extended features with ``iw`` without a shell.

    ldn 0.0.17's netlink decoder cannot parse every attribute returned by a
    modern split Wiphy dump.  A non-split GET_WIPHY remains useful for the
    custom-EtherType flag, but some kernels omit EXT_FEATURES from that reply.
    In that specific case, use iw's current nl80211 decoder and accept only the
    exact feature token it prints.  ``None`` means the inspection itself
    failed; callers must fail closed.
    """

    try:
        result = subprocess.run(
            ["iw", "phy", phyname, "info"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for raw_line in result.stdout.splitlines():
        feature = raw_line.strip()
        if feature.startswith("*"):
            feature = feature[1:].strip()
        # Newer iw versions append details after a colon.  Compare the
        # bracketed feature name itself exactly so similarly named features or
        # explanatory text elsewhere cannot produce a false positive.
        feature_name = feature.partition(":")[0].rstrip()
        if feature_name == _IW_CONTROL_PORT_OVER_NL80211:
            return True
    return False


def _raw_ext_feature_state(values: list[Any], bit: int) -> bool | None:
    """Return a raw nl80211 feature bit, or None when it was not decodable."""

    saw_decodable_value = False
    for value in values:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            continue
        saw_decodable_value = True
        if len(value) > bit // 8 and value[bit // 8] & (1 << (bit % 8)):
            return True
    return False if saw_decodable_value else None


def _interface_driver(ifname: str) -> str | None:
    module_link = f"/sys/class/net/{ifname}/device/driver/module"
    try:
        name = os.path.basename(os.path.realpath(module_link)).strip().lower()
        if name and name != "module":
            return name
    except OSError:
        pass
    try:
        with open(
            f"/sys/class/net/{ifname}/device/uevent", errors="replace"
        ) as source:
            for line in source:
                if line.startswith("DRIVER="):
                    return line.partition("=")[2].strip().lower() or None
    except OSError:
        pass
    return None


def _required(module: object, name: str) -> Any:
    value = getattr(module, name, None)
    if value is None:
        raise RuntimeError(
            f"ldn single-interface compatibility requires ldn.{name}; "
            "install the pinned ldn==0.0.17 dependency"
        )
    return value


async def _close_socket(interface: object) -> None:
    """Close an ldn Interface's unused/raw socket before changing its type."""

    sock = getattr(interface, "_socket", None)
    if sock is None:
        return
    close = getattr(sock, "aclose", None)
    if close is not None:
        await close()
        return
    close = getattr(sock, "close", None)
    if close is not None:
        close()


class ExistingInterfaceFactory:
    """Subset of ``ldn.wlan.Factory`` that reuses one physical netdev."""

    def __init__(
        self,
        ldn_module: object,
        factory: object,
        ifname: str,
        *,
        log=print,
    ) -> None:
        self.ldn = ldn_module
        self.wlan_module = _required(ldn_module, "wlan")
        self.factory = factory
        self.ifname = ifname
        self.log = log
        try:
            self._wlan = factory._wlan
            self._router = factory._router
        except AttributeError as exc:
            raise RuntimeError(
                "ldn single-interface compatibility is incompatible with this "
                "ldn Factory; install ldn==0.0.17"
            ) from exc

    @property
    def _nl80211(self) -> object:
        return _required(self.wlan_module, "nl80211")

    async def _attributes(self, phyname: str) -> dict[int, Any]:
        nl = self._nl80211
        try:
            index = socket.if_nametoindex(self.ifname)
        except OSError as exc:
            raise RuntimeError(
                f"single-interface netdev does not exist: {self.ifname}"
            ) from exc
        messages = await self._wlan.request(
            nl.NL80211_CMD_GET_INTERFACE,
            {nl.NL80211_ATTR_IFINDEX: index},
        )
        if not messages:
            raise RuntimeError(
                f"nl80211 returned no information for interface {self.ifname}"
            )
        attributes = messages[0].attributes
        expected_wiphy = await self.factory._get_wiphy_index(phyname)
        actual_wiphy = attributes.get(nl.NL80211_ATTR_WIPHY)
        if actual_wiphy is not None and actual_wiphy != expected_wiphy:
            raise RuntimeError(
                f"interface {self.ifname} is not attached to {phyname}"
            )
        return attributes

    async def _require_ldn_control_port(self, phyname: str) -> None:
        """Fail closed unless the Wiphy advertises both LDN requirements."""

        nl = self._nl80211
        expected_wiphy = await self.factory._get_wiphy_index(phyname)
        messages = await self._wlan.request(
            nl.NL80211_CMD_GET_WIPHY,
            {nl.NL80211_ATTR_WIPHY: expected_wiphy},
        )
        matching_attributes = []
        for message in messages:
            attributes = message.attributes
            actual_wiphy = attributes.get(nl.NL80211_ATTR_WIPHY)
            if actual_wiphy in (None, expected_wiphy):
                matching_attributes.append(attributes)
        if not matching_attributes:
            raise RuntimeError(
                f"cannot inspect nl80211 control-port capabilities for "
                f"{phyname}; refusing to join"
            )

        has_custom_ethertype = any(
            nl.NL80211_ATTR_CONTROL_PORT_ETHERTYPE in attributes
            for attributes in matching_attributes
        )
        bit = CONTROL_PORT_OVER_NL80211_EXT_FEATURE_BIT
        raw_feature_state = _raw_ext_feature_state(
            [
                attributes[nl.NL80211_ATTR_EXT_FEATURES]
                for attributes in matching_attributes
                if nl.NL80211_ATTR_EXT_FEATURES in attributes
            ],
            bit,
        )
        has_over_nl80211 = (
            raw_feature_state
            if raw_feature_state is not None
            else bool(_iw_has_control_port_over_nl80211(phyname))
        )
        if has_custom_ethertype and has_over_nl80211:
            return

        missing = []
        if not has_custom_ethertype:
            missing.append("custom control-port EtherType 0x88B7")
        if not has_over_nl80211:
            missing.append(
                "NL80211_ATTR_CONTROL_PORT_OVER_NL80211 "
                "(EXT_FEATURES bit 26)"
            )
        driver = _interface_driver(self.ifname) or "unknown driver"
        legacy_hint = (
            " This is the known AWUS036ACH legacy 88XXau/aircrack-ng "
            "limitation."
            if driver in LEGACY_CONTROL_PORT_INCOMPATIBLE_DRIVERS
            else ""
        )
        raise UnsupportedLDNControlPortDriverError(
            f"LDN room was found, but {driver} on {self.ifname} cannot join "
            f"it because its Wiphy does not advertise {' and '.join(missing)}. "
            "Dropping those CONNECT attributes would break LDN "
            "authentication; use a compatible mac80211 adapter/driver."
            f"{legacy_hint}"
        )

    async def _down(self, index: int) -> None:
        # update_link(change=IFF_UP, flags=0) clears only the administrative UP
        # bit.  Type changes are rejected by most drivers while the netdev is up.
        await self._router.update_link(
            socket.AF_UNSPEC,
            0,
            index,
            0,
            self.wlan_module.IFF_UP,
            {},
        )

    async def _up(self, index: int) -> None:
        """Restore only the administrative UP bit captured before scanning."""
        await self._router.update_link(
            socket.AF_UNSPEC,
            0,
            index,
            self.wlan_module.IFF_UP,
            self.wlan_module.IFF_UP,
            {},
        )

    async def _down_for_cleanup(self, index: int) -> None:
        """Bound final DOWN even if cancellation stopped route-netlink RX."""

        trio_module = _required(self.ldn, "trio")
        completed = False
        with trio_module.move_on_after(_STATION_DOWN_TIMEOUT_SECONDS):
            await self._down(index)
            completed = True
        if not completed:
            raise RuntimeError(
                "single-interface cleanup timed out while bringing the "
                "station DOWN"
            )

    async def _close_socket_for_cleanup(
        self, interface: object, *, label: str
    ) -> None:
        """Close an interface socket without making shielded cleanup hang."""

        trio_module = _required(self.ldn, "trio")
        completed = False
        with trio_module.move_on_after(_INTERFACE_SOCKET_CLOSE_TIMEOUT_SECONDS):
            await _close_socket(interface)
            completed = True
        if not completed:
            raise RuntimeError(
                f"single-interface cleanup timed out closing the {label} socket"
            )

    async def _set_type(
        self,
        index: int,
        interface_type: int,
        *,
        monitor_flags: dict[int, bool] | None = None,
        address: bytes | None = None,
    ) -> None:
        nl = self._nl80211
        await self._down(index)
        try:
            request = {
                nl.NL80211_ATTR_IFINDEX: index,
                nl.NL80211_ATTR_IFTYPE: interface_type,
            }
            if monitor_flags is not None:
                request[nl.NL80211_ATTR_MNTR_FLAGS] = monitor_flags
            if address is not None:
                request[nl.NL80211_ATTR_MAC] = address
            await self._wlan.request(nl.NL80211_CMD_SET_INTERFACE, request)
            attributes = await self._attributes_for_index(index)
            actual_type = attributes.get(nl.NL80211_ATTR_IFTYPE)
            if actual_type is None:
                raise RuntimeError(
                    f"driver did not report the nl80211 interface type for "
                    f"{self.ifname}"
                )
            if actual_type != interface_type:
                raise RuntimeError(
                    f"driver did not keep requested nl80211 interface type "
                    f"{interface_type} on {self.ifname} (reported {actual_type})"
                )
        finally:
            # 88XXau may bring the netdev back up as a side effect of changing
            # its type. Keep it down until Monitor/Station deliberately starts.
            # This is cleanup of a failed SET_INTERFACE, so do not let a lost
            # route-netlink ACK mask the original error or strand the caller.
            trio_module = _required(self.ldn, "trio")
            completed = False
            with trio_module.move_on_after(_STATION_DOWN_TIMEOUT_SECONDS):
                await self._down(index)
                completed = True
            if not completed:
                self.log(
                    "[live] nl80211 DOWN acknowledgement timed out after "
                    "interface type change; leaving outer cleanup to retry"
                )

    async def _restore_station_type(self, index: int, address: bytes) -> None:
        """Best-effort bounded mode restore used while unwinding a session.

        A lost nl80211 reply must not strand the async-context-manager exit.
        The outer transport cleanup still owns the administrative DOWN and
        will report any remaining radio lease to the caller.
        """
        trio_module = _required(self.ldn, "trio")
        completed = False
        with trio_module.move_on_after(_INTERFACE_TYPE_RESTORE_TIMEOUT_SECONDS):
            await self._set_type(
                index,
                self._nl80211.NL80211_IFTYPE_STATION,
                address=address,
            )
            completed = True
        if not completed:
            self.log(
                f"[live] timed out restoring {self.ifname} to station mode; "
                "outer radio cleanup will retry"
            )

    async def _attributes_for_index(self, index: int) -> dict[int, Any]:
        nl = self._nl80211
        messages = await self._wlan.request(
            nl.NL80211_CMD_GET_INTERFACE,
            {nl.NL80211_ATTR_IFINDEX: index},
        )
        if not messages:
            raise RuntimeError(
                f"nl80211 returned no information for interface {self.ifname}"
            )
        return messages[0].attributes

    async def _prime_station_bss(
        self,
        station: object,
        ssid: str,
        channel: int,
    ) -> None:
        """Give mac80211 a longer directed scan for an LDN hidden BSS.

        Some USB mac80211 drivers turn NL80211_CMD_CONNECT into a very short
        (~200 ms) directed scan.  LDN hosts advertise the room with action
        frames and may not answer inside that tiny window, causing a local
        ``TIMED_OUT``/status 1 before authentication is even attempted.  A
        bounded directed scan on the already selected channel populates the
        cfg80211 BSS cache without changing the authentication protocol.
        """

        channels = getattr(self.wlan_module, "Channels", None)
        frequency = channels.get(channel) if isinstance(channels, dict) else None
        if not isinstance(frequency, int):
            return

        await station.up()
        command = [
            "iw",
            "dev",
            self.ifname,
            "scan",
            "freq",
            str(frequency),
            "duration",
            "3000",
            "duration-mandatory",
            "ssid",
            ssid,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.log(f"[live] directed LDN BSS scan unavailable: {exc}")
            return

        target = f"SSID: {ssid}"
        found = result.returncode == 0 and any(
            line.strip() == target for line in result.stdout.splitlines()
        )
        if found:
            self.log("[live] directed LDN BSS scan found the advertised SSID")
        elif result.returncode == 0:
            self.log("[live] directed LDN BSS scan did not expose the advertised SSID")
        else:
            self.log(
                "[live] directed LDN BSS scan failed "
                f"(iw exit {result.returncode}); continuing with driver connect"
            )

    def _shield(self):
        trio_module = _required(self.ldn, "trio")
        return trio_module.CancelScope(shield=True)

    @asynccontextmanager
    async def create_monitor(
        self, phyname: str, _requested_ifname: str, channel: int | None = None
    ) -> AsyncIterator[object]:
        """Temporarily turn the existing netdev into the scan monitor."""

        nl = self._nl80211
        attributes = await self._attributes(phyname)
        index = attributes[nl.NL80211_ATTR_IFINDEX]
        address = self.wlan_module.MACAddress(attributes[nl.NL80211_ATTR_MAC])
        was_up = _interface_was_up(self.ifname)
        # Mirror the upstream monitor creation request on the *only* monitor
        # transition.  Without OTHER_BSS, the station adapter can receive
        # ordinary ch6 traffic yet miss the host's LDN action advertisements.
        # Do not send a second SET_INTERFACE: stock 88XXau may then revert the
        # netdev to managed mode.
        monitor = None
        try:
            await self._set_type(
                index,
                nl.NL80211_IFTYPE_MONITOR,
                monitor_flags={nl.NL80211_MNTR_FLAG_OTHER_BSS: True},
            )
            monitor = self.wlan_module.Monitor(
                self._wlan, self._router, self.ifname, index, address
            )
            await monitor.activate()
            if channel is not None:
                # This first SET_CHANNEL precedes _ObservedMonitor and its
                # optional bounded ACK wait. An outer cancellation here opens
                # no RX window and previously left no channel-command evidence.
                # Observe only: setup latency is not proof of RF tuning, and
                # an interrupted userspace await need not cancel the driver.
                initial_hop_started = time.monotonic()
                initial_hop_completed = False
                try:
                    await monitor.set_channel(channel)
                    initial_hop_completed = True
                finally:
                    self.log(
                        "[diag] LDN initial monitor channel command phase=setup "
                        f"requested_channel={channel} "
                        f"completed={'true' if initial_hop_completed else 'false'} "
                        f"elapsed_s={max(0.0, time.monotonic() - initial_hop_started):.3f}"
                    )
            self.log(
                f"[live] reusing {self.ifname} in monitor mode for LDN scan"
            )
            yield monitor
        finally:
            # Trio cancellation remains active while async-context managers
            # unwind.  Shield teardown so DOWN + station restoration cannot be
            # skipped halfway through.
            with self._shield():
                try:
                    if monitor is not None:
                        await self._close_socket_for_cleanup(
                            monitor, label="monitor"
                        )
                finally:
                    # Scan and association are sequential.  Returning to
                    # station here also makes an aborted scan safe.
                    await self._restore_station_type(index, bytes(address))
                    if was_up:
                        await self._up(index)

    @asynccontextmanager
    async def connect_network(
        self,
        phyname: str,
        _requested_ifname: str,
        ssid: str,
        channel: int,
        key: bytes | None,
        bssid: bytes | None = None,
    ) -> AsyncIterator[object]:
        """Join through the existing netdev without creating/deleting a VIF."""

        nl = self._nl80211
        # Scan remains available on limited drivers, but association must not
        # issue a CONNECT request unless both custom-control-port capabilities
        # required by ldn are explicitly advertised by this Wiphy.
        await self._require_ldn_control_port(phyname)
        attributes = await self._attributes(phyname)
        index = attributes[nl.NL80211_ATTR_IFINDEX]
        address = self.wlan_module.MACAddress(attributes[nl.NL80211_ATTR_MAC])
        station = None
        connect_stage = "station-setup"
        try:
            await self._set_type(index, nl.NL80211_IFTYPE_STATION)

            # ldn's generic Station leaves NL80211_ATTR_MAC out of CONNECT.
            # For a hidden LDN BSS that makes mac80211 issue a broadcast
            # directed-SSID scan, which some USB radios complete without ever
            # learning the Switch BSS.  The advertisement already authenticates
            # and supplies the host MAC, so pin that BSSID on CONNECT.
            delegate_wlan = self._wlan
            pinned_bssid = (
                bytes(bssid)
                if isinstance(bssid, (bytes, bytearray, memoryview))
                and len(bssid) == 6
                else None
            )
            trio_module = _required(self.ldn, "trio")
            owner_log = self.log

            class _StationWlan:
                def __getattr__(self, name):
                    return getattr(delegate_wlan, name)

                async def request(self, command, request_attributes):
                    nonlocal connect_stage
                    if command == nl.NL80211_CMD_CONNECT:
                        if pinned_bssid is not None:
                            request_attributes = dict(request_attributes)
                            request_attributes[nl.NL80211_ATTR_MAC] = pinned_bssid
                        # The seeded-BSS wrapper's older "CONNECT request"
                        # marker runs in _prime_station_bss, before upstream
                        # Station.connect even awaits its second UP. Observe
                        # the real backend boundary, not that preflight marker.
                        # Entry proves an await, NOT wire submission or RF TX.
                        connect_stage = "connect-request-pending"
                        owner_log(
                            "[diag] LDN CONNECT request boundary phase=awaiting-ack"
                        )
                        response = await delegate_wlan.request(
                            command, request_attributes
                        )
                        connect_stage = "connect-event-pending"
                        owner_log(
                            "[diag] LDN CONNECT request boundary phase=acknowledged"
                        )
                        return response
                    if command == nl.NL80211_CMD_DISCONNECT:
                        completed = False
                        response = []
                        # Station.connect performs this request from its
                        # cancellation-sensitive __aexit__. A cancelled
                        # scope would otherwise prevent even the bounded
                        # timeout from starting, leaving the radio associated
                        # until the outer cleanup happens and racing the next
                        # SET_INTERFACE against the old link. Shield only this
                        # best-effort request; the deadline remains bounded.
                        with trio_module.CancelScope(shield=True):
                            with trio_module.move_on_after(
                                _STATION_DISCONNECT_TIMEOUT_SECONDS
                            ):
                                response = await delegate_wlan.request(
                                    command, request_attributes
                                )
                                completed = True
                        if not completed:
                            owner_log(
                                "[live] nl80211 DISCONNECT acknowledgement "
                                "timed out; continuing with mandatory "
                                "interface DOWN"
                            )
                        return response
                    return await delegate_wlan.request(
                        command, request_attributes
                    )

                async def receive(self):
                    nonlocal connect_stage
                    # ldn 0.0.17 accepts the first CONNECT multicast on its
                    # generic socket.  With a pinned peer, keep consuming until
                    # the event belongs to this exact ifindex/BSSID.
                    while True:
                        message = await delegate_wlan.receive()
                        if getattr(message, "type", None) != nl.NL80211_CMD_CONNECT:
                            return message
                        if pinned_bssid is None:
                            # Preserve upstream's unpinned acceptance policy.
                            if connect_stage == "connect-event-pending":
                                connect_stage = "connect-result-received"
                            return message
                        message_attributes = getattr(message, "attributes", {})
                        if not isinstance(message_attributes, dict):
                            continue
                        if (
                            message_attributes.get(nl.NL80211_ATTR_IFINDEX)
                            != index
                        ):
                            self.log_ignored("different interface")
                            continue
                        event_mac = message_attributes.get(nl.NL80211_ATTR_MAC)
                        if event_mac is not None:
                            try:
                                event_mac = bytes(event_mac)
                            except (TypeError, ValueError):
                                self.log_ignored("invalid peer address")
                                continue
                            if event_mac != pinned_bssid:
                                self.log_ignored("different peer")
                                continue
                        if connect_stage == "connect-event-pending":
                            connect_stage = "connect-result-received"
                        owner_log(_connect_event_diagnostic(nl, message_attributes))
                        return message

                @staticmethod
                def log_ignored(reason):
                    owner_log(
                        "[live] ignored unrelated nl80211 CONNECT event "
                        f"({reason})"
                    )

            wlan = _StationWlan()
            if pinned_bssid is not None:
                self.log("[live] pinning the advertised LDN BSSID for connect")

            station = self.wlan_module.Station(
                wlan,
                self._router,
                self.ifname,
                index,
                address,
                ssid,
                channel,
                key,
            )
            seed = getattr(self, "bss_seed", None)
            if seed is None:
                await self._prime_station_bss(station, ssid, channel)
            else:
                await seed.prime(self, station, ssid, channel, pinned_bssid)
            self.log(
                f"[live] reusing {self.ifname} in managed mode for LDN join"
            )
            connect_stage = "station-start"
            async with station.connect():
                connect_stage = "wlan-ready"
                yield station
        finally:
            # Synchronous and before cleanup: cancellation/DISCONNECT must not
            # overwrite which predecessor was pending. wlan-ready establishes
            # WLAN/key/action-registration readiness only, not LDN auth or IP.
            self.log(f"[diag] LDN WLAN handoff exit stage={connect_stage}")
            with self._shield():
                try:
                    if station is not None:
                        await self._close_socket_for_cleanup(
                            station, label="station"
                        )
                finally:
                    # Station.connect issues a bounded best-effort DISCONNECT
                    # through _StationWlan.  DOWN is the authoritative fallback
                    # if that request is cancelled, rejected, or times out.
                    await self._down_for_cleanup(index)


@asynccontextmanager
async def _keep_factory_receivers_alive(wlan_module: object, trio_module: object):
    """Keep netlink ACK receivers alive until the consumer has unwound.

    Opening create_factory in the consumer task makes its receiver nurseries
    inherit scan/join cancellation.  Shielding DOWN/DEL_INTERFACE afterwards
    cannot revive those receivers: netlink.request then waits forever for an
    ACK nobody can deliver.  A sibling owns the backend instead, shielding its
    receiver tree only after startup.  The consumer stays cancellable and its
    finally releases the backend *after* interface cleanup, even on failure.
    """

    released = trio_module.Event()

    async def serve(*, task_status):
        # Startup remains cancellable; no shield may strand nursery.start().
        with trio_module.CancelScope() as lifetime:
            async with wlan_module.create_factory() as backend:
                lifetime.shield = True
                task_status.started(backend)
                await released.wait()

    async with trio_module.open_nursery() as nursery:
        try:
            backend = await nursery.start(serve)
            yield backend
        finally:
            # No checkpoint here: this must also run under active cancellation.
            released.set()


@asynccontextmanager
async def _factory(ldn_module: object, ifname: str, log=print):
    wlan_module = _required(ldn_module, "wlan")
    trio_module = _required(ldn_module, "trio")
    async with _keep_factory_receivers_alive(wlan_module, trio_module) as factory:
        yield ExistingInterfaceFactory(ldn_module, factory, ifname, log=log)


class _ScanWindowObservation:
    """Bounded, payload-free receive evidence, not proof of transmitter silence.

    Times are monotonic userspace receive times, not hardware capture times.
    Off-channel packets can be queued across a hop; they are counted, never
    discarded or mistaken for proof that the channel command failed. Packets
    received during SET_CHANNEL itself remain in the scan-wide counters, but
    outside these post-ACK windows. Packetless intervals prove no RX frequency.
    """

    def __init__(
        self,
        channel: int,
        started: float,
        hop_elapsed: float = 0.0,
        window_index: int | None = None,
    ):
        self.channel = channel
        self.window_index = window_index
        self.expected_frequency = (2407 if channel <= 14 else 5000) + channel * 5
        self.started = started
        # Time spent in SET_CHANNEL is outside the receive dwell. Record it so
        # the ACK-before-tune hypothesis is falsifiable without packet data.
        self.hop_elapsed = max(0.0, hop_elapsed)
        self.frames = 0
        self.candidates = 0
        self.frequencies: dict[int, int] = {}
        self.unknown = 0
        self.other = 0
        self.off_channel = 0
        # Positive evidence that the driver delivered a frame on the requested
        # RF channel. A nonzero frame count alone cannot distinguish a stuck
        # hop from a quiet Switch; keep this payload-free diagnostic explicit.
        self.expected_frequency_frames = 0
        # A candidate reached the authenticated NetworkInfo path but the
        # caller's freshness/identity predicate refused it. Keep this
        # separate from candidate==0 (RF/decoder absence) and off-channel
        # provenance rejection.
        self.predicate_rejections = 0
        # Count identities returned by targeted fallback after their last RX window.
        # This makes stale identity reuse distinguishable from RF advertisement loss.
        self.stale_fallbacks = 0
        # Distinguish a healthy RX path carrying ordinary traffic from a
        # decoder-visible action stream.  Counts are payload-free and make a
        # candidate-zero capture falsifiable without retaining frame data.
        self.action_decoded = 0
        self.action_decode_errors = 0
        self.non_ldn_actions = 0
        # Fixed-cardinality pre-decoder evidence; never retain packet bytes or
        # exception messages. Radiotap has already been decoded by Monitor.recv.
        self.fc_counts = dict.fromkeys((
            *(f"version{i}" for i in range(4)),
            *(f"type{i}" for i in range(4)),
            *(f"subtype{i}" for i in range(16)),
            "nonbytes", "short_fc", "short_mac", "action_header",
            "error_value", "error_parse", "error_other",
            "action_error", "action_error_value", "action_error_parse",
        ), 0)
        self._last_action_header = False
        # Compatibility name: ldn 0.0.17 derives NetworkInfo.channel from
        # Radiotap, NOT an independent advertised channel. This is a decoded
        # metadata consistency guard (also useful for custom Scanner adapters),
        # not proof of a stale host identity when it increments.
        self.declared_channel_mismatches = 0
        self.transitions = 0
        self.previous_frequency: int | None = None
        self.first_candidate: float | None = None
        self.last_candidate: float | None = None
        self.max_candidate_gap = 0.0
        self.first_rx: float | None = None
        self.last_rx: float | None = None
        self.max_rx_gap: float | None = None

    def record_frame_control(self, data: object) -> None:
        self._last_action_header = False
        c = self.fc_counts
        if type(data) not in (bytes, bytearray):
            c["nonbytes"] += 1
            return
        c["short_mac"] += len(data) < 24
        if len(data) < 2:
            c["short_fc"] += 1
            return
        # Frame control is little endian; these fields occupy its first byte.
        version, kind, subtype = data[0] & 3, (data[0] >> 2) & 3, data[0] >> 4
        c[f"version{version}"] += 1
        c[f"type{kind}"] += 1
        c[f"subtype{subtype}"] += 1
        self._last_action_header = version == 0 and kind == 0 and subtype == 13
        c["action_header"] += self._last_action_header

    def record_action_error(self, error: Exception, parse_error_type: object) -> None:
        # Exact trusted types only. Unknown/custom backends remain 'other'.
        category = ("value" if type(error) is ValueError else
                    "parse" if parse_error_type is not None and type(error) is parse_error_type
                    else "other")
        self.fc_counts[f"error_{category}"] += 1
        if self._last_action_header:
            self.fc_counts["action_error"] += 1
            if category in ("value", "parse"):
                self.fc_counts[f"action_error_{category}"] += 1

    def frame_control_summary(self, reason: str) -> str:
        window = "none" if self.window_index is None else str(self.window_index)
        fields = " ".join(f"{key}={value}" for key, value in self.fc_counts.items())
        return (f"[live] LDN RX structure index={window} requested_channel={self.channel} "
                f"end={reason} {fields}")

    def record(self, frequency: object, candidate: bool, now: float) -> None:
        self.frames += 1
        # A large frame total can be one queued burst followed by silence.
        # Keep RX timing independent of candidate/frequency validity, so empty
        # advertisement windows do not imply continuously working reception.
        if self.first_rx is None:
            self.first_rx = now
        if self.last_rx is not None:
            self.max_rx_gap = max(self.max_rx_gap or 0.0, now - self.last_rx)
        self.last_rx = now
        if (
            isinstance(frequency, bool)
            or not isinstance(frequency, int)
            or not 0 < frequency <= 65535
        ):
            self.unknown += 1
            # Do not bridge a missing-frequency interval as a known transition.
            self.previous_frequency = None
        else:
            if (
                self.previous_frequency is not None
                and frequency != self.previous_frequency
            ):
                self.transitions += 1
            self.previous_frequency = frequency
            self.off_channel += frequency != self.expected_frequency
            if frequency == self.expected_frequency:
                self.expected_frequency_frames += 1
            if frequency in self.frequencies or len(self.frequencies) < 16:
                self.frequencies[frequency] = self.frequencies.get(frequency, 0) + 1
            else:
                self.other += 1
        if candidate:
            self.candidates += 1
            if self.first_candidate is None:
                self.first_candidate = now
            if self.last_candidate is not None:
                self.max_candidate_gap = max(
                    self.max_candidate_gap, now - self.last_candidate
                )
            self.last_candidate = now

    def summary(self, now: float, reason: str) -> str:
        def elapsed(value: float | None) -> str:
            return "none" if value is None else f"{max(0.0, value - self.started):.3f}"

        frequencies = ",".join(
            f"{freq}:{count}" for freq, count in sorted(self.frequencies.items())
        ) or "none"
        tail = (
            "none" if self.last_candidate is None
            else f"{max(0.0, now - self.last_candidate):.3f}"
        )
        window = "none" if self.window_index is None else str(self.window_index)
        return (
            f"[live] LDN RX window index={window} requested_channel={self.channel} "
            f"elapsed_s={elapsed(now)} hop_elapsed_s={self.hop_elapsed:.3f} "
            f"end={reason} frames={self.frames} "
            f"candidates={self.candidates} frequencies={frequencies} "
            f"unknown_frequency={self.unknown} other_frequency={self.other} "
            f"off_channel_frames={self.off_channel} frequency_transitions={self.transitions} "
            f"expected_frequency_frames={self.expected_frequency_frames} "
            f"first_candidate_s={elapsed(self.first_candidate)} "
            f"last_candidate_s={elapsed(self.last_candidate)} "
            f"max_candidate_gap_s={self.max_candidate_gap:.3f} candidate_tail_s={tail} "
            f"first_rx_s={elapsed(self.first_rx)} last_rx_s={elapsed(self.last_rx)} "
            f"max_rx_gap_s={'none' if self.max_rx_gap is None else f'{self.max_rx_gap:.3f}'} "
            f"rx_tail_s={'none' if self.last_rx is None else f'{max(0.0, now - self.last_rx):.3f}'} "
            f"predicate_rejections={self.predicate_rejections} "
            f"stale_fallbacks={self.stale_fallbacks} "
            f"declared_channel_mismatches={self.declared_channel_mismatches} "
            f"action_decoded={self.action_decoded} "
            f"action_decode_errors={self.action_decode_errors} "
            f"non_ldn_actions={self.non_ldn_actions}"
        )


async def scan(
    ldn_module: object,
    keys: dict[str, bytes],
    *,
    phyname: str,
    ifname: str,
    # Revisit each stock-firmware social channel so an advertisement that falls
    # between earlier short windows can still be observed.  Firmware 22.1.0's
    # default LDN masks enable channels 1/6/11 and disable every 5 GHz channel.
    # Callers can still pass the full API-supported set for consoles whose
    # system settings were explicitly changed.
    # Keep the channel set separate from repetition.  The targeted caller
    # controls repetition with ``targeted_cycles``; embedding three copies
    # here multiplied the intended four-cycle live sweep into 36 windows.
    channels: tuple[int, ...] = (1, 6, 11),
    # A 110 ms dwell (the upstream minimum-style value) was too easy to miss
    # the Switch advertisement on USB rtw88 hardware: monitor activation and
    # a channel hop can consume much of that window.  Half a second keeps the
    # stock three-channel sweep short while covering several advertisement
    # intervals.
    dwell_time: float = 0.500,
    protocols: tuple[int, ...] = (1, 3),
    stop_when: Callable[[object], bool] | None = None,
    on_match: Callable[[object], Awaitable[None]] | None = None,
    preferred_channel: int | None = None,
    preferred_dwell_time: float = 6.0,
    preferred_first: bool = False,
    # A sweep can miss intermittent host/channel availability, but lossless
    # continuous 100 ms advertising cannot phase-hide from a 500 ms dwell.
    # Live callers can repeat/rotate sweeps within one monitor/VIF lifetime.
    targeted_cycles: int = 1,
    # Compatibility default: callers that need discovery truth must pass
    # False; live transport does so explicitly.
    retain_stale_fallbacks: bool = True,
    # Adds up to 100 ms per hop, outside the receive dwell. Off by default to
    # avoid perturbing timing while investigating phase-sensitive loss.
    diagnose_channel_readback: bool = False,
    log=print,
) -> list[object]:
    """Scan on an existing interface, optionally returning a fresh match.

    With no predicate this retains ldn 0.0.17's generic scan behaviour.  A
    predicate enables the targeted path used by the live client: matching LDN
    networks are short-lived, so it returns as soon as a usable advertisement
    is decoded instead of finishing the full multi-channel sweep.  While that
    targeted scan continues, a new advertisement from the same BSSID replaces
    the old NetworkInfo rather than leaving Z-A's rotating hidden SSID stale.
    """

    if not channels:
        return []
    if on_match is not None and stop_when is None:
        raise ValueError("on_match requires stop_when")
    if on_match is not None and not callable(on_match):
        raise TypeError("on_match must be an awaitable callback")
    wlan_module = _required(ldn_module, "wlan")
    for channel in channels:
        if not wlan_module.is_valid_channel(channel):
            raise ValueError(f"Invalid channel: {channel}")
    if (
        preferred_channel is not None
        and not wlan_module.is_valid_channel(preferred_channel)
    ):
        raise ValueError(f"Invalid preferred channel: {preferred_channel}")
    if preferred_dwell_time < 0:
        raise ValueError("preferred dwell time must not be negative")
    if (
        isinstance(targeted_cycles, bool)
        or not isinstance(targeted_cycles, int)
        or targeted_cycles < 1
    ):
        raise ValueError("targeted_cycles must be a positive integer")
    for protocol in protocols:
        if protocol not in (1, 3):
            raise ValueError(f"Invalid protocol: {protocol}")
    key_derivation = _required(ldn_module, "KeyDerivation")
    scanner_type = _required(ldn_module, "Scanner")
    derivations = {
        protocol: key_derivation(keys, protocol) for protocol in protocols
    }

    # Scanner.receive intentionally ignores every non-LDN frame.  Count the
    # raw monitor traffic as well so a zero-network result can distinguish an
    # idle/broken RX path from a working radio that simply heard no Nintendo
    # advertisement.  Only aggregate counts are logged; nearby MACs/SSIDs and
    # frame contents are never exposed.
    advertisement_header = bytes([0x7F, 0x00, 0x22, 0xAA, 0x04, 0x00, 0x01, 0x01])

    def _is_transient_channel_timeout(exc: BaseException) -> bool:
        """Recognize timeout-shaped nl80211 errors without swallowing device loss."""
        if isinstance(exc, TimeoutError):
            return True
        return isinstance(exc, OSError) and exc.errno in {
            errno.ETIMEDOUT,
            errno.EAGAIN,
            errno.EWOULDBLOCK,
        }

    def expected_frequency(channel: int) -> int | None:
        # 2.4 GHz social channels only; leave unusual/custom PHY channels
        # unclassified rather than inventing a frequency mapping.
        return {1: 2412, 6: 2437, 11: 2462}.get(channel)

    class _ObservedMonitor:
        def __init__(self, delegate: object):
            self.delegate = delegate
            self.frames = 0
            self.ldn_candidates = 0
            self.frames_without_frequency = 0
            # Keep only the last RX frequency as provenance for a decoded
            # match.  A frame can be queued across SET_CHANNEL; logging this
            # beside the requested window lets us test that hypothesis without
            # retaining payload, MAC, SSID, or NetworkInfo identity.
            self.last_frequency: int | None = None
            self.window: _ScanWindowObservation | None = None

        def set_filter(self, *args, **kwargs):
            return self.delegate.set_filter(*args, **kwargs)

        def finish_window(self, reason: str) -> None:
            if self.window is not None:
                window, self.window = self.window, None
                log(window.summary(time.monotonic(), reason))
                log(window.frame_control_summary(reason))

        async def set_channel(self, channel):
            self.finish_window("channel-change")
            # Do not carry the previous window's Radiotap provenance across a
            # hop.  Some drivers omit frequency on a queued frame; retaining
            # the old value would make that frame look like it was received on
            # the new channel and could let a stale advertisement pass the
            # targeted off-channel guard.
            self.last_frequency = None
            hop_started = time.monotonic()
            try:
                if diagnose_channel_readback:
                    # A lost nl80211 ACK must not consume the caller's whole
                    # join deadline while the scan appears to be idle. Keep
                    # the diagnostic path bounded, just like the corrective
                    # retry below, so a failed hop is explicit evidence.
                    trio_module = _required(ldn_module, "trio")
                    with trio_module.move_on_after(
                        _MONITOR_READBACK_TIMEOUT_SECONDS
                    ) as hop_scope:
                        result = await self.delegate.set_channel(channel)
                    if hop_scope.cancelled_caught:
                        elapsed = max(0.0, time.monotonic() - hop_started)
                        log(
                            "[diag] LDN channel command timed_out=true "
                            f"requested_channel={channel} elapsed_s={elapsed:.3f}"
                        )
                        raise TimeoutError(
                            f"timed out setting channel {channel}"
                        )
                else:
                    result = await self.delegate.set_channel(channel)
            except BaseException:
                # This command is OUTSIDE move_on_after(receive dwell). A lost
                # ACK can consume the outer join deadline without opening any
                # RX window. Preserve that boundary on cancellation/error rather
                # than making it look like a completed, candidate-empty dwell.
                # No checkpoint, exception text, peer or payload in diagnostics;
                # interruption does not prove that the driver failed to tune.
                log(
                    "[diag] LDN channel command interrupted "
                    f"requested_channel={channel} "
                    f"elapsed_s={max(0.0, time.monotonic() - hop_started):.3f}"
                )
                raise
            hop_elapsed = time.monotonic() - hop_started
            if diagnose_channel_readback:
                # Query latency is not receive dwell or SET_CHANNEL latency.
                # Queued packets still face the existing provenance gates.
                readback = await _monitor_channel_readback(
                    ldn_module, self.delegate, channel, log=log
                )
                # Some rtw88 monitor paths ACK SET_CHANNEL before applying it.
                # A single corrective request closes that race while keeping
                # the retry bounded and payload-free. Persistent mismatch is
                # still only diagnostic; normal frequency gates decide whether
                # received frames are usable.
                if readback == "mismatch":
                    retry_started = time.monotonic()
                    # The first ACK/readback mismatch is precisely the driver
                    # race this diagnostic targets.  Do not let the corrective
                    # command become a new unbounded outer-deadline hang:
                    # nl80211 can wait forever for a dead firmware ACK.
                    corrective_completed = False
                    try:
                        with _required(ldn_module, "trio").move_on_after(
                            _MONITOR_READBACK_TIMEOUT_SECONDS
                        ) as retry_scope:
                            await self.delegate.set_channel(channel)
                            corrective_completed = True
                    finally:
                        # The initial ACK succeeded, but this separate command
                        # can still be cancelled before any RX window opens.
                        # Record it synchronously, including local timeout;
                        # do not confuse it with the following readback wait.
                        if not corrective_completed:
                            log(
                                "[diag] LDN channel command interrupted "
                                f"requested_channel={channel} "
                                f"elapsed_s={max(0.0, time.monotonic() - retry_started):.3f} "
                                "phase=readback-correction"
                            )
                    retry_elapsed = max(0.0, time.monotonic() - retry_started)
                    if retry_scope.cancelled_caught:
                        log(
                            "[diag] LDN channel command retry timed_out=true "
                            "reason=readback-mismatch "
                            f"requested_channel={channel} elapsed_s={retry_elapsed:.3f}"
                        )
                        raise TimeoutError(
                            f"timed out correcting channel {channel} after readback mismatch"
                        )
                    log(
                        "[diag] LDN channel command retry timed_out=false "
                        "reason=readback-mismatch "
                        f"requested_channel={channel} elapsed_s={retry_elapsed:.3f}"
                    )
                    corrected = await _monitor_channel_readback(
                        ldn_module, self.delegate, channel, log=log
                    )
                    if corrected == "mismatch":
                        # Do not open a receive dwell on a VIF that the kernel
                        # still reports on the previous channel. Without this
                        # guard the scan records a plausible-looking empty
                        # window and loses the actual ACK/readback divergence.
                        log(
                            "[diag] LDN channel readback persistent_mismatch "
                            f"requested_channel={channel}"
                        )
                        raise RuntimeError(
                            f"channel {channel} remained mismatched after corrective retry"
                        )
            ready = time.monotonic()
            self.window = _ScanWindowObservation(
                channel, ready, hop_elapsed=hop_elapsed
            )
            return result

        async def recv(self):
            radiotap = await self.delegate.recv()
            self.frames += 1
            self.last_frequency = getattr(radiotap, "frequency", None)
            if self.last_frequency is None:
                self.frames_without_frequency += 1
            candidate = False
            if self.window is not None:
                self.window.record_frame_control(radiotap.data)
            action_type = getattr(wlan_module, "ActionFrame", None)
            if action_type is not None:
                try:
                    action = action_type()
                    action.decode(radiotap.data)
                    if self.window is not None:
                        self.window.action_decoded += 1
                    if action.action.startswith(advertisement_header):
                        self.ldn_candidates += 1
                        candidate = True
                    else:
                        if self.window is not None:
                            self.window.non_ldn_actions += 1
                except Exception as error:
                    if self.window is not None:
                        self.window.action_decode_errors += 1
                        self.window.record_action_error(
                            error, getattr(getattr(wlan_module, "streams", None), "ParseError", None)
                        )
            if self.window is not None:
                self.window.record(
                    getattr(radiotap, "frequency", None), candidate, time.monotonic()
                )
            return radiotap

    # Resolve the complete receive order before creating the monitor.  Retry
    # scans may lead with a last-seen channel other than channels[0]; creating
    # on channels[0] would first tune away from the very room being retried.
    # Generic scans intentionally retain the upstream channel/dwell policy.
    base_windows = [(channel, dwell_time) for channel in channels]
    preferred_window = None
    if (
        stop_when is not None
        and preferred_channel is not None
        and preferred_dwell_time > 0
    ):
        preferred_window = (preferred_channel, preferred_dwell_time)
    if stop_when is not None:
        # Repeat the social-channel sweep, not the long preferred retry.  Rotate
        # the starting point on each cycle: an identical ordered sweep can stay
        # phase-locked to a short, recurrent advertisement window, while a
        # rotation samples the same channel budget at different phases.  Keep
        # one monitor/VIF alive so this does not reintroduce setup gaps.
        scan_windows = [
            window
            for cycle in range(targeted_cycles)
            for window in (
                base_windows[cycle % len(base_windows):]
                + base_windows[:cycle % len(base_windows)]
            )
        ]
        if preferred_window is not None:
            if preferred_first:
                scan_windows.insert(0, preferred_window)
            else:
                scan_windows.append(preferred_window)
    else:
        scan_windows = base_windows
    initial_channel = scan_windows[0][0]
    log(
        f"[live] LDN scan plan mode={'generic' if stop_when is None else 'targeted'} "
        f"initial_channel={initial_channel} windows={len(scan_windows)} "
        f"channels={','.join(str(c) for c in sorted({c for c, _ in scan_windows}))} "
        f"dwell_budget_s={sum(d for _, d in scan_windows):.3f} "
        f"cycles={targeted_cycles if stop_when is not None else 1}"
    )
    setup_started = time.monotonic()
    async with _factory(ldn_module, ifname, log) as factory:
        # Select the first channel during monitor creation, not only via the
        # Scanner's later SET_CHANNEL.  Some drivers acknowledge the first hop
        # before a new monitor has inherited the requested channel, leaving the
        # initial receive window on the previous channel intermittently.
        # The explicit set_channel below remains intentional: it revalidates
        # the channel immediately before receive.
        async with factory.create_monitor(
            phyname, ifname, channel=initial_channel
        ) as monitor:
            # Command intent/setup duration only; RX-window Radiotap counters
            # remain the evidence for actual received frequencies.  Setup and
            # later channel commands are outside dwell_budget_s, but consume
            # the caller's outer join deadline.
            log(
                f"[live] LDN scan monitor ready initial_channel={initial_channel} "
                f"setup_s={max(0.0, time.monotonic() - setup_started):.3f}"
            )
            observed = _ObservedMonitor(monitor)
            scanner = scanner_type(derivations, observed)
            result: list[object] | None = None
            # This is deliberately distinct from a dwell expiry: an outer
            # join deadline can cancel the scan between hops and leave the
            # final windows unopened.  Keep that boundary visible in logs.
            scan_completed = False
            scan_exit_reason = "not_started"
            # Generic Scanner.scan has no targeted window counters, but the
            # lifecycle reason still needs to be emitted without referencing
            # a targeted-only local.
            channel_windows_skipped = 0
            try:
                if stop_when is None:
                    result = list(
                        await scanner.scan(list(channels), dwell_time)
                    )
                else:
                    # The upstream Scanner de-duplicates solely by BSSID and
                    # keeps the first advertisement for the entire sweep. Z-A
                    # rotates NetworkInfo while retaining its host MAC. Keep
                    # the newest value and stop as soon as the requested room
                    # is decoded, before that identity can expire.
                    trio_module = _required(ldn_module, "trio")
                    latest_by_address: dict[object, object] = {}
                    latest_channel_by_address: dict[object, int] = {}
                    latest_window_by_address: dict[object, int] = {}
                    previous_channel: int | None = None
                    channel_windows_attempted = 0
                    # Keep command attempts separate from actual receive
                    # windows. A hop can fail after the previous window has
                    # closed; counting it as covered made intermittent loss
                    # look like a quiet RF window.
                    channel_windows_opened = 0
                    channel_windows_skipped = 0
                    for window_index, (channel, window_time) in enumerate(scan_windows):
                        # A transient nl80211/driver hop failure must not abort
                        # the whole rotating sweep. The monitor is still alive,
                        # so the next hop is a safe recovery boundary;
                        # cancellation remains uncaught and still tears down
                        # the VIF promptly. Previously one failed readback made
                        # all remaining channels disappear until the outer retry.
                        channel_windows_attempted += 1
                        retry_failed = False
                        for hop_attempt in range(2):
                            try:
                                await observed.set_channel(channel)
                                break
                            except (RuntimeError, TimeoutError, OSError) as exc:
                                # A bounded ACK/readback timeout is often a
                                # transient firmware race. Retry the SAME hop
                                # once before declaring its RX window unopened;
                                # advancing immediately can alias a short
                                # Switch advertisement window and manufacture
                                # periodic loss. Device removal and other
                                # non-timeout OSErrors still propagate.
                                if not _is_transient_channel_timeout(exc):
                                    if isinstance(exc, RuntimeError):
                                        channel_windows_skipped += 1
                                        log(
                                            "[diag] LDN channel window skipped "
                                            f"index={window_index} requested_channel={channel} "
                                            f"reason={type(exc).__name__} "
                                            "opened=false frames=0 candidates=0"
                                        )
                                        retry_failed = True
                                        break
                                    raise
                                if hop_attempt == 0:
                                    log(
                                        "[diag] LDN channel command retry "
                                        f"requested_channel={channel} attempt=1 "
                                        "reason=transient_timeout"
                                    )
                                    continue
                                channel_windows_skipped += 1
                                log(
                                    "[diag] LDN channel window skipped "
                                    f"index={window_index} "
                                    f"requested_channel={channel} "
                                    f"reason={type(exc).__name__} "
                                    "opened=false frames=0 candidates=0"
                                )
                                retry_failed = True
                        if retry_failed:
                            continue
                        if observed.window is not None:
                            observed.window.window_index = window_index
                        channel_windows_opened += 1
                        # Preserve the existing fallback policy, but expose
                        # stale-identity candidates instead of hiding them.
                        if previous_channel is not None and previous_channel != channel:
                            cross_channel = sum(
                                learned != channel
                                for learned in latest_channel_by_address.values()
                            )
                            if cross_channel:
                                log(
                                    "[diag] LDN cross-channel NetworkInfo "
                                    f"fallback_candidates={cross_channel} "
                                    f"from_channel={previous_channel} "
                                    f"requested_channel={channel}"
                                )
                        previous_channel = channel
                        matched_network = None
                        with trio_module.move_on_after(window_time) as receive_scope:
                            while True:
                                candidates_before = (
                                    observed.window.candidates
                                    if observed.window is not None else 0
                                )
                                network = await scanner.receive()
                                address = getattr(network, "address", None)
                                try:
                                    address_key: object = bytes(address)
                                except (TypeError, ValueError):
                                    address_key = str(address)
                                # A frame queued across SET_CHANNEL can be
                                # decoded by Scanner after the hop. Binding
                                # that stale advertisement would seed/connect
                                # on the wrong channel and make the next retry
                                # look like intermittent advertisement loss.
                                # Unknown frequencies remain accepted for
                                # non-standard PHYs.
                                expected = expected_frequency(channel)
                                actual = observed.last_frequency
                                off_channel = (
                                    expected is not None
                                    and actual is not None
                                    and actual != expected
                                )
                                # Defensive consistency check for custom
                                # Scanner implementations. In audited ldn
                                # 0.0.17 this value is derived from this same
                                # Radiotap frequency; parse_advertisement does
                                # not set it. It is NOT a second host-side
                                # channel assertion or independent stale proof.
                                declared_channel = getattr(network, "channel", None)
                                declared_off_channel = (
                                    isinstance(declared_channel, int)
                                    and not isinstance(declared_channel, bool)
                                    and declared_channel != channel
                                )
                                if off_channel:
                                    log(
                                        "[diag] LDN match rejected off-channel "
                                        f"requested_channel={channel} "
                                        f"rx_frequency={actual} expected_frequency={expected}"
                                    )
                                if declared_off_channel:
                                    if observed.window is not None:
                                        observed.window.declared_channel_mismatches += 1
                                    log(
                                        "[diag] LDN match rejected declared-channel "
                                        f"mismatch requested_channel={channel} "
                                        f"network_channel={declared_channel}"
                                    )
                                # Validate provenance BEFORE the predicate:
                                # the fresh-identity wrapper latches its first
                                # exact match (or rotation baseline) inside
                                # stop_when. Rejecting afterwards cannot undo
                                # that state and can suppress the next valid
                                # advertisement or manufacture a rotation.
                                is_match = (
                                    False
                                    if off_channel or declared_off_channel
                                    else stop_when(network)
                                )
                                if (
                                    observed.window is not None
                                    and observed.window.candidates > candidates_before
                                    and not off_channel
                                    and not declared_off_channel
                                    and not is_match
                                ):
                                    # Affirmative evidence that an advertisement
                                    # was received and decoded; don't report a
                                    # freshness rejection as RF loss.
                                    observed.window.predicate_rejections += 1
                                # Do not let a queued off-channel decode leak
                                # into the fallback result.  The targeted path
                                # may finish all windows without a match, and
                                # its caller can otherwise treat this stale
                                # NetworkInfo as a usable identity on the wrong
                                # channel during the next retry.
                                if not off_channel and not declared_off_channel:
                                    previous_network = latest_by_address.get(address_key)
                                    if previous_network is not None:
                                        previous_ssid = getattr(previous_network, "ssid", None)
                                        current_ssid = getattr(network, "ssid", None)
                                        previous_identity = (
                                            getattr(previous_network, "local_communication_id", None),
                                            getattr(previous_network, "scene_id", None),
                                            getattr(previous_network, "application_data", None),
                                            previous_ssid,
                                        )
                                        current_identity = (
                                            getattr(network, "local_communication_id", None),
                                            getattr(network, "scene_id", None),
                                            getattr(network, "application_data", None),
                                            current_ssid,
                                        )
                                        # Same source/BSSID is not sufficient
                                        # freshness evidence: Z-A can rotate
                                        # NetworkInfo while keeping the host
                                        # address. Keep the newest value as
                                        # before, but make that transition
                                        # observable without logging identity
                                        # bytes, MACs, or SSIDs. Transport's failed
                                        # WLAN identity exclusion compares SSID:
                                        # room regeneration can change only that
                                        # field, leaving comm/scene/app unchanged.
                                        # This is decoded-value comparison, NOT
                                        # proof of packet age or a freshness gate.
                                        if previous_identity != current_identity:
                                            previous_window = latest_window_by_address.get(
                                                address_key, window_index
                                            )
                                            log(
                                                "[diag] LDN NetworkInfo identity_refresh "
                                                f"window={window_index} previous_window={previous_window} "
                                                "same_address=true changed=true "
                                                f"ssid_changed={str(previous_ssid != current_ssid).lower()}"
                                            )
                                    latest_by_address[address_key] = network
                                    latest_channel_by_address[address_key] = channel
                                    latest_window_by_address[address_key] = window_index
                                log(
                                    "[live] decoded LDN NetworkInfo "
                                    f"match={is_match} "
                                    f"comm=0x{getattr(network, 'local_communication_id', 0):016x} "
                                    f"scene={getattr(network, 'scene_id', '?')} "
                                    f"channel={getattr(network, 'channel', '?')} "
                                    f"rx_frequency={observed.last_frequency or '?'} "
                                    f"participants={getattr(network, 'num_participants', '?')}/"
                                    f"{getattr(network, 'max_participants', '?')} "
                                    f"accept={getattr(network, 'accept_policy', '?')} "
                                    f"appbytes={len(getattr(network, 'application_data', b'') or b'')}"
                                )
                                if is_match:
                                    matched_network = network
                                    break
                        # Close the receive window before BSS seeding/CONNECT
                        # hooks, whose latency is not an advertisement gap.
                        if getattr(receive_scope, "cancelled_caught", False):
                            log(
                                "[diag] LDN receive dwell expired "
                                f"requested_channel={channel} dwell_s={window_time:.3f}"
                            )
                        # The fallback decision happens after all windows, but
                        # its evidence belongs to this final receive window:
                        # it says whether a quiet final channel would otherwise
                        # return NetworkInfo decoded before a hop. Set it before
                        # flushing, because finish_window() drops the mutable
                        # observation.
                        if (
                            matched_network is None
                            and window_index == len(scan_windows) - 1
                            and observed.window is not None
                        ):
                            observed.window.stale_fallbacks = sum(
                                learned_window != window_index
                                for learned_window in latest_window_by_address.values()
                            )
                        observed.finish_window(
                            "matched" if matched_network is not None else "dwell-expired"
                        )
                        if matched_network is not None:
                            # Await hooks outside the dwell cancellation scope,
                            # but while the monitor/factory are still alive.
                            if on_match is not None:
                                await on_match(matched_network)
                            log(
                                "[live] matching LDN advertisement found; "
                                "ending the channel sweep immediately"
                            )
                            result = [matched_network]
                            break
                    if result is None:
                        final_window = len(scan_windows) - 1
                        stale = sum(
                            window != final_window
                            for window in latest_window_by_address.values()
                        )
                        if stale:
                            log(
                                "[diag] LDN targeted scan returning stale "
                                f"fallback identities={stale} final_window={final_window}"
                            )
                        if retain_stale_fallbacks:
                            result = list(latest_by_address.values())
                        else:
                            # A NetworkInfo from an earlier channel window is
                            # not a discovery result for the current retry.
                            # Returning it made a quiet final window look like
                            # a successful advertisement and sent CONNECT into
                            # a stale BSS/identity. Keep the diagnostic count,
                            # but require a final-window observation by default.
                            result = [
                                network
                                for address, network in latest_by_address.items()
                                if latest_window_by_address[address] == final_window
                            ]
                # A sweep that reached its end but missed a hop is still
                # incomplete coverage, not evidence of a complete scan.
                scan_completed = (
                    stop_when is None or channel_windows_skipped == 0
                )
            except BaseException:
                # Preserve the distinction between outer cancellation or an
                # exception and a sweep that actually reached its final hop.
                scan_exit_reason = "cancelled_or_exception"
                raise
            else:
                scan_exit_reason = (
                    "skipped_window" if channel_windows_skipped else "completed"
                )
            finally:
                # Synchronous: retain evidence even under active cancellation,
                # without starting new work or delaying monitor cleanup.
                observed.finish_window("scan-exit")
                log(
                    "[live] LDN scan received "
                    f"{observed.frames} monitor frame(s), "
                    f"{observed.ldn_candidates} Nintendo advertisement candidate(s), "
                    f"{observed.frames_without_frequency} frame(s) without frequency"
                )
                if stop_when is not None:
                    log(
                        "[diag] LDN channel window totals "
                        f"attempted={channel_windows_attempted} opened={channel_windows_opened} skipped={channel_windows_skipped} "
                        f"planned={len(scan_windows)} terminal={'completed' if scan_completed else 'interrupted'} "
                        f"reason={scan_exit_reason if scan_exit_reason != 'not_started' else 'cancelled_or_exception'}"
                    )

            if result is None:
                raise RuntimeError("LDN scan ended without producing a result")
            if not result and stop_when is not None and channel_windows_skipped:
                # A failed SET_CHANNEL/readback never opened an RX dwell. A
                # miss with missing windows cannot establish a complete silent
                # sweep, even if the other windows received no packets. The
                # transport uses NoMonitorFramesError to authorize a guarded
                # USB reset; keep incomplete coverage on its ordinary retry
                # path instead. A later match still wins over an earlier skip.
                raise IncompleteScanError(
                    "LDN targeted scan incomplete: "
                    f"attempted={channel_windows_attempted} "
                    f"opened={channel_windows_opened} skipped={channel_windows_skipped}; "
                    "channel windows were not opened"
                )
            if not result and observed.frames == 0:
                raise NoMonitorFramesError(
                    f"LDN monitor on {ifname} completed a full scan without "
                    "receiving any 802.11 frame; the USB radio/driver may need "
                    "a reset"
                )
            return result


def _advertisement_queue_depth(queue: object) -> int:
    """Buffered items only; read-only, payload-free, and -1 when unavailable.

    Pinned ldn 0.0.17 Queue has neither qsize nor _qsize. Its Trio receiver
    statistics are synchronous and do not dequeue an item or add a waiter.
    A snapshot is not packet age or proof of loss/cancellation; a blocked
    rendezvous sender is not counted as a buffered item either.
    """
    try:
        qsize = getattr(queue, "qsize", None)
        if callable(qsize):
            value = qsize()
        else:
            value = getattr(queue, "_qsize", None)
            if value is None:
                receiver = getattr(queue, "_receiver", None)
                statistics = getattr(receiver, "statistics", None)
                value = statistics().current_buffer_used if callable(statistics) else None
    except Exception:
        # Diagnostics must never replace auth/assignment/cleanup exceptions.
        return -1
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else -1


async def _authenticate_single_interface_station(
    station: object,
    ldn_module: object,
    authenticate: Callable[[], Awaitable[None]],
    *,
    log: Callable[[str], None],
) -> None:
    """Keep the latest host action consumed by the pinned upstream auth loop.

    ldn 0.0.17 registers the participant before sending its auth response, but
    its station auth loop discards intervening ActionFrameEvents. Preserve one
    current-attempt action, NOT a scan/cache NetworkInfo. Only successful auth
    releases it to the normal source/decode/identity-checked event processor.
    The upstream request/challenge/response validation and retry loop run as-is.
    """
    interface = station._interface
    wlan = _required(ldn_module, "wlan")
    clock = _required(ldn_module, "trio").current_time
    station._pending_auth_advertisement = None
    # Preserve a metadata-only continuity witness: a successful auth must not
    # silently leave the event processor attached to a replacement interface or
    # advertisement queue.  The live 55555555 run had auth accepted but zero
    # post-auth events, so distinguish queue/interface discontinuity from host
    # silence without retaining frames, addresses, or payloads.
    auth_interface = interface
    auth_advertisements = getattr(station, "_advertisements", None)

    auth_queue_depth = _advertisement_queue_depth(auth_advertisements)

    class AuthInterface:
        pending = None
        actions = 0

        def __getattr__(self, name):
            return getattr(interface, name)

        async def send_custom_frame(self, *args, **kwargs):
            # Never carry a participant record across authentication retries.
            self.pending = None
            return await interface.send_custom_frame(*args, **kwargs)

        async def next_event(self):
            event = await interface.next_event()
            if (isinstance(event, wlan.ActionFrameEvent)
                    and event.frame.source == station._network.address):
                self.actions += 1
                # Replace even with a malformed/newer host action: never fall
                # back to an older assignment after contradictory new input.
                self.pending = (clock(), event)
            return event  # Upstream still sees every response/disassociation.

    proxy = AuthInterface()
    accepted = False
    station._interface = proxy
    try:
        await authenticate()
        accepted = True
        station._pending_auth_advertisement = proxy.pending
    finally:
        station._interface = interface
        proxy.pending = None
        same_interface = station._interface is auth_interface
        same_advertisement_queue = (
            getattr(station, "_advertisements", None) is auth_advertisements
        )
        log(
            "[diag] LDN auth action handoff "
            f"accepted={str(accepted).lower()} host_actions={proxy.actions} "
            f"pending={int(station._pending_auth_advertisement is not None)} "
            f"interface_continuity={str(same_interface).lower()} "
            f"advertisement_queue_continuity={str(same_advertisement_queue).lower()} "
            f"advertisement_queue_depth_before={auth_queue_depth} "
            f"advertisement_queue_depth_after={_advertisement_queue_depth(getattr(station, '_advertisements', None))}"
        )


class _StationEventObservation:
    """Cumulative station-event processing evidence; never an RF-silence claim.

    Counts start after authentication, including at most one auth_handoff event
    received earlier. They do not observe raw nl80211 messages or independent
    over-the-air traffic. Fixed counters retain no frame data.
    Assignment/processor summaries are snapshots, not additive windows.
    """

    def __init__(self):
        self.started = False
        self.counts = dict.fromkeys((
            "events", "auth_handoff", "actions", "other_source", "host_actions", "host_candidates",
            "decode_rejected", "candidate_decode_rejected", "decoded",
            "parse_failed", "identity_rejected", "queue_started", "queued",
            "custom", "disassociation", "other",
        ), 0)

    def summary(self, stage: str) -> str:
        if stage not in ("assignment-exit", "processor-exit"):
            raise ValueError("invalid station observation stage")
        counts = " ".join(f"{name}={value}" for name, value in self.counts.items())
        return (
            f"[diag] LDN station RX stage={stage} "
            f"started={str(self.started).lower()} {counts}"
        )


async def _process_single_interface_events(
    station: object,
    ldn_module: object,
    observation: _StationEventObservation,
    *,
    log: Callable[[str], None],
) -> None:
    """Pinned ldn 0.0.17 event dispatch with metadata-only decision counters.

    Keep the upstream source/decode/identity filters, queue order and exception
    semantics. In particular, invalid advertisements stay silently skipped,
    incompatible identity and parse failures still abort, and disassociation
    remains an event (not a new retry or early assignment-abort policy).
    Tests compare this path with the actual upstream method on the same fakes.
    """
    wlan = _required(ldn_module, "wlan")
    counts = observation.counts
    observation.started = True
    # Authentication has completed. A single current-attempt event may have
    # arrived just before its response. Consume it once through the SAME gates
    # below; do not assign an IP, authorize early, or reuse discovery state.
    pending = getattr(station, "_pending_auth_advertisement", None)
    station._pending_auth_advertisement = None
    try:
        while True:
            if pending is not None:
                received_at, event = pending
                pending = None
                age = _required(ldn_module, "trio").current_time() - received_at
                if not 0 <= age < .7:  # One upstream authentication attempt.
                    log("[diag] LDN auth action handoff expired=true")
                    continue
                counts["auth_handoff"] += 1
                log("[diag] LDN auth action handoff consumed=true")
            else:
                event = await station._interface.next_event()
            counts["events"] += 1
            if isinstance(event, wlan.ActionFrameEvent):
                counts["actions"] += 1
                if event.frame.source != station._network.address:
                    counts["other_source"] += 1
                    continue
                counts["host_actions"] += 1
                # Header-only candidate, not authenticated NetworkInfo. Other
                # host action types can legitimately fail this decoder too.
                action = event.frame.action
                candidate = isinstance(action, (bytes, bytearray)) and action.startswith(
                    b"\x7f\x00\x22\xaa\x04\x00\x01\x01"
                )
                counts["host_candidates"] += int(candidate)
                frame = ldn_module.AdvertisementFrame(
                    station._key_derivation, station._network.protocol
                )
                try:
                    frame.decode(event.frame.action)
                except Exception:
                    counts["decode_rejected"] += 1
                    counts["candidate_decode_rejected"] += int(candidate)
                    continue
                counts["decoded"] += 1
                try:
                    info = ldn_module.NetworkInfo(station._network.protocol)
                    info.address = event.frame.source
                    info.channel = wlan.map_frequency(event.frequency)
                    info.band = ldn_module.ChannelBands[info.channel]
                    info.parse_advertisement(frame)
                except Exception:
                    counts["parse_failed"] += 1
                    raise
                if not station._network.is_same_network(info):
                    counts["identity_rejected"] += 1
                    raise ConnectionError(
                        "Received incompatible advertisement frame from host"
                    )
                counts["queue_started"] += 1
                await station._advertisements.put(info)
                # Consumer scheduling may precede this increment. Completed
                # puts alone must not be treated as a queue-delivery oracle.
                counts["queued"] += 1
            elif isinstance(event, wlan.CustomFrameEvent):
                counts["custom"] += 1
                disconnect_frame = ldn_module.DisconnectFrame()
                disconnect_frame.decode(event.data)
                await station._events.put(
                    ldn_module.DisconnectEvent(disconnect_frame.reason)
                )
            elif isinstance(event, wlan.DisassociationEvent):
                counts["disassociation"] += 1
                await station._events.put(ldn_module.DisconnectEvent(
                    ldn_module.DISCONNECT_CONNECTION_LOST
                ))
            else:
                counts["other"] += 1
    finally:
        # Synchronous flush survives Trio cancellation; no extra task, radio
        # request, await, payload/exception logging or cleanup delay.
        log(observation.summary("processor-exit"))


def _validated_participant_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= _PARTICIPANT_ASSIGNMENT_TIMEOUT_SECONDS
    ):
        raise ValueError("invalid single-interface participant wait timeout")
    return float(value)


async def _initialize_single_interface_station(
    station: object,
    ldn_module: object,
    *,
    timeout: float,
    log: Callable[[str], None],
) -> None:
    """Wait boundedly for the host's post-auth participant advertisement.

    Mirrors ``ldn.STANetwork._initialize_network`` from pinned 0.0.17 with a
    bounded wait and an active-slot check (a disconnected slot retains its MAC).
    ``_process_events`` has already authenticated, decrypted and
    identity-checked every item placed on ``_advertisements``.
    """

    timeout = _validated_participant_timeout(timeout)

    interface = getattr(station, "_interface", None)
    advertisements = getattr(station, "_advertisements", None)
    # Keep object identity, not contents: a replacement queue can strand
    # decoded post-auth advertisements while the consumer waits on the old one.
    assignment_queue = advertisements
    queue_get = getattr(advertisements, "get", None)
    if interface is None or not callable(queue_get):
        raise RuntimeError(
            "ldn 0.0.17 station participant queue is unavailable"
        )
    set_authorized = getattr(interface, "set_authorized", None)
    address = getattr(interface, "address", None)
    if not callable(set_authorized) or not callable(address):
        raise RuntimeError(
            "ldn 0.0.17 station authorization interface is unavailable"
        )

    trio_module = _required(ldn_module, "trio")

    move_on_after = getattr(trio_module, "move_on_after", None)
    if not callable(move_on_after):
        raise RuntimeError("ldn 0.0.17 bounded wait scope is unavailable")

    await set_authorized()
    local_mac = address()
    decoded_advertisements = 0
    # Keep the diagnostic clock compatible with the minimal Trio-like fakes
    # used by callers/tests; real Trio supplies current_time(), while the
    # monotonic fallback preserves elapsed-time semantics.
    current_time = getattr(trio_module, "current_time", time.monotonic)
    wait_started = current_time()
    first_decoded_after = None
    last_decoded_after = None
    max_decoded_gap = 0.0
    assigned_network = None
    assigned_index = None
    log(
        "[diag] LDN authentication accepted; waiting up to "
        f"{timeout:.1f} s for participant assignment"
    )
    with move_on_after(timeout):
        while True:
            network = await queue_get()
            decoded_at = current_time()
            decoded_after = max(0.0, decoded_at - wait_started)
            if first_decoded_after is None:
                first_decoded_after = decoded_after
            if last_decoded_after is not None:
                max_decoded_gap = max(max_decoded_gap, decoded_after - last_decoded_after)
            last_decoded_after = decoded_after
            decoded_advertisements += 1
            participants = getattr(network, "participants", ())
            for index, participant in enumerate(participants):
                if (getattr(participant, "mac_address", None) == local_mac
                        and getattr(participant, "connected", False)):
                    assigned_network = network
                    assigned_index = index
                    break
            if assigned_network is not None:
                break

    if assigned_network is None or assigned_index is None:
        log(
            "[diag] participant assignment wait expired after receiving "
            f"{decoded_advertisements} decoded identity-matching "
            "host advertisement(s) "
            f"first_after_s={-1.0 if first_decoded_after is None else first_decoded_after:.3f} "
            f"last_after_s={-1.0 if last_decoded_after is None else last_decoded_after:.3f} "
            f"max_gap_s={max_decoded_gap:.3f}"
        )
        # Keep receive-path evidence adjacent to the timeout. The processor
        # can be cancelled immediately after this exception, so its finally
        # log is not a reliable witness for the intermittent case.
        observation = getattr(station, "_station_event_observation", None)
        counts = getattr(observation, "counts", None)
        if isinstance(counts, dict):
            log(
                "[diag] LDN assignment RX evidence "
                + " ".join(
                    f"{name}={counts.get(name, 0)}"
                    for name in (
                        "events", "auth_handoff", "actions", "host_actions", "host_candidates",
                        "decode_rejected", "decoded", "parse_failed",
                        "identity_rejected", "queued", "custom", "disassociation",
                    )
                )
            )
        # A decoded advertisement can be present but never reach the
        # assignment consumer if cancellation/replacement swaps the queue.
        # Record only depth: this tests backlog-vs-RF loss without payloads.
        log(
            "[diag] LDN assignment queue snapshot "
            f"depth={_advertisement_queue_depth(getattr(station, '_advertisements', None))}"
        )
        queue_continuity = (
            getattr(station, "_advertisements", None) is assignment_queue
        )
        # A queue swap is the concrete failure mode this wait cannot recover
        # from: the producer may be putting into the new queue while this
        # consumer remains blocked on the old one. Keep it explicit rather
        # than making a timeout look like RF silence.
        log(
            "[diag] LDN assignment queue identity "
            f"continuity={str(queue_continuity).lower()} "
            f"replaced={str(not queue_continuity).lower()}"
        )
        raise ConnectionError(
            "Failed to obtain IP address after joining network (timeout)"
        )

    log(
        "[diag] participant assignment received after "
        f"{decoded_advertisements} decoded identity-matching "
        "host advertisement(s) "
        f"first_after_s={first_decoded_after:.3f} "
        f"last_after_s={last_decoded_after:.3f} "
        f"max_gap_s={max_decoded_gap:.3f}"
    )
    setattr(station, "_network", assigned_network)
    host = assigned_network.participants[0]
    network_id = int(host.ip_address.split(".")[2])
    setattr(station, "_network_id", network_id)
    setattr(station, "_participant_id", assigned_index)

    add_address = getattr(interface, "add_address", None)
    add_neighbor = getattr(interface, "add_neighbor", None)
    if not callable(add_address) or not callable(add_neighbor):
        raise RuntimeError("ldn 0.0.17 station address hooks are unavailable")
    participant = assigned_network.participants[assigned_index]
    broadcast_address = f"169.254.{network_id}.255"
    await add_address(participant.ip_address, broadcast_address)
    for peer in assigned_network.participants:
        if getattr(peer, "connected", False):
            await add_neighbor(peer.ip_address, peer.mac_address)


@asynccontextmanager
async def connect(
    ldn_module: object,
    param: object,
    *,
    ifname: str,
    log=print,
    participant_timeout: float = _PARTICIPANT_ASSIGNMENT_TIMEOUT_SECONDS,
    bss_seed=None,
) -> AsyncIterator[object]:
    """Equivalent to ldn 0.0.17 ``connect``, using an existing interface."""

    participant_timeout = _validated_participant_timeout(participant_timeout)
    param = copy.copy(param)
    if param.client_random is None:
        param.client_random = secrets.token_bytes(16)
    param.check()

    network = param.network
    key_derivation_type = _required(ldn_module, "KeyDerivation")
    key_derivation = key_derivation_type(
        param.keys,
        network.protocol,
        override_advertise_key=param.override_advertise_key,
        override_data_key=param.override_data_key,
        override_challenge_key=param.override_challenge_key,
    )
    wlan_key = None
    if network.security_mode == ldn_module.SECURITY_MODE_PROD:
        wlan_key = key_derivation.derive_data_key(
            network.server_random, param.password
        )

    network_address = getattr(network, "address", None)
    if isinstance(network_address, (bytes, bytearray, memoryview)):
        bssid = bytes(network_address)
    else:
        encode_address = getattr(network_address, "encode", None)
        bssid = encode_address() if callable(encode_address) else None
    if not isinstance(bssid, bytes) or len(bssid) != 6:
        bssid = None

    sta_network_type = _required(ldn_module, "STANetwork")

    class SingleInterfaceSTANetwork(sta_network_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._station_event_observation = _StationEventObservation()

        async def _authenticate(self) -> None:
            await _authenticate_single_interface_station(
                self, ldn_module, super()._authenticate, log=log
            )

        async def _process_events(self) -> None:
            await _process_single_interface_events(
                self, ldn_module, self._station_event_observation, log=log
            )

        async def _initialize_network(self) -> None:
            try:
                await _initialize_single_interface_station(
                    self,
                    ldn_module,
                    timeout=participant_timeout,
                    log=log,
                )
            finally:
                log(self._station_event_observation.summary("assignment-exit"))

    async with _factory(ldn_module, ifname, log) as factory:
        if bss_seed is not None:
            factory.bss_seed = bss_seed
        async with factory.connect_network(
            param.phyname,
            ifname,
            network.ssid.hex(),
            network.channel,
            wlan_key,
            bssid,
        ) as interface:
            station = SingleInterfaceSTANetwork(
                interface, param, key_derivation
            )
            async with station.start():
                yield station


__all__ = [
    "ExistingInterfaceFactory",
    "NoMonitorFramesError",
    "UnsupportedLDNControlPortDriverError",
    "connect",
    "scan",
]
