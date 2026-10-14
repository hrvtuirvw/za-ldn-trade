"""Z-A LDN/Pia datagram transport.

LiveTransport joins LDN and uses bound UDP TX plus AF_PACKET RX. Public
adapters assemble the complete title/room settings in zasim.transport_setup.
Construction alone does not read keys or operate a device. Direct operation
requires Linux, optional live dependencies and suitable radio privileges.
"""

import errno
import hashlib
import ipaddress
import json
import os
import socket
import stat
import struct
import subprocess
import threading
import time
import traceback

from zasim.profile import ZA_BOX_TRADE_SCENE_ID, ZA_PROFILE
from zasim.transport_setup import _application_bytes

from zasim.ldn.ldn_name import (
    MAX_LDN_NICKNAME_BYTES as MAX_LDN_NICKNAME_BYTES,
    MAX_LDN_NICKNAME_CHARACTERS as MAX_LDN_NICKNAME_CHARACTERS,
    validate_ldn_nickname as validate_ldn_nickname,
)

ETH_P_IP = 0x0800
ETH_P_ALL = 0x0003
PROTO_UDP = 17
PIA_PORT = 12345
# A single adjacent port reserved for the handshake-only endpoint-state oracle.
# Normal Pia traffic always uses PIA_PORT.  Keeping this value explicit makes
# it impossible for the diagnostic path to become a general alternate-port
# transport by accident.
PIA_DIAGNOSTIC_SOURCE_PORT = 12346
DEFAULT_MAX_LIVE_DATAGRAM_BYTES = 1472
MAX_LIVE_RECV_FRAMES = 256
MAX_PENDING_EGRESS_PROBES = 512
MAX_EGRESS_DETAIL_LOGS = 64
# Linux exposes these from <linux/if_packet.h>/<asm-generic/socket.h>.  Keep
# numeric fallbacks so the socket code can be unit-tested from Windows without
# pretending that direct LDN itself is portable to Windows.
PACKET_OUTGOING = getattr(socket, "PACKET_OUTGOING", 4)
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
# linux/usbdevice_fs.h: USBDEVFS_RESET = _IO('U', 20).  This request has no
# architecture-dependent size payload, so the Linux ioctl number is 0x5514.
_USBDEVFS_RESET = (ord("U") << 8) | 20


def _cancel_requested(cancel_event):
    if cancel_event is None:
        return False
    is_set = getattr(cancel_event, "is_set", None)
    if not callable(is_set):
        raise TypeError("cancel_event must provide an is_set() method or be None")
    return bool(is_set())


def _flatten_exc(e, depth=0):
    """Recursively flatten a (Base)ExceptionGroup - which is how trio reports failures from inside
    its nursery ('Exceptions from Trio nursery (N sub-exceptions)') - into the LEAF exceptions, so
    the real cause (e.g. a netlink/nl80211 EBUSY, an association timeout) is visible instead of the
    opaque group wrapper. Returns a list of (depth, exception) leaves."""
    subs = getattr(e, "exceptions", None)           # ExceptionGroup / trio.MultiError
    if subs:
        out = []
        for sub in subs:
            out.extend(_flatten_exc(sub, depth + 1))
        return out
    return [(depth, e)]


def _format_join_error(e):
    """Human-readable, fully-unwrapped description of an LDN-join failure, with the leaf exceptions'
    types, messages, and tracebacks (the opaque trio ExceptionGroup hides all of these)."""
    leaves = _flatten_exc(e)
    if len(leaves) == 1 and leaves[0][1] is e:      # not a group: report it directly
        leaf = e
        body = "".join(traceback.format_exception(type(leaf), leaf, leaf.__traceback__))
        return f"{type(leaf).__name__}: {leaf}\n{body}"
    parts = [f"{type(e).__name__} with {len(leaves)} underlying error(s):"]
    for i, (_d, leaf) in enumerate(leaves, 1):
        body = "".join(traceback.format_exception(type(leaf), leaf, leaf.__traceback__))
        parts.append(f"  [{i}] {type(leaf).__name__}: {leaf}\n{body}")
    return "\n".join(parts)

# LDN virtual interfaces to clear off the radio (ported from the bridge tooling).
LDN_VIFS = {"ldn", "ldn-mon", "ldn-tap", "ldnclient"}
SINGLE_INTERFACE_DRIVERS = {
    "88xxau",
    "8812au",
    "rtl8812au",
    "rtl88xxau",
}
# Automatic USB recovery is intentionally narrower than generic USB identity
# validation.  Only radios whose complete RX-stall/reset behavior was observed
# on hardware are reset without an additional user action.
SILENT_USB_RECOVERY_DEVICES = frozenset({
    ("0bda", "8812", "rtw88_8812au"),
})


# --- radio / interface cleanup (the library needs the radio free of stale vifs) -------------
def _run(cmd):
    return subprocess.run(
        cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _iw_del(iface):
    _run(["iw", "dev", iface, "del"])


def _sysctl(key, val):
    return _run(["sysctl", "-wq", f"{key}={val}"])


def _read_sysctl(key):
    try:
        return subprocess.check_output(
            ["sysctl", "-n", key], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def _interface_state(iface):
    """Return the managed/admin-up state needed for best-effort restoration."""
    managed = None
    admin_up = None
    for field in ("GENERAL.NM-MANAGED", "GENERAL.MANAGED"):
        try:
            value = subprocess.check_output(
                ["nmcli", "-g", field, "device", "show", iface],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip().lower()
            if value in {"yes", "no"}:
                managed = value == "yes"
                break
        except Exception:
            pass
    try:
        value = subprocess.check_output(
            ["ip", "-o", "link", "show", "dev", iface],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        flags = value.split("<", 1)[1].split(">", 1)[0].split(",")
        admin_up = "UP" in flags
    except Exception:
        pass
    return managed, admin_up


def restore_network_state(
    interface_state,
    sysctl_state,
    log=print,
    ephemeral_ifaces=None,
):
    """Undo host-network changes and retain every unverified restoration lease."""
    failures = []
    ephemeral_ifaces = (
        LDN_VIFS | set(ephemeral_ifaces or ())
    ) - {"all", "default"}
    for key, value in reversed(tuple(sysctl_state.items())):
        # Match the exact interface prefix instead of splitting on dots: dots
        # are valid inside Linux netdev names.  Longest-first avoids treating
        # ``foo.bar`` as ``foo`` when both are leased names.
        sysctl_iface = next(
            (
                iface
                for iface in sorted(ephemeral_ifaces, key=len, reverse=True)
                if key.startswith(f"net.ipv4.conf.{iface}.")
                or key.startswith(f"net.ipv6.conf.{iface}.")
            ),
            None,
        )
        is_removed_ldn_vif = (
            sysctl_iface is not None and not _iface_exists(sysctl_iface)
        )
        # A normal VIF teardown removes its per-interface sysctl directory.
        # There is no state left to restore in that case.  Global/default and
        # reused physical-interface sysctls remain strict.
        if is_removed_ldn_vif:
            sysctl_state.pop(key, None)
            continue
        if value is None:
            failures.append(
                f"original sysctl {key} is unknown; restoration cannot be verified"
            )
            continue
        try:
            result = _sysctl(key, value)
        except BaseException as exc:
            failures.append(f"could not restore sysctl {key}: {exc}")
            continue
        if getattr(result, "returncode", 0) != 0:
            failures.append(f"could not restore sysctl {key}")
            continue
        actual_value = _read_sysctl(key)
        if actual_value != value:
            failures.append(
                f"sysctl {key} is {actual_value!r}, expected {value!r} after cleanup"
            )
            continue
        sysctl_state.pop(key, None)
    for iface, (managed, admin_up) in reversed(tuple(interface_state.items())):
        command_failed = False
        if managed is not None:
            try:
                result = _run([
                    "nmcli", "device", "set", iface, "managed",
                    "yes" if managed else "no",
                ])
            except BaseException as exc:
                command_failed = True
                failures.append(
                    f"could not restore NetworkManager managed state for {iface}: {exc}"
                )
            else:
                if getattr(result, "returncode", 0) != 0:
                    command_failed = True
                    failures.append(
                        f"could not restore NetworkManager managed state for {iface}"
                    )
        if admin_up is not None:
            try:
                result = _run([
                    "ip", "link", "set", iface, "up" if admin_up else "down"
                ])
            except BaseException as exc:
                command_failed = True
                failures.append(
                    f"could not restore administrative link state for {iface}: {exc}"
                )
            else:
                if getattr(result, "returncode", 0) != 0:
                    command_failed = True
                    failures.append(
                        f"could not restore administrative link state for {iface}"
                    )
        actual_managed, actual_up = _interface_state(iface)
        verified = True
        if managed is not None and actual_managed != managed:
            verified = False
            failures.append(
                f"{iface} NetworkManager managed state was not restored"
            )
        if admin_up is not None and actual_up != admin_up:
            verified = False
            failures.append(f"{iface} administrative link state was not restored")
        if not command_failed and verified:
            log(f"[live] restored host interface state for {iface}")
            interface_state.pop(iface, None)
    if failures:
        raise RuntimeError("; ".join(dict.fromkeys(failures)))


def list_phy_ifaces():
    """Map phyName -> netdev names, or None when ``iw dev`` cannot be read."""
    mapping, current = {}, None
    try:
        out = subprocess.check_output(["iw", "dev"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    for raw in out.splitlines():
        s = raw.strip()
        if s.startswith("phy#"):
            current = "phy" + s[4:]
            mapping[current] = []
        elif s.startswith("Interface ") and current is not None:
            mapping[current].append(s.split()[1])
    return mapping


def _phy_for_interface(iface, mapping=None):
    """Return the one PHY containing ``iface``, or None if not provable."""
    if mapping is None:
        mapping = list_phy_ifaces()
    if mapping is None:
        return None
    owners = [phy for phy, ifaces in mapping.items() if iface in ifaces]
    return owners[0] if len(owners) == 1 else None


def _resolve_reenumerated_single_interface_phy(phyname, ifname, log=print):
    """Follow a USB radio whose kernel PHY index changed after re-enumeration.

    A stale PHY is accepted only when that old name no longer exists and the
    explicitly selected physical netdev belongs to exactly one current PHY. If
    both PHYs exist, the arguments are inconsistent and we fail closed rather
    than silently selecting a different radio.
    """
    mapping = list_phy_ifaces()
    if mapping is None:
        raise RuntimeError(
            "could not inspect wireless interfaces with 'iw dev'; refusing "
            "stale-PHY recovery"
        )
    actual = _phy_for_interface(ifname, mapping)
    if actual is None:
        raise RuntimeError(
            f"could not prove the current wireless PHY for interface {ifname}"
        )
    if actual == phyname:
        return phyname
    if phyname in mapping:
        raise RuntimeError(
            f"interface {ifname} belongs to {actual}, not requested {phyname}"
        )
    log(
        f"[live] {ifname} was re-enumerated from stale {phyname} to {actual}; "
        "using the current PHY"
    )
    return actual


def interface_driver(iface):
    """Return a normalized Linux driver/module name for a netdev, if known."""
    module_link = f"/sys/class/net/{iface}/device/driver/module"
    try:
        name = os.path.basename(os.path.realpath(module_link)).strip().lower()
        if name and name != "module":
            return name
    except OSError:
        pass
    try:
        with open(f"/sys/class/net/{iface}/device/uevent", errors="replace") as f:
            for line in f:
                if line.startswith("DRIVER="):
                    return line.partition("=")[2].strip().lower() or None
    except OSError:
        pass
    return None


def interface_mac_address(iface):
    """Return one normalized unicast netdev MAC address, or None."""
    try:
        with open(f"/sys/class/net/{iface}/address", encoding="ascii") as source:
            value = source.read().strip().lower()
        raw = bytes.fromhex(value.replace(":", ""))
    except (OSError, ValueError):
        return None
    if len(raw) != 6 or raw == b"\0" * 6 or raw[0] & 1:
        return None
    return raw.hex(":")


def wireless_interface_type(iface):
    """Read the current iw interface type (managed/monitor/etc.)."""
    try:
        output = subprocess.check_output(
            ["iw", "dev", iface, "info"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    for raw in output.splitlines():
        fields = raw.strip().split()
        if len(fields) == 2 and fields[0] == "type":
            return fields[1]
    return None


def interface_ipv4_addresses(iface):
    """Return configured IPv4 CIDRs, or None when state cannot be inspected."""
    try:
        output = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "dev", iface],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    addresses = set()
    for raw in output.splitlines():
        fields = raw.split()
        if "inet" in fields:
            index = fields.index("inet") + 1
            if index < len(fields):
                addresses.add(fields[index])
    return addresses


def interface_neighbor_ips(iface):
    """Return IPv4 neighbor keys, or None when state cannot be inspected."""
    try:
        output = subprocess.check_output(
            ["ip", "-4", "neigh", "show", "dev", iface],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    return {line.split()[0] for line in output.splitlines() if line.split()}


_IPV4_NEIGHBOR_STATES = {
    "PERMANENT",
    "NOARP",
    "REACHABLE",
    "STALE",
    "NONE",
    "INCOMPLETE",
    "DELAY",
    "PROBE",
    "FAILED",
}


def _parse_mac_address(value, *, label):
    """Return a six-byte colon MAC, rejecting ambiguous iproute2 output."""
    if not isinstance(value, str):
        raise RuntimeError(f"{label} is not a MAC address")
    fields = value.split(":")
    if len(fields) != 6 or any(len(field) != 2 for field in fields):
        raise RuntimeError(f"{label} is not a six-byte MAC address")
    try:
        result = bytes(int(field, 16) for field in fields)
    except ValueError as exc:
        raise RuntimeError(f"{label} is not a hexadecimal MAC address") from exc
    return result


def interface_ipv4_neighbor_state(iface, destination):
    """Snapshot one exact IPv4 neighbor entry, or ``None`` when absent.

    Unlike :func:`interface_neighbor_ips`, inspection errors are exceptions:
    callers use this immediately before overwriting a neighbor and therefore
    cannot safely confuse an unreadable table with an empty one.
    """
    try:
        output = subprocess.check_output(
            [
                "ip", "-j", "-4", "neigh", "show", "to", destination,
                "dev", iface,
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        records = json.loads(output)
    except Exception as exc:
        raise RuntimeError(
            f"could not inspect IPv4 neighbor {destination} on {iface}"
        ) from exc
    if not isinstance(records, list):
        raise RuntimeError(
            f"unexpected IPv4 neighbor response for {destination} on {iface}"
        )
    if not records:
        return None
    if len(records) != 1 or not isinstance(records[0], dict):
        raise RuntimeError(
            f"IPv4 neighbor {destination} on {iface} was not unique"
        )
    record = records[0]
    # Some iproute2 releases omit ``dev`` from JSON when it was supplied as a
    # selector.  The argv query above is already scoped to this exact netdev;
    # accept omission, but never accept a conflicting device when one is
    # rendered.
    returned_iface = record.get("dev")
    if (
        record.get("dst") != destination
        or (returned_iface is not None and returned_iface != iface)
    ):
        raise RuntimeError(
            f"IPv4 neighbor query returned a mismatched entry for "
            f"{destination} on {iface}"
        )
    # These attributes affect forwarding semantics but cannot be faithfully
    # reconstructed by the narrow static-neighbor lease below.  Refuse the
    # mutation rather than silently discarding them.
    if (
        record.get("flags")
        or record.get("proxy")
        or record.get("router")
        or record.get("protocol")
        or record.get("master")
    ):
        raise RuntimeError(
            f"IPv4 neighbor {destination} on {iface} has unsupported flags"
        )
    raw_states = record.get("state")
    if isinstance(raw_states, str):
        raw_states = [raw_states]
    if (
        not isinstance(raw_states, list)
        or len(raw_states) != 1
        or not isinstance(raw_states[0], str)
    ):
        raise RuntimeError(
            f"IPv4 neighbor {destination} on {iface} has an ambiguous state"
        )
    state = raw_states[0].upper()
    if state not in _IPV4_NEIGHBOR_STATES:
        raise RuntimeError(
            f"IPv4 neighbor {destination} on {iface} has unknown state {state!r}"
        )
    lladdr = record.get("lladdr")
    mac = (
        _parse_mac_address(lladdr, label="neighbor lladdr")
        if lladdr is not None else None
    )
    if mac is None and state not in {"INCOMPLETE", "FAILED"}:
        raise RuntimeError(
            f"IPv4 neighbor {destination} on {iface} has no restorable lladdr"
        )
    return {
        "destination": destination,
        "iface": iface,
        "lladdr": mac,
        "state": state,
    }


def verify_direct_ipv4_route(iface, destination, source):
    """Fail unless Linux selects the joined interface/source with no gateway."""
    try:
        output = subprocess.check_output(
            [
                "ip", "-4", "route", "get", destination,
                "from", source, "oif", iface,
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        raise RuntimeError(
            f"could not resolve the direct LDN route to {destination} on {iface}"
        ) from exc
    first_line = next(
        (line.strip() for line in output.splitlines() if line.strip()), ""
    )
    fields = first_line.split()
    rejected_kinds = {
        "blackhole", "unreachable", "prohibit", "throw", "local",
        "broadcast", "multicast", "nat",
    }

    def single_value(label):
        positions = [index for index, field in enumerate(fields) if field == label]
        if len(positions) != 1 or positions[0] + 1 >= len(fields):
            return None
        return fields[positions[0] + 1]

    reported_source = single_value("src")
    if reported_source is None:
        # With an explicit ``from`` selector, iproute2 commonly echoes
        # ``from A.B.C.D`` instead of rendering a separate ``src`` field.
        reported_source = single_value("from")

    if (
        not fields
        or fields[0] in rejected_kinds
        or fields[0] != destination
        or "via" in fields
        or single_value("dev") != iface
        or reported_source != source
    ):
        raise RuntimeError(
            f"kernel route to {destination} does not use {source} directly on {iface}"
        )
    return True


def interface_local_broadcast_routes(iface):
    """Return local-table broadcast destinations associated with a netdev."""
    try:
        output = subprocess.check_output(
            [
                "ip", "-4", "route", "show", "table", "local",
                "type", "broadcast", "dev", iface,
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    destinations = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "broadcast":
            destinations.add(fields[1])
    return destinations


def interface_active_connection(iface):
    """Return active UUID, False when disconnected, or None on inspection error."""
    try:
        value = subprocess.check_output(
            ["nmcli", "-g", "GENERAL.CON-UUID", "device", "show", iface],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None
    return value if value and value != "--" else False


def interface_autoconnect(iface):
    """Return NetworkManager's per-device autoconnect policy, or None."""
    try:
        value = subprocess.check_output(
            ["nmcli", "-g", "GENERAL.AUTOCONNECT", "device", "show", iface],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip().lower()
    except Exception:
        return None
    if value in {"yes", "no"}:
        return value == "yes"
    return None


def interface_has_default_route(iface):
    """Return whether either IP family has a default route, fail closed."""
    outputs = []
    for family in ("-4", "-6"):
        try:
            outputs.append(subprocess.check_output(
                ["ip", family, "route", "show", "default", "dev", iface],
                text=True,
                stderr=subprocess.DEVNULL,
            ))
        except Exception:
            # Both families must be inspectable.  Returning an IPv4 result
            # after an IPv6 query failure could take down the user's only path.
            return None
    return any(output.strip() for output in outputs)


def interface_has_ipv6_state(iface):
    """Return whether disabling IPv6 could destroy existing interface state."""
    commands = (
        ["ip", "-6", "-o", "addr", "show", "dev", iface],
        ["ip", "-6", "route", "show", "dev", iface],
        ["ip", "-6", "neigh", "show", "dev", iface],
    )
    outputs = []
    for command in commands:
        try:
            outputs.append(subprocess.check_output(
                command,
                text=True,
                stderr=subprocess.DEVNULL,
            ))
        except Exception:
            return None
    return any(output.strip() for output in outputs)


def interface_carries_ssh(iface):
    """Best-effort guard against taking down the caller's SSH transport."""
    connection = os.environ.get("SSH_CONNECTION", "").split()
    if not connection:
        return False
    try:
        output = subprocess.check_output(
            ["ip", "-4", "route", "get", connection[0]],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    fields = output.split()
    if "dev" not in fields:
        return False
    index = fields.index("dev") + 1
    return index < len(fields) and fields[index] == iface


def restore_active_connections(connections, log=print):
    """Reconnect NetworkManager profiles detached solely for the LDN run."""
    failures = []
    for iface, uuid in reversed(tuple(connections.items())):
        result = _run([
            "nmcli", "connection", "up", "uuid", uuid, "ifname", iface,
        ])
        if getattr(result, "returncode", 0) != 0:
            failures.append(f"could not reconnect NetworkManager profile on {iface}")
            continue
        log(f"[live] restored NetworkManager connection on {iface}")
    if failures:
        raise RuntimeError("; ".join(failures))


def phy_supports_new_interface(phyname):
    """Return nl80211 NEW_INTERFACE command support, or None if undetectable."""
    try:
        output = subprocess.check_output(
            ["iw", "phy", phyname, "info"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    in_commands = False
    commands = set()
    for raw in output.splitlines():
        stripped = raw.strip()
        if stripped == "Supported commands:":
            in_commands = True
            continue
        if not in_commands:
            continue
        if stripped.startswith("*"):
            commands.add(stripped[1:].strip())
            continue
        if stripped and not raw[:1].isspace():
            break
    return "new_interface" in commands


def _read_sysfs_value(path):
    try:
        with open(path, encoding="ascii", errors="strict") as source:
            value = source.read().strip()
    except (OSError, UnicodeError):
        return None
    if not value or "\n" in value or "\r" in value:
        return None
    return value


def _usb_wireless_identity(iface):
    """Return a fail-closed identity for a one-netdev USB WLAN interface."""
    interface_path = os.path.realpath(f"/sys/class/net/{iface}/device")
    if not os.path.isdir(interface_path):
        return None
    device_path = os.path.dirname(interface_path)
    vendor = _read_sysfs_value(os.path.join(device_path, "idVendor"))
    product = _read_sysfs_value(os.path.join(device_path, "idProduct"))
    busnum = _read_sysfs_value(os.path.join(device_path, "busnum"))
    devnum = _read_sysfs_value(os.path.join(device_path, "devnum"))
    device_id = _read_sysfs_value(os.path.join(device_path, "dev"))
    if any(
        value is None
        for value in (vendor, product, busnum, devnum, device_id)
    ):
        return None
    try:
        bus_number = int(busnum, 10)
        device_number = int(devnum, 10)
        major_text, minor_text = device_id.split(":", 1)
        device_major = int(major_text, 10)
        device_minor = int(minor_text, 10)
    except (AttributeError, ValueError):
        return None
    if (
        not 0 <= bus_number <= 999
        or not 0 <= device_number <= 999
        or device_major < 0
        or device_minor < 0
    ):
        return None
    driver_path = os.path.realpath(os.path.join(interface_path, "driver"))
    driver = os.path.basename(driver_path).strip().lower()
    if not driver or driver == "driver":
        return None
    try:
        netdevs = sorted(os.listdir(os.path.join(interface_path, "net")))
    except OSError:
        return None
    if netdevs != [iface]:
        return None
    return {
        "iface": iface,
        "interface_path": interface_path,
        "device_path": device_path,
        "port": os.path.basename(device_path),
        "vendor": vendor.lower(),
        "product": product.lower(),
        "serial": _read_sysfs_value(os.path.join(device_path, "serial")) or "",
        "driver": driver,
        "busnum": bus_number,
        "devnum": device_number,
        "device_major": device_major,
        "device_minor": device_minor,
    }


def _same_usb_wireless_device(first, second):
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    stable_keys = (
        "iface",
        "interface_path",
        "device_path",
        "port",
        "vendor",
        "product",
        "serial",
        "driver",
    )
    return all(first.get(key) == second.get(key) for key in stable_keys)


def reset_silent_usb_radio(
    iface,
    expected_mac,
    log=print,
    *,
    wait_timeout=20.0,
):
    """Reset one proven-dedicated USB WLAN device and return its current PHY.

    The caller must already own the single-interface radio lease. This helper
    revalidates the exact USB port, VID/PID, serial, driver and sole netdev both
    before and after ``usbreset``. It leaves the interface managed-type,
    NetworkManager-unmanaged and administratively down for the next LDN retry.
    """
    if (
        isinstance(wait_timeout, bool)
        or not isinstance(wait_timeout, (int, float))
        or not 0 < float(wait_timeout) <= 60
    ):
        raise ValueError(
            "USB radio recovery timeout must be greater than 0 and at most "
            "60 seconds"
        )
    expected_mac = str(expected_mac).strip().lower()
    try:
        expected_raw = bytes.fromhex(expected_mac.replace(":", ""))
    except ValueError as exc:
        raise ValueError("invalid expected USB radio MAC address") from exc
    if len(expected_raw) != 6 or expected_raw == b"\0" * 6 or expected_raw[0] & 1:
        raise ValueError("invalid expected USB radio MAC address")
    expected_mac = expected_raw.hex(":")

    identity = _usb_wireless_identity(iface)
    if identity is None:
        raise RuntimeError(
            f"{iface} is not a uniquely identifiable one-netdev USB radio"
        )
    active_connection = interface_active_connection(iface)
    route_state = interface_has_default_route(iface)
    ssh_state = interface_carries_ssh(iface)
    ipv6_state = interface_has_ipv6_state(iface)
    addresses = interface_ipv4_addresses(iface)
    neighbors = interface_neighbor_ips(iface)
    broadcasts = interface_local_broadcast_routes(iface)
    if any(
        value is None
        for value in (
            active_connection,
            route_state,
            ssh_state,
            ipv6_state,
            addresses,
            neighbors,
            broadcasts,
        )
    ):
        raise RuntimeError(
            f"could not prove that {iface} is idle before USB radio recovery"
        )
    if (
        active_connection is not False
        or route_state
        or ssh_state
        or ipv6_state
        or addresses
        or neighbors
        or broadcasts
    ):
        raise RuntimeError(
            f"refusing USB radio recovery while {iface} carries host network state"
        )
    if wireless_interface_type(iface) != "managed":
        raise RuntimeError(
            f"refusing USB radio recovery while {iface} is not in managed mode"
        )

    def checked(command, label):
        result = _run(command)
        if getattr(result, "returncode", 0) != 0:
            raise RuntimeError(label)

    checked(
        ["nmcli", "device", "set", iface, "managed", "no"],
        f"could not detach NetworkManager from {iface} before USB recovery",
    )
    checked(
        ["ip", "link", "set", iface, "down"],
        f"could not bring {iface} down before USB recovery",
    )
    # Re-read immediately before selecting the bus/device pair so an unplug or
    # replacement cannot redirect the reset to another USB device.
    current = _usb_wireless_identity(iface)
    if not _same_usb_wireless_device(identity, current):
        raise RuntimeError("USB radio identity changed before reset")
    device_node = (
        f"/dev/bus/usb/{current['busnum']:03d}/{current['devnum']:03d}"
    )
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        import fcntl

        descriptor = os.open(device_node, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISCHR(metadata.st_mode)
            or os.major(metadata.st_rdev) != current["device_major"]
            or os.minor(metadata.st_rdev) != current["device_minor"]
        ):
            raise RuntimeError(
                "USB radio device node does not match its sysfs identity"
            )
        # Revalidate after opening. Even if the node is unplugged immediately
        # afterwards, the descriptor cannot be redirected to another device.
        opened_identity = _usb_wireless_identity(iface)
        if (
            not _same_usb_wireless_device(current, opened_identity)
            or any(
                opened_identity.get(key) != current[key]
                for key in (
                    "busnum",
                    "devnum",
                    "device_major",
                    "device_minor",
                )
            )
        ):
            raise RuntimeError("USB radio identity changed before reset ioctl")
        fcntl.ioctl(descriptor, _USBDEVFS_RESET, 0)
    except ImportError as exc:
        raise RuntimeError("USB radio reset ioctl is unavailable") from exc
    except OSError as exc:
        raise RuntimeError("USB radio reset ioctl failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    time.sleep(0.2)
    deadline = time.monotonic() + float(wait_timeout)
    returned_identity = None
    current_phy = None
    while time.monotonic() < deadline:
        candidate = _usb_wireless_identity(iface)
        mapping = list_phy_ifaces()
        candidate_phy = _phy_for_interface(iface, mapping)
        if _same_usb_wireless_device(identity, candidate) and candidate_phy is not None:
            returned_identity = candidate
            current_phy = candidate_phy
            break
        time.sleep(0.1)
    if returned_identity is None or current_phy is None:
        raise RuntimeError("USB radio did not return with its audited identity")

    checked(
        ["nmcli", "device", "set", iface, "managed", "no"],
        f"could not detach the returned USB radio {iface} from NetworkManager",
    )
    checked(
        ["ip", "link", "set", iface, "down"],
        f"could not bring the returned USB radio {iface} down",
    )
    checked(
        ["iw", "dev", iface, "set", "type", "managed"],
        f"could not restore managed mode on returned USB radio {iface}",
    )
    checked(
        ["ip", "link", "set", iface, "address", expected_mac],
        f"could not restore the leased MAC address on returned USB radio {iface}",
    )
    if wireless_interface_type(iface) != "managed":
        raise RuntimeError("returned USB radio did not retain managed mode")
    if interface_mac_address(iface) != expected_mac:
        raise RuntimeError("returned USB radio did not retain the leased MAC address")
    final_identity = _usb_wireless_identity(iface)
    if not _same_usb_wireless_device(identity, final_identity):
        raise RuntimeError("USB radio identity changed during reset recovery")
    final_phy = _phy_for_interface(iface)
    if final_phy != current_phy:
        raise RuntimeError("USB radio PHY changed during reset recovery")
    returned_state = (
        interface_active_connection(iface),
        interface_has_default_route(iface),
        interface_carries_ssh(iface),
        interface_has_ipv6_state(iface),
        interface_ipv4_addresses(iface),
        interface_neighbor_ips(iface),
        interface_local_broadcast_routes(iface),
    )
    if any(value is None for value in returned_state):
        raise RuntimeError(
            "could not verify the returned USB radio's host network state"
        )
    returned_connection, *returned_network_state = returned_state
    if returned_connection is not False or any(
        bool(value) for value in returned_network_state
    ):
        raise RuntimeError(
            "returned USB radio acquired unexpected host network state"
        )
    log(
        f"[live] reset silent USB radio {iface}; current PHY is {current_phy}"
    )
    return current_phy


def choose_single_interface(phyname, requested_ifname, policy=None, log=print):
    """Select an existing netdev for no-VIF operation.

    ``policy`` is ``True`` to force reuse, ``False`` to retain ldn's virtual
    interface path, and ``None`` to automatically reuse known 88XXau-family
    devices.  Auto mode never probes NEW_INTERFACE on a detected 88XXau radio.
    """
    if policy is not None and not isinstance(policy, bool):
        raise TypeError("single_interface must be a bool or None")
    if policy is False:
        return None
    phy_ifaces = list_phy_ifaces()
    if phy_ifaces is None:
        raise RuntimeError(
            "could not inspect wireless interfaces with 'iw dev'; refusing "
            "radio mutation"
        )
    candidates = [
        iface for iface in phy_ifaces.get(phyname, [])
        if iface not in LDN_VIFS
    ]
    if policy is None:
        compatible = [
            iface for iface in candidates
            if interface_driver(iface) in SINGLE_INTERFACE_DRIVERS
        ]
        new_interface_support = phy_supports_new_interface(phyname)
        no_new_interface = new_interface_support is False
        if no_new_interface and len(candidates) != 1:
            names = ", ".join(sorted(candidates)) or "none"
            raise RuntimeError(
                f"{phyname} cannot create virtual interfaces; single-interface "
                "reuse requires exactly one non-LDN netdev, found "
                f"{len(candidates)} ({names})"
            )
        if not compatible and no_new_interface:
            compatible = list(candidates)
        if not compatible and new_interface_support is None:
            raise RuntimeError(
                f"could not determine whether {phyname} supports virtual "
                "interfaces; refusing automatic radio mutation"
            )
        if not compatible:
            return None
        if len(candidates) != 1:
            names = ", ".join(sorted(candidates))
            raise RuntimeError(
                "single-interface reuse requires exactly one non-LDN netdev on "
                f"{phyname}; found {len(candidates)} ({names})"
            )
        if requested_ifname in compatible:
            chosen = requested_ifname
        elif len(compatible) == 1:
            chosen = compatible[0]
        else:
            names = ", ".join(sorted(compatible))
            raise RuntimeError(
                f"multiple single-interface radios exist on {phyname}: {names}; "
                "select the physical netdev with --interface"
            )
        reason = interface_driver(chosen)
        if reason:
            reason = f"{reason} driver"
        else:
            reason = "a PHY without NEW_INTERFACE support"
        log(f"[live] detected {reason} on {chosen}; avoiding VIF creation")
        return chosen
    if not candidates:
        raise RuntimeError(
            f"no existing physical interface found on {phyname} for "
            "--single-interface"
        )
    if len(candidates) != 1:
        names = ", ".join(sorted(candidates))
        raise RuntimeError(
            "single-interface reuse requires exactly one non-LDN netdev on "
            f"{phyname}; found {len(candidates)} ({names})"
        )
    return candidates[0]


def restore_interface_mac_address(iface, expected_mac, log=print):
    """Restore and verify a reused netdev's pre-LDN MAC address."""
    if expected_mac is None:
        return
    current = interface_mac_address(iface)
    if current == expected_mac:
        return
    failures = []
    for command, label in (
        (["nmcli", "device", "set", iface, "managed", "no"], "NetworkManager detach"),
        (["ip", "link", "set", iface, "down"], "administrative down"),
        (["ip", "link", "set", iface, "address", expected_mac], "MAC restore"),
    ):
        try:
            result = _run(command)
        except BaseException as exc:
            failures.append(f"{label} failed: {exc}")
            break
        if getattr(result, "returncode", 0) != 0:
            failures.append(f"{label} failed")
            break
    if interface_mac_address(iface) != expected_mac:
        failures.append(f"{iface} MAC address was not restored")
    if failures:
        raise RuntimeError("; ".join(failures))
    log(f"[live] restored MAC address for {iface}")


def restore_wireless_interface_types(interface_types, log=print):
    """Restore and verify existing netdev types before relay cleanup is acked."""
    failures = []
    for iface, interface_type in reversed(tuple(interface_types.items())):
        down = _run(["ip", "link", "set", iface, "down"])
        if getattr(down, "returncode", 0) != 0:
            failures.append(f"could not bring {iface} down")
            continue
        if interface_type:
            changed = _run(["iw", "dev", iface, "set", "type", interface_type])
            if getattr(changed, "returncode", 0) != 0:
                failures.append(
                    f"could not restore {iface} to wireless type {interface_type}"
                )
                continue
            actual = wireless_interface_type(iface)
            if actual != interface_type:
                failures.append(
                    f"{iface} reported wireless type {actual}, expected {interface_type}"
                )
                continue
        log(f"[live] restored wireless mode for {iface} ({interface_type})")
        interface_types.pop(iface, None)
    if failures:
        raise RuntimeError("; ".join(failures))


def _iface_exists(iface):
    return os.path.exists(f"/sys/class/net/{iface}")


def _target_owned_ldn_vifs(
    mapping,
    target_phys,
    owned_ifaces,
    candidate_ifaces=None,
):
    """Resolve cleanup candidates without ever deleting an ambiguous netdev."""
    if mapping is None:
        raise RuntimeError(
            "could not inspect LDN virtual-interface ownership with 'iw dev'"
        )
    target_phys = set(target_phys or ())
    owned_ifaces = set(owned_ifaces or ())
    candidates = (
        set(LDN_VIFS) if candidate_ifaces is None else set(candidate_ifaces)
    ) | owned_ifaces
    failures = []
    target_owned = []
    for vif in sorted(candidates):
        owners = {
            phy for phy, ifaces in mapping.items()
            if vif in ifaces
        }
        exists = bool(owners) or _iface_exists(vif)
        if not exists:
            continue
        if not owners:
            failures.append(
                f"{vif} exists but its wireless PHY ownership is unknown"
            )
            continue
        if len(owners) != 1:
            failures.append(
                f"{vif} has ambiguous wireless PHY ownership: {sorted(owners)!r}"
            )
            continue
        owner = next(iter(owners))
        if owner not in target_phys:
            failures.append(
                f"{vif} belongs to off-target {owner}, not {sorted(target_phys)!r}"
            )
            continue
        if vif not in owned_ifaces:
            failures.append(
                f"{vif} on {owner} is not owned by this LDN session"
            )
            continue
        target_owned.append(vif)
    if failures:
        raise RuntimeError("; ".join(failures))
    return target_owned


def free_radio(
    phys,
    log=print,
    restore_state=None,
    *,
    delete_vifs=True,
    on_mutation=None,
    reserved_ifaces=None,
    owned_ifaces=None,
):
    """Delete leftover LDN vifs and take any other interface off the radio so the station can
    grab the channel (fixes SET_CHANNEL -> EBUSY). Brings normal Wi-Fi down on that adapter
    for the duration; LiveTransport records and restores its prior state. ``on_mutation`` is
    called after all fail-closed checks and immediately before the first host write. Needs root."""
    mapping = list_phy_ifaces()
    if mapping is None:
        raise RuntimeError(
            "could not inspect wireless interfaces with 'iw dev'; refusing "
            "radio mutation"
        )
    target_phys = {phy for phy in phys if phy}
    if restore_state is None:
        restore_state = {}
    # Known LDN names may be stale products of our protocol and are handled by
    # the scoped cleanup below.  An arbitrary requested station name is not
    # ours until this session creates it, so any pre-existing netdev with that
    # name is a collision rather than something we may disconnect or delete.
    reserved_custom_ifaces = set(reserved_ifaces or ()) - LDN_VIFS
    session_owned_ifaces = set(owned_ifaces or ())
    cleanup_vif_names = LDN_VIFS | session_owned_ifaces
    try:
        _target_owned_ldn_vifs(
            mapping,
            target_phys,
            session_owned_ifaces,
            candidate_ifaces=reserved_custom_ifaces,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"requested LDN interface collision: {exc}"
        ) from exc
    target_vifs = _target_owned_ldn_vifs(
        mapping, target_phys, cleanup_vif_names
    )
    if not delete_vifs and target_vifs:
        raise RuntimeError(
            "LDN virtual interfaces already exist in single-interface mode: "
            + ", ".join(target_vifs)
        )
    current_non_ldn = sorted({
        iface
        for phy in target_phys
        for iface in mapping.get(phy, [])
        if iface not in cleanup_vif_names
    })
    if not delete_vifs and (
        len(current_non_ldn) != 1
        or set(current_non_ldn) != set(restore_state)
    ):
        leased = ", ".join(sorted(restore_state)) or "none"
        current = ", ".join(current_non_ldn) or "none"
        raise RuntimeError(
            "single-interface PHY membership changed after preflight; "
            f"leased={leased}, current={current}; refusing radio mutation"
        )
    target_ifaces = sorted({
        iface
        for phy in target_phys
        for iface in mapping.get(phy, [])
        if iface not in cleanup_vif_names
    })
    snapshots = {}
    for iface in target_ifaces:
        state = (
            restore_state[iface]
            if iface in restore_state else _interface_state(iface)
        )
        if state[0] is None or state[1] is None:
            raise RuntimeError(
                f"could not snapshot NetworkManager/link state for {iface}; "
                "refusing radio mutation"
            )
        snapshots[iface] = state
    for iface, state in snapshots.items():
        restore_state.setdefault(iface, state)
    will_mutate = bool(target_ifaces or (delete_vifs and target_vifs))
    if will_mutate and on_mutation is not None:
        on_mutation()
    for phy in target_phys:
        for iface in mapping.get(phy, []):
            if iface in cleanup_vif_names:
                continue
            else:
                _run(["nmcli", "device", "disconnect", iface])
                _run(["nmcli", "device", "set", iface, "managed", "no"])
                _run(["ip", "link", "set", iface, "down"])
                log(f"[live] freed radio: brought {iface} ({phy}) down")
    # A failed join may leak its station VIF.  Delete only names whose ownership
    # was proven to be the selected PHY; an unmapped or off-target name may
    # belong to another process/radio and was rejected above without mutation.
    if delete_vifs:
        for vif in target_vifs:
            _iw_del(vif)
            _run(["ip", "link", "del", vif])
            log(f"[live] freed radio: removed stale LDN vif {vif}")
    time.sleep(0.3)
    post_mapping = list_phy_ifaces()
    remaining_vifs = _target_owned_ldn_vifs(
        post_mapping, target_phys, cleanup_vif_names
    )
    if remaining_vifs:
        raise RuntimeError(
            "LDN virtual interfaces remain after radio preparation: "
            + ", ".join(remaining_vifs)
        )
    try:
        _target_owned_ldn_vifs(
            post_mapping,
            target_phys,
            session_owned_ifaces,
            candidate_ifaces=reserved_custom_ifaces,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"requested LDN interface collision after radio preparation: {exc}"
        ) from exc
    return restore_state


def light_cleanup(
    log=print,
    *,
    delete_vifs=True,
    target_phys=None,
    owned_ifaces=None,
):
    """Delete only target-PHY VIFs owned by this session, then verify."""
    target_vifs = _target_owned_ldn_vifs(
        list_phy_ifaces(), target_phys, owned_ifaces
    )
    if not delete_vifs and target_vifs:
        raise RuntimeError(
            "LDN virtual interfaces remain in single-interface mode: "
            + ", ".join(target_vifs)
        )
    if delete_vifs:
        for iface in target_vifs:
            _iw_del(iface)
    time.sleep(0.3)
    remaining = _target_owned_ldn_vifs(
        list_phy_ifaces(), target_phys, owned_ifaces
    )
    if remaining:
        raise RuntimeError(
            "LDN virtual interfaces remain after cleanup: " + ", ".join(remaining)
        )


def tune_iface(iface, keep_ip, broadcast_ip, log=print, restore_state=None,
               remove_stray=True, protected_broadcast_routes=None):
    """Make the LDN interface deliver the host's link-local subnet broadcasts: relax rp_filter,
    force the 169.254.X.255 broadcast route into the local table, and drop stray zeroconf
    addresses that would shadow it (ported from the bridge tooling). Needs root."""
    if restore_state is None:
        restore_state = {}
    sysctl_targets = (
        (f"net.ipv4.conf.{iface}.rp_filter", "0"),
        ("net.ipv4.conf.all.rp_filter", "0"),
        ("net.ipv4.conf.default.rp_filter", "0"),
        (f"net.ipv4.conf.{iface}.accept_local", "1"),
    )
    snapshots = {}
    for key, _value in sysctl_targets:
        original = restore_state.get(key) if key in restore_state else _read_sysctl(key)
        if original is None:
            raise RuntimeError(
                f"could not snapshot sysctl {key}; refusing network mutation"
            )
        snapshots[key] = original
    # Commit the complete baseline before the first write.  A partial tuning
    # failure can therefore always restore every persistent/global key.
    for key, original in snapshots.items():
        restore_state.setdefault(key, original)
    _run(["nmcli", "device", "set", iface, "managed", "no"])
    _run(["pkill", "-f", f"avahi-autoipd.*{iface}"])
    for key, value in sysctl_targets:
        result = _sysctl(key, value)
        if getattr(result, "returncode", 0) != 0:
            raise RuntimeError(f"could not set sysctl {key}={value}")
    if broadcast_ip not in (protected_broadcast_routes or set()):
        result = _run([
            "ip", "route", "replace", "table", "local", "broadcast",
            broadcast_ip, "dev", iface, "proto", "static", "scope", "link",
            "src", keep_ip,
        ])
        if getattr(result, "returncode", 0) != 0:
            raise RuntimeError(
                f"could not install local broadcast route {broadcast_ip} on {iface}"
            )
    else:
        log(f"[live] preserved pre-existing local broadcast route {broadcast_ip}")
    if remove_stray:
        try:
            out = subprocess.check_output(["ip", "-4", "addr", "show", "dev", iface],
                                          text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    cidr = line.split()[1]
                    ip, _, prefix = cidr.partition("/")
                    if ip != keep_ip and prefix != "24":
                        result = _run(["ip", "addr", "del", cidr, "dev", iface])
                        if getattr(result, "returncode", 0) != 0:
                            raise RuntimeError(
                                f"could not remove stray address {cidr} from {iface}"
                            )
                        log(f"[live] removed stray address {cidr} from {iface}")
        except RuntimeError:
            raise
        except Exception as e:
            log(f"[live] stray-address cleanup skipped: {e}")
    return restore_state

class LiveTransport:
    """Join LDN and exchange Pia datagrams; construction alone performs no join."""

    LOCAL_COMMUNICATION_ID = ZA_PROFILE.title_id_int
    SCENE_ID = ZA_BOX_TRADE_SCENE_ID
    APPLICATION_VERSION = 1

    def __init__(self, password=None, nickname="ZA-Trade", keys_path="~/.switch/prod.keys",
                 local_comm_id=None, scene_id=None, app_version=None,
                 phyname="phy0", ifname="ldnclient", log=print,
                 game_name=ZA_PROFILE.name, app_data_decoder=None, auto_app_version=False,
                 require_exact_network=False, expected_max_participants=None,
                 application_data_matcher=None,
                 single_interface=None, preferred_scan_channel=None, bss_helper=None,
                 max_datagram_size=DEFAULT_MAX_LIVE_DATAGRAM_BYTES):
        self.info = getattr(log, "info", log)   # clean milestone sink (default-mode narration)
        self.password = password if password else ZA_PROFILE.ldn_passphrase
        self.nickname = validate_ldn_nickname(nickname)
        self.keys_path = keys_path
        self.phyname = phyname
        self.ifname = ifname
        if single_interface is not None and not isinstance(single_interface, bool):
            raise TypeError("single_interface must be a bool or None")
        if (
            not isinstance(max_datagram_size, int)
            or isinstance(max_datagram_size, bool)
            or not 1 <= max_datagram_size <= 65507
        ):
            raise ValueError("max_datagram_size must be from 1 through 65507 bytes")
        self._bss_seed = None
        if bss_helper is not None:
            if single_interface is False:
                raise ValueError("BSS helper requires reuse of an existing interface")
            from zasim.ldn.bss_seed import SeedLease, validate_bundle
            self._bss_seed = SeedLease(validate_bundle(bss_helper), log=log)
            single_interface = True
        self.single_interface = single_interface
        if (
            preferred_scan_channel is not None
            and (
                not isinstance(preferred_scan_channel, int)
                or isinstance(preferred_scan_channel, bool)
            )
        ):
            raise TypeError("preferred_scan_channel must be an int or None")
        self.preferred_scan_channel = preferred_scan_channel
        self._last_ldn_channel = None
        self.max_datagram_size = max_datagram_size
        self._single_interface_active = False
        self._session_ifname = ifname
        self.game_name = game_name
        self.app_data_decoder = app_data_decoder or _application_bytes
        self.auto_app_version = auto_app_version
        self.require_exact_network = bool(require_exact_network)
        self.expected_max_participants = expected_max_participants
        if application_data_matcher is not None and not callable(
            application_data_matcher
        ):
            raise TypeError("application_data_matcher must be callable or None")
        self.application_data_matcher = application_data_matcher
        if local_comm_id is not None:
            self.LOCAL_COMMUNICATION_ID = local_comm_id
        if scene_id is not None:
            self.SCENE_ID = scene_id
        if app_version is not None:
            self.APPLICATION_VERSION = app_version
        self.log = log
        self.ssid = None
        self.our_ip = None
        self.host_ip = None
        self.our_mac = None        # our 6-byte LDN MAC = our Pia connection GUID (constant id)
        self.host_mac = None       # the host's 6-byte LDN MAC = its Pia connection GUID
        self.app_data = None       # the joined host's title-specific LDN advertisement
        self.network_app_version = None
        self.iface = None
        self.broadcast = None
        self._tx = None
        self._diagnostic_tx = {}
        self._accepted_udp_ports = {PIA_PORT}
        self._last_receive_ports = []
        self._rx = None
        self._rx_seen = 0
        self._tx_diag_lock = threading.Lock()
        self._ldn_link_lock = threading.Lock()
        self._ldn_link_error = None
        # Monotonic per-instance evidence for the first terminal LDN loss.
        # This is deliberately separate from IEEE deauth reason codes: the
        # latter arrive through iw, while this counter proves when the Python
        # LDN worker observed its own disconnect event.
        self._ldn_disconnect_events = 0
        self._tx_probe_sequence = 0
        self._tx_successful_sends = 0
        self._tx_pending_egress = []
        self._tx_egress_matched = 0
        self._tx_egress_mismatched = 0
        self._tx_egress_unmatched = 0
        self._tx_egress_evicted = 0
        self._tx_egress_summary_logged = False
        self._thread = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        # Every retry owns a distinct stop token.  Re-clearing the shared
        # token used to let a tardy worker from an earlier attempt resume and
        # race the new LDN session.
        self._attempt_stop = None
        # Single-interface retries may wait for Z-A to rotate the 16-byte
        # NetworkInfo identity after a failed WLAN connection.  The input and
        # output slots are reset/consumed by start(), so an identity is never
        # excluded by a later start() call or for more than one retry.
        self._attempt_excluded_ssid = None
        self._attempt_failed_ssid = None
        self._monitor_receive_silent = False
        self._silent_usb_recovery_attempted = False
        self._err = None
        self._join_exception = None
        self._join_retryable = True
        self._radio_restore_state = {}
        self._sysctl_restore_state = {}
        self._wireless_type_restore_state = {}
        self._single_interface_original_addresses = None
        self._single_interface_original_neighbors = None
        self._single_interface_original_broadcast_routes = None
        self._single_interface_original_mac = None
        self._host_neighbor_restore_lease = None
        self._connection_restore_state = {}
        self._connection_baseline_state = {}
        self._autoconnect_baseline_state = {}
        self._single_interface_session = single_interface is True
        self._owned_ldn_vifs = set()
        self._radio_teardown_complete = False

    # -- LDN join runs in a trio thread that keeps the connection alive -------
    def _signal_worker_stop(self):
        self._stop.set()
        attempt_stop = self._attempt_stop
        if attempt_stop is not None:
            attempt_stop.set()

    def _join_worker_or_raise(self, timeout=2):
        thread = self._thread
        if thread is None:
            return
        if getattr(thread, "ident", None) is None:
            # A Thread.start() failure must never mask radio restoration with
            # "cannot join thread before it is started".
            self._thread = None
            self._attempt_stop = None
            return
        thread.join(timeout=max(timeout, 8) if getattr(self, "_bss_seed", None) is not None else timeout)
        if thread.is_alive():
            raise RuntimeError(
                "LDN worker did not stop; refusing to retry or restore the radio "
                "while it may still be in use"
            )
        self._thread = None
        self._attempt_stop = None

    def _teardown_radio(self):
        """Verify VIF cleanup and restore leased host state as one transaction."""
        if self._radio_teardown_complete:
            return
        cleanup_errors = []
        seed = getattr(self, "_bss_seed", None)
        if seed is not None:
            try:
                seed.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        owned_ifaces = set(self._owned_ldn_vifs)
        try:
            # The station netdev must still exist while an overwritten entry is
            # restored.  VIF deletion and physical-interface restoration happen
            # only after this lease has either verified or reported a failure.
            self._restore_host_neighbor()
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            if self._single_interface_session:
                # The reused-device path never creates or deletes a netdev.
                light_cleanup(
                    self.log,
                    delete_vifs=False,
                    target_phys={self.phyname},
                    owned_ifaces=owned_ifaces,
                )
            else:
                light_cleanup(
                    self.log,
                    target_phys={self.phyname},
                    owned_ifaces=owned_ifaces,
                )
        except BaseException as exc:
            cleanup_errors.append(exc)
        self._restore_radio_state(prior_errors=cleanup_errors)

    def _prepare_radio_path(self):
        """Choose and snapshot a no-VIF path before NetworkManager is detached."""
        try:
            chosen = choose_single_interface(
                self.phyname,
                self.ifname,
                self.single_interface,
                self.log,
            )
        except RuntimeError:
            # USB reset/replug allocates a new phyN while its persistent netdev
            # name often remains stable. Follow that explicit interface only
            # when the requested old PHY disappeared completely.
            if self.single_interface is not True:
                raise
            resolved_phy = _resolve_reenumerated_single_interface_phy(
                self.phyname, self.ifname, self.log
            )
            if resolved_phy == self.phyname:
                raise
            self.phyname = resolved_phy
            chosen = choose_single_interface(
                self.phyname,
                self.ifname,
                self.single_interface,
                self.log,
            )
        if chosen is None:
            self._single_interface_active = False
            self._single_interface_session = False
            self._session_ifname = self.ifname
            return
        default_route = interface_has_default_route(chosen)
        if default_route is None:
            raise RuntimeError(
                f"could not determine whether {chosen} carries the default route; "
                "refusing single-interface mode"
            )
        if default_route:
            raise RuntimeError(
                f"refusing to reuse {chosen}: it carries an IPv4/IPv6 default route; "
                "use a dedicated Wi-Fi adapter for LDN"
            )
        carries_ssh = interface_carries_ssh(chosen)
        if carries_ssh is None:
            raise RuntimeError(
                f"could not determine whether {chosen} carries SSH; refusing "
                "single-interface mode"
            )
        if carries_ssh:
            raise RuntimeError(
                f"refusing to reuse {chosen}: it carries the active SSH session"
            )
        ipv6_state = interface_has_ipv6_state(chosen)
        if ipv6_state is None:
            raise RuntimeError(
                f"could not inspect existing IPv6 state for {chosen}; refusing "
                "single-interface mode"
            )
        if ipv6_state:
            raise RuntimeError(
                f"refusing to reuse {chosen}: existing IPv6 addresses, routes, "
                "or neighbors could be destroyed by the LDN station"
            )
        managed, admin_up = _interface_state(chosen)
        if managed is None or admin_up is None:
            raise RuntimeError(
                f"could not snapshot NetworkManager/link state for {chosen}; "
                "refusing single-interface mode"
            )
        interface_type = wireless_interface_type(chosen)
        if interface_type is None:
            raise RuntimeError(
                f"could not read the original wireless mode of {chosen}; "
                "refusing single-interface mode because it could not be restored"
            )
        original_mac = interface_mac_address(chosen)
        if original_mac is None:
            raise RuntimeError(
                f"could not read a restorable MAC address for {chosen}; "
                "refusing single-interface mode"
            )
        addresses = interface_ipv4_addresses(chosen)
        neighbors = interface_neighbor_ips(chosen)
        broadcast_routes = interface_local_broadcast_routes(chosen)
        if addresses is None or neighbors is None or broadcast_routes is None:
            raise RuntimeError(
                f"could not snapshot IPv4 state for {chosen}; refusing "
                "single-interface mode because cleanup could not be verified"
            )
        connection_uuid = interface_active_connection(chosen)
        if connection_uuid is None:
            raise RuntimeError(
                f"could not snapshot NetworkManager connection state for {chosen}"
            )
        autoconnect = interface_autoconnect(chosen)
        if autoconnect is None:
            raise RuntimeError(
                f"could not snapshot NetworkManager autoconnect policy for {chosen}"
            )
        # ldn.Station disables IPv6 on its interface.  A virtual netdev normally
        # disappears afterwards; for a reused physical netdev it must be restored.
        ipv6_key = f"net.ipv6.conf.{chosen}.disable_ipv6"
        ipv6_value = _read_sysctl(ipv6_key)
        if ipv6_value is None:
            raise RuntimeError(
                f"could not snapshot {ipv6_key}; refusing single-interface mode"
            )
        # Publish the lease only after every read-only preflight completed.
        # A failed preflight therefore cannot make stop() treat unknown host
        # state as an empty baseline and delete it.
        self._single_interface_active = True
        self._single_interface_session = True
        self._session_ifname = chosen
        self._radio_restore_state.setdefault(chosen, (managed, admin_up))
        self._wireless_type_restore_state.setdefault(chosen, interface_type)
        self._single_interface_original_addresses = addresses
        self._single_interface_original_neighbors = neighbors
        self._single_interface_original_broadcast_routes = broadcast_routes
        self._single_interface_original_mac = original_mac
        self._connection_baseline_state[chosen] = connection_uuid
        self._autoconnect_baseline_state[chosen] = autoconnect
        if connection_uuid is not False:
            self._connection_restore_state[chosen] = connection_uuid
        self._sysctl_restore_state.setdefault(ipv6_key, ipv6_value)

    def _cleanup_single_interface_network(self, *, final=False):
        """Remove only link-local state created for the reused LDN interface."""
        if not self._single_interface_active:
            return
        iface = self._session_ifname
        failures = []
        current_routes = interface_local_broadcast_routes(iface)
        if current_routes is None:
            failures.append(f"could not inspect local broadcast routes on {iface}")
        else:
            original = self._single_interface_original_broadcast_routes or set()
            for destination in sorted(current_routes - original):
                _run([
                    "ip", "route", "del", "table", "local", "broadcast",
                    destination, "dev", iface,
                ])
        current_neighbors = interface_neighbor_ips(iface)
        if current_neighbors is None:
            failures.append(f"could not inspect IPv4 neighbors on {iface}")
        else:
            original = self._single_interface_original_neighbors or set()
            for peer_ip in sorted(current_neighbors - original):
                _run(["ip", "neigh", "del", peer_ip, "dev", iface])
        current_addresses = interface_ipv4_addresses(iface)
        if current_addresses is None:
            failures.append(f"could not inspect IPv4 addresses on {iface}")
        else:
            original = self._single_interface_original_addresses or set()
            for cidr in sorted(current_addresses - original):
                _run(["ip", "addr", "del", cidr, "dev", iface])
        remaining_routes = interface_local_broadcast_routes(iface)
        remaining_neighbors = interface_neighbor_ips(iface)
        remaining_addresses = interface_ipv4_addresses(iface)
        if remaining_routes is None or (
            remaining_routes
            - (self._single_interface_original_broadcast_routes or set())
        ):
            failures.append(f"LDN broadcast routes remain on {iface}")
        if remaining_neighbors is None or (
            remaining_neighbors - (self._single_interface_original_neighbors or set())
        ):
            failures.append(f"LDN IPv4 neighbors remain on {iface}")
        if remaining_addresses is None or (
            remaining_addresses - (self._single_interface_original_addresses or set())
        ):
            failures.append(f"LDN IPv4 addresses remain on {iface}")
        if failures:
            raise RuntimeError("; ".join(dict.fromkeys(failures)))

    def _clear_radio_restore_lease(self):
        """Release snapshots only when no mutation occurred or restore verified."""
        seed = getattr(self, "_bss_seed", None)
        if seed is not None:
            seed.close()
        if self._host_neighbor_restore_lease is not None:
            raise RuntimeError(
                "static LDN host-neighbor restoration is still incomplete"
            )
        self._single_interface_original_addresses = None
        self._single_interface_original_neighbors = None
        self._single_interface_original_broadcast_routes = None
        self._single_interface_original_mac = None
        self._monitor_receive_silent = False
        self._silent_usb_recovery_attempted = False
        self._wireless_type_restore_state.clear()
        self._radio_restore_state.clear()
        self._sysctl_restore_state.clear()
        self._connection_restore_state.clear()
        self._connection_baseline_state.clear()
        self._autoconnect_baseline_state.clear()
        self._single_interface_active = False
        self._owned_ldn_vifs.clear()
        self._radio_teardown_complete = True

    def _restore_radio_state(self, prior_errors=()):
        cleanup_errors = list(prior_errors)
        critical_reuse = self._single_interface_active
        expected_links = dict(self._radio_restore_state)
        expected_sysctls = dict(self._sysctl_restore_state)
        expected_types = dict(self._wireless_type_restore_state)
        expected_connections = dict(self._connection_baseline_state)
        expected_autoconnect = dict(self._autoconnect_baseline_state)
        expected_mac = self._single_interface_original_mac
        try:
            self._cleanup_single_interface_network(final=True)
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            restore_wireless_interface_types(
                dict(self._wireless_type_restore_state),
                self.log,
            )
        except BaseException:
            # NetworkManager restoration below may itself return the device to
            # managed mode. Verify the final state after all cleanup instead
            # of prematurely abandoning the remaining restoration work.
            pass
        try:
            if critical_reuse and expected_mac is not None:
                restore_interface_mac_address(
                    self._session_ifname, expected_mac, self.log
                )
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            restore_network_state(
                dict(self._radio_restore_state),
                dict(self._sysctl_restore_state),
                self.log,
                set(self._owned_ldn_vifs),
            )
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            restore_active_connections(
                dict(self._connection_restore_state),
                self.log,
            )
        except BaseException as exc:
            cleanup_errors.append(exc)
        if critical_reuse:
            if (
                expected_mac is not None
                and interface_mac_address(self._session_ifname) != expected_mac
            ):
                cleanup_errors.append(RuntimeError(
                    f"{self._session_ifname} MAC address was not restored"
                ))
            for iface, expected_type in expected_types.items():
                actual_type = wireless_interface_type(iface)
                if actual_type != expected_type:
                    cleanup_errors.append(RuntimeError(
                        f"{iface} wireless type is {actual_type!r}, expected "
                        f"{expected_type!r} after cleanup"
                    ))
            for iface, (expected_managed, expected_up) in expected_links.items():
                actual_managed, actual_up = _interface_state(iface)
                if (
                    (expected_managed is not None and actual_managed != expected_managed)
                    or (expected_up is not None and actual_up != expected_up)
                ):
                    # The earlier restore_network_state readback may have
                    # succeeded. Distinguish this later snapshot from worker
                    # teardown failure, without weakening the restoration lease
                    # or guessing that NetworkManager caused the state change.
                    self.log(
                        "[diag] radio cleanup link mismatch phase=final-readback "
                        f"expected_managed={expected_managed} actual_managed={actual_managed} "
                        f"expected_up={expected_up} actual_up={actual_up}"
                    )
                if expected_managed is not None and actual_managed != expected_managed:
                    cleanup_errors.append(RuntimeError(
                        f"{iface} NetworkManager managed state was not restored"
                    ))
                if expected_up is not None and actual_up != expected_up:
                    cleanup_errors.append(RuntimeError(
                        f"{iface} administrative link state was not restored"
                    ))
            for key, expected_value in expected_sysctls.items():
                if expected_value is not None and _read_sysctl(key) != expected_value:
                    cleanup_errors.append(RuntimeError(
                        f"sysctl {key} was not restored"
                    ))
            for iface, expected_uuid in expected_connections.items():
                if expected_uuid is False:
                    # A managed/up device may autoconnect just after our query.
                    # Temporarily inhibit it while restoring the disconnected
                    # state; the original policy is restored below.
                    _run(["nmcli", "device", "set", iface, "autoconnect", "no"])
                    _run(["nmcli", "device", "disconnect", iface])
                actual_uuid = interface_active_connection(iface)
                if actual_uuid != expected_uuid:
                    cleanup_errors.append(RuntimeError(
                        f"{iface} active NetworkManager connection was not restored"
                    ))
            for iface, expected_autoconnect_value in expected_autoconnect.items():
                result = _run([
                    "nmcli", "device", "set", iface, "autoconnect",
                    "yes" if expected_autoconnect_value else "no",
                ])
                actual_autoconnect = interface_autoconnect(iface)
                if (
                    getattr(result, "returncode", 0) != 0
                    or actual_autoconnect != expected_autoconnect_value
                ):
                    cleanup_errors.append(RuntimeError(
                        f"{iface} NetworkManager autoconnect policy was not restored"
                    ))
            # NetworkManager reconnection runs before this verification because
            # it may legitimately recreate addresses, neighbors, and routes
            # that disappeared while the device was unmanaged.  Do not guess
            # how to reconstruct incomplete entries; retain the lease and fail
            # cleanup if any original state is still absent.
            iface = self._session_ifname
            current_addresses = interface_ipv4_addresses(iface)
            missing_addresses = (
                (self._single_interface_original_addresses or set())
                - (current_addresses or set())
            )
            if current_addresses is None or missing_addresses:
                cleanup_errors.append(RuntimeError(
                    f"original IPv4 addresses are missing on {iface}: "
                    f"{sorted(missing_addresses)!r}"
                ))
            current_neighbors = interface_neighbor_ips(iface)
            missing_neighbors = (
                (self._single_interface_original_neighbors or set())
                - (current_neighbors or set())
            )
            if current_neighbors is None or missing_neighbors:
                cleanup_errors.append(RuntimeError(
                    f"original IPv4 neighbors are missing on {iface}: "
                    f"{sorted(missing_neighbors)!r}"
                ))
            current_routes = interface_local_broadcast_routes(iface)
            missing_routes = (
                (self._single_interface_original_broadcast_routes or set())
                - (current_routes or set())
            )
            if current_routes is None or missing_routes:
                cleanup_errors.append(RuntimeError(
                    f"original local broadcast routes are missing on {iface}: "
                    f"{sorted(missing_routes)!r}"
                ))
        if cleanup_errors:
            details = "; ".join(str(error) for error in cleanup_errors)
            raise RuntimeError(f"radio cleanup incomplete: {details}")
        # Release the baseline only after *all* critical state is verified.
        # A second stop() is then a no-op for the physical interface; on any
        # failure the complete baseline remains available for another attempt.
        self._clear_radio_restore_lease()

    def start(self, timeout=30, attempts=3, settle=1.5, cancel_event=None):
        """Join the LDN network, retrying transient failures. The LDN/nl80211 layer flakes
        intermittently (radio busy, association timeout, a stale vif racing the fresh join) - the
        SAME failure the bridge hits as 'connection failed'. Rather than make the user re-run, we
        free the radio and retry up to `attempts` times, logging each attempt's FULLY-UNWRAPPED
        cause (see _format_join_error) so persistent problems are still diagnosable instead of
        hidden behind trio's opaque ExceptionGroup."""
        _cancel_requested(cancel_event)
        pending_lease = (
            self._single_interface_active
            or self._thread is not None
            or any((
                self._radio_restore_state,
                self._sysctl_restore_state,
                self._wireless_type_restore_state,
                self._connection_restore_state,
                self._connection_baseline_state,
                self._autoconnect_baseline_state,
            ))
            or self._single_interface_original_addresses is not None
            or self._single_interface_original_neighbors is not None
            or self._single_interface_original_broadcast_routes is not None
            or self._single_interface_original_mac is not None
            or self._host_neighbor_restore_lease is not None
        )
        if pending_lease:
            raise RuntimeError(
                "a previous radio restoration lease is still active; call stop() "
                "and resolve cleanup errors before starting again"
            )
        # No host mutation has happened yet.  If preflight rejects the radio,
        # a caller's defensive stop() must remain a no-op.
        self._radio_teardown_complete = True
        self._prepare_radio_path()
        seed = getattr(self, "_bss_seed", None)
        if seed is not None:
            try:
                seed.acquire(self._session_ifname)
            except BaseException:
                self._clear_radio_restore_lease()
                raise
            attempts = 1  # one insertion/connection per explicit helper session
        self._radio_teardown_complete = False
        self._owned_ldn_vifs.clear()
        self._silent_usb_recovery_attempted = False
        last_err = None
        radio_ever_mutated = False
        failed_ssid_for_retry = None
        self._attempt_excluded_ssid = None
        self._attempt_failed_ssid = None
        for attempt in range(1, attempts + 1):
            if _cancel_requested(cancel_event):
                if radio_ever_mutated:
                    self._teardown_radio()
                else:
                    self._clear_radio_restore_lease()
                return self
            radio_mutated = False

            def mark_radio_mutated():
                nonlocal radio_mutated, radio_ever_mutated
                radio_mutated = True
                radio_ever_mutated = True

            try:
                free_radio(
                    {self.phyname},
                    self.log,
                    self._radio_restore_state,
                    delete_vifs=not self._single_interface_active,
                    on_mutation=mark_radio_mutated,
                    reserved_ifaces=(
                        set()
                        if self._single_interface_active
                        else {self._session_ifname}
                    ),
                    owned_ifaces=set(self._owned_ldn_vifs),
                )                                       # clear the radio before each join attempt
            except BaseException as free_error:
                if not radio_mutated and not radio_ever_mutated:
                    self._clear_radio_restore_lease()
                    raise
                try:
                    self._teardown_radio()
                except BaseException as restore_error:
                    raise RuntimeError(
                        "radio preparation failed and restoration also failed: "
                        f"{restore_error}"
                    ) from free_error
                raise
            if self._single_interface_active:
                self._owned_ldn_vifs.clear()
            else:
                # ldn.scan uses its default ``ldn`` monitor VIF; connect uses
                # the requested station VIF.  No other known name is owned by
                # this session merely because it appears on the same PHY.
                self._owned_ldn_vifs = {"ldn", self._session_ifname}
            self._err = None
            self._join_exception = None
            with self._ldn_link_lock:
                self._ldn_link_error = None
                self._ldn_disconnect_events = 0
            self._join_retryable = True
            self._monitor_receive_silent = False
            self._ready.clear()
            self._stop.clear()
            self._attempt_stop = threading.Event()
            self._attempt_excluded_ssid = (
                failed_ssid_for_retry if self._single_interface_active else None
            )
            # Consume the failure for exactly this retry.  A scan-only failure
            # must not carry an older exclusion into a later attempt.
            failed_ssid_for_retry = None
            self._attempt_failed_ssid = None
            thread = threading.Thread(target=self._run_ldn, daemon=True)
            try:
                thread.start()
            except BaseException as start_error:
                # Publish only started threads so stop() can never attempt to
                # join an unstarted object.  The radio was already freed, so
                # restoration belongs to this failure path, not its caller.
                self._thread = None
                self._attempt_stop = None
                try:
                    self._teardown_radio()
                except BaseException as restore_error:
                    raise RuntimeError(
                        "LDN worker could not start and radio restoration failed: "
                        f"{restore_error}"
                    ) from start_error
                raise
            self._thread = thread
            radio_ever_mutated = True
            deadline = time.monotonic() + timeout
            while not self._ready.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
                if _cancel_requested(cancel_event):
                    self._signal_worker_stop()
                    self._join_worker_or_raise()
                    self._teardown_radio()
                    return self
                if time.monotonic() >= deadline:
                    break
            if _cancel_requested(cancel_event):
                self._signal_worker_stop()
                self._join_worker_or_raise()
                self._teardown_radio()
                return self
            if not self._ready.is_set():
                last_err = f"LDN join timed out after {timeout}s (attempt {attempt}/{attempts})"
                self.log(f"[live] {last_err}")
                self._signal_worker_stop()              # ask the (stuck) attempt to unwind
            elif self._err:
                last_err = self._err                    # already unwrapped + logged in _run_ldn
            else:
                try:
                    tune_iface(
                        self.iface,
                        self.our_ip,
                        self.broadcast,
                        self.log,
                        self._sysctl_restore_state,
                        remove_stray=not self._single_interface_active,
                        protected_broadcast_routes=(
                            self._single_interface_original_broadcast_routes
                            if self._single_interface_active else None
                        ),
                    )                                   # host broadcasts
                    # ldn installs a permanent peer entry during association,
                    # but tune_iface() detaches NetworkManager and that state
                    # transition can flush it.  Reinstall only after tuning,
                    # then verify it before creating either data-plane socket.
                    self._install_host_neighbor()
                    self._setup_sockets()
                except BaseException:
                    self.stop()
                    raise
                if attempt > 1:
                    self.log(f"[live] LDN join succeeded on attempt {attempt}/{attempts}.")
                return self
            self._signal_worker_stop()
            # A retry may begin only after the prior attempt has completely
            # relinquished the PHY and shared transport fields.
            self._join_worker_or_raise()
            monitor_receive_silent = self._monitor_receive_silent
            if self._single_interface_active:
                failed_ssid_for_retry = self._attempt_failed_ssid
            if not self._join_retryable:
                self._teardown_radio()
                raise RuntimeError(f"LDN join cannot continue:\n{last_err}") from self._join_exception
            if self._single_interface_active:
                try:
                    self._cleanup_single_interface_network()
                except BaseException as cleanup_error:
                    try:
                        self._teardown_radio()
                    except BaseException as restore_error:
                        raise RuntimeError(
                            "single-interface retry cleanup failed: "
                            f"{cleanup_error}; final restoration failed: "
                            f"{restore_error}"
                        ) from cleanup_error
                    raise RuntimeError(
                        f"single-interface retry cleanup failed: {cleanup_error}"
                    ) from cleanup_error

            if (
                monitor_receive_silent
                and self._single_interface_active
                and attempt < attempts
                and not self._silent_usb_recovery_attempted
            ):
                self._silent_usb_recovery_attempted = True
                iface = self._session_ifname
                baseline_idle = (
                    self._connection_baseline_state.get(iface) is False
                    and not (self._single_interface_original_addresses or set())
                    and not (self._single_interface_original_neighbors or set())
                    and not (
                        self._single_interface_original_broadcast_routes or set()
                    )
                    and self._single_interface_original_mac is not None
                )
                identity = _usb_wireless_identity(iface) if baseline_idle else None
                if not baseline_idle:
                    self.log(
                        "[live] monitor received no frames, but the original "
                        "interface was not an idle disconnected adapter; "
                        "skipping automatic USB reset"
                    )
                elif identity is None:
                    self.log(
                        "[live] monitor received no frames, but the selected "
                        "interface is not a uniquely identifiable USB radio; "
                        "skipping automatic reset"
                    )
                elif (
                    identity["vendor"],
                    identity["product"],
                    identity["driver"],
                ) not in SILENT_USB_RECOVERY_DEVICES:
                    self.log(
                        "[live] monitor received no frames, but automatic USB "
                        "reset is not enabled for this adapter/driver; "
                        "continuing with an ordinary retry"
                    )
                else:
                    try:
                        self.phyname = reset_silent_usb_radio(
                            iface,
                            self._single_interface_original_mac,
                            self.log,
                        )
                    except BaseException as recovery_error:
                        current_phy = _phy_for_interface(iface)
                        if current_phy is not None:
                            self.phyname = current_phy
                        try:
                            self._teardown_radio()
                        except BaseException as restore_error:
                            raise RuntimeError(
                                "silent USB radio recovery failed and final "
                                f"restoration also failed: {restore_error}"
                            ) from recovery_error
                        raise RuntimeError(
                            f"silent USB radio recovery failed: {recovery_error}"
                        ) from recovery_error
                    self._last_ldn_channel = None
                    failed_ssid_for_retry = None
                    self._attempt_excluded_ssid = None
            if attempt < attempts:
                self.log(f"[live] retrying LDN join in {settle}s "
                         f"(attempt {attempt + 1}/{attempts})...")
                settle_deadline = time.monotonic() + settle
                while time.monotonic() < settle_deadline:
                    if _cancel_requested(cancel_event):
                        self._teardown_radio()
                        return self
                    time.sleep(min(0.05, settle_deadline - time.monotonic()))
        self._teardown_radio()
        raise RuntimeError(f"LDN join failed after {attempts} attempt(s):\n{last_err}") from self._join_exception

    def _run_ldn(self):
        # Capture this attempt's immutable token before any retry can replace
        # ``self._attempt_stop``.  The shared token remains as a compatibility
        # fallback for direct/internal callers.
        stop_event = self._attempt_stop or self._stop
        excluded_ssid = (
            self._attempt_excluded_ssid
            if self._single_interface_active else None
        )
        attempted_ssid = None
        connection_started = False
        connection_entered = False
        ldn_single = None
        try:
            import trio
            import ldn
            if self._single_interface_active:
                from zasim.ldn import ldn_single
        except ImportError as e:                     # pragma: no cover
            self._join_exception = e
            self._err = f"missing dep for live mode: {e}"
            self._ready.set()
            return

        async def session_main():
            nonlocal attempted_ssid, connection_started, connection_entered
            keys = ldn.load_keys(self.keys_path)
            self.info(f"Scanning for the {self.game_name} network...")

            def network_ssid(network):
                value = getattr(network, "ssid", None)
                if isinstance(value, (bytes, bytearray, memoryview)):
                    return bytes(value)
                return None

            def is_exact_room_joinable(network):
                return (
                    network.accept_policy != ldn.ACCEPT_NONE
                    and network.num_participants < network.max_participants
                    and network.local_communication_id
                    == self.LOCAL_COMMUNICATION_ID
                    and network.scene_id == self.SCENE_ID
                    and (
                        self.expected_max_participants is None
                        or network.max_participants
                        == self.expected_max_participants
                    )
                    and (
                        self.application_data_matcher is None
                        or self.application_data_matcher(
                            getattr(network, "application_data", b"") or b""
                        ) is True
                    )
                )

            exclusion_logged = False

            def is_exact_joinable(network):
                nonlocal exclusion_logged
                if not is_exact_room_joinable(network):
                    return False
                if (
                    excluded_ssid is not None
                    and network_ssid(network) == excluded_ssid
                ):
                    if not exclusion_logged:
                        self.log(
                            "[live] previous WLAN connection failed for this "
                            "LDN identity; waiting for the host to rotate it"
                        )
                        exclusion_logged = True
                    return False
                return True

            if self._single_interface_active:
                networks = await ldn_single.scan(
                    ldn,
                    keys,
                    phyname=self.phyname,
                    ifname=self._session_ifname,
                    stop_when=is_exact_joinable,
                    preferred_channel=(
                        self._last_ldn_channel
                        if self._last_ldn_channel is not None
                        else self.preferred_scan_channel
                    ),
                    # A previous channel is only a hint: putting its long
                    # preferred dwell first can consume the outer discovery
                    # deadline before the rotating 1/6/11 sweep reaches the
                    # channel where a regenerated room is advertising.
                    # Always survey the social channels first; the preferred
                    # channel remains a bounded final retry.
                    preferred_first=False,
                    # Revisit intermittent host/channel availability within
                    # one monitor lifetime. Continuous lossless 100 ms actions
                    # cannot phase-hide from a 500 ms dwell; a zero-candidate
                    # result still needs host/RX evidence, not more retries alone.
                    targeted_cycles=4,
                    # Never feed a later retry an identity captured only in
                    # an earlier channel window; a quiet final window must
                    # remain a miss rather than a stale discovery success.
                    retain_stale_fallbacks=False,
                    # SET_CHANNEL ACK can precede the monitor's actual kernel
                    # frequency.  Read back GET_INTERFACE after every hop so
                    # live logs distinguish an RF miss from an ACK/readback
                    # race; this is diagnostic-only and never retunes here.
                    diagnose_channel_readback=True,
                    log=self.log,
                )
            else:
                networks = await ldn.scan(keys, phyname=self.phyname)
            joinable = [n for n in networks
                        if n.accept_policy != ldn.ACCEPT_NONE
                        and n.num_participants < n.max_participants]
            eligible_joinable = [
                n for n in joinable
                if not (
                    self._single_interface_active
                    and excluded_ssid is not None
                    and network_ssid(n) == excluded_ssid
                )
            ]
            for n in networks:
                # also log accept_policy: a blacklist/whitelist host (policy != ACCEPT_ALL) passes the
                # joinable filter but then rejects our auth, surfacing as an opaque trio timeout - logging
                # it makes "this Switch isn't accepting this MAC" diagnosable.
                self.log(f"[live] saw network comm_id=0x{n.local_communication_id:016x} "
                         f"scene={n.scene_id} channel={getattr(n, 'channel', '?')} "
                         f"{n.num_participants}/{n.max_participants} "
                         f"accept_policy={getattr(n, 'accept_policy', '?')}")
            # Prefer an exact communication-id match; else fall back to the only joinable network.
            net = next((n for n in eligible_joinable if is_exact_joinable(n)), None)
            if (
                net is None
                and len(eligible_joinable) == 1
                and not self.require_exact_network
            ):
                net = eligible_joinable[0]
                self.log(f"[live] no comm-id match; using the only joinable network "
                         f"(comm_id=0x{net.local_communication_id:016x})")
            if net is None:
                requirements = []
                if self.expected_max_participants is not None:
                    requirements.append("participant limit")
                if self.application_data_matcher is not None:
                    requirements.append("application data")
                required = (
                    " with the required " + " and ".join(requirements)
                    if requirements else ""
                )
                self._err = (f"no exact joinable {self.game_name} network{required} "
                             f"(saw {len(networks)}, {len(joinable)} joinable) - "
                             "check the communication id and scene shown above")
                self._ready.set()
                return
            self.LOCAL_COMMUNICATION_ID = net.local_communication_id
            self.SCENE_ID = net.scene_id
            detected_channel = getattr(net, "channel", None)
            if isinstance(detected_channel, int) and not isinstance(
                detected_channel, bool
            ):
                self._last_ldn_channel = detected_channel
            self.network_app_version = getattr(net, "app_version", None)
            param = ldn.ConnectNetworkParam()
            param.keys = keys
            param.network = net
            param.password = self.password           # 64-byte emulator passphrase
            param.name = self.nickname.encode("utf-8")
            param.app_version = (self.network_app_version
                                 if self.auto_app_version and self.network_app_version is not None
                                 else self.APPLICATION_VERSION)
            param.phyname = self.phyname              # wifi phy (like the bridge: phy0)
            param.ifname = self._session_ifname       # VIF, or reused physical station iface
            self.info("Joining the host...")
            attempted_ssid = network_ssid(net)
            connection_started = True
            connection = (
                ldn_single.connect(
                    ldn, param, ifname=self._session_ifname, log=self.log,
                    **({"bss_seed": self._bss_seed} if getattr(self, "_bss_seed", None) is not None else {})
                )
                if self._single_interface_active
                else ldn.connect(param)
            )
            async with connection as network:
                # Entering ldn.connect means WLAN association and the LDN
                # control-port handshake both completed.  Failures after this
                # point must not quarantine an otherwise valid WLAN identity.
                connection_entered = True
                info = network.info()
                current_application_data = (
                    getattr(info, "application_data", b"") or b""
                )
                if (
                    self.application_data_matcher is not None
                    and self.application_data_matcher(current_application_data)
                    is not True
                ):
                    raise RuntimeError(
                        "joined LDN network application data no longer matches "
                        "the requested room"
                    )
                # Use the post-association snapshot, not a potentially stale
                # scan object, for the title/session adapter.
                self.app_data = self.app_data_decoder(
                    current_application_data, self.log
                )
                self.ssid = info.ssid
                self.iface = self._session_ifname
                # The host is participant 0 (the network creator); its IP fixes the 169.254.X subnet
                # [ldn/__init__.py NetworkInfo.participants; the bridge's network_nodes]. Each
                # ParticipantInfo carries ip_address + mac_address (the 6-byte LDN MAC = the Pia
                # connection GUID). We are the participant whose name matches our nickname (we set
                # param.name); fall back to the first connected non-host, then to subnet .2.
                parts = list(getattr(info, "participants", []) or [])
                host = parts[0] if parts else None
                self.host_ip = host.ip_address if host else "169.254.21.1"
                self.host_mac = bytes(host.mac_address) if host else b"\x00" * 6
                ours = next((p for p in parts if p is not host and self._pname(p) == self.nickname),
                            None) or next((p for p in parts if p is not host
                                           and getattr(p, "connected", False)), None)
                # A reused physical interface can still carry an older
                # link-local address from its baseline.  In that path the LDN
                # participant record is authoritative; a freshly-created VIF
                # retains the historical kernel-address-first behavior.
                self.our_ip = self._resolve_our_ip(ours)
                self.our_mac = ((bytes(ours.mac_address) if ours else None)
                                or self._iface_mac() or b"\x00" * 6)
                self.broadcast = self.our_ip.rsplit(".", 1)[0] + ".255"
                self.log(f"[live] joined ssid={self.ssid.hex()} "
                         f"us={self.our_ip}/{self.our_mac.hex()} "
                         f"host={self.host_ip}/{self.host_mac.hex()}")
                self.info("Joined.")
                self._ready.set()
                await self._wait_for_ldn_stop_or_disconnect(ldn, network, stop_event)

        async def main():
            async with trio.open_nursery() as nursery:
                async def watch_stop():
                    while not stop_event.is_set():
                        await trio.sleep(0.05)
                    # Once the joined session has published readiness, let its
                    # 200 ms loop return normally.  Cancelling this whole
                    # nursery here also cancels the nl80211/route receiver
                    # tasks needed by Station.connect's disconnect and final
                    # administrative DOWN cleanup.  Pre-ready scan/join hangs
                    # still require cancellation to make stop() bounded.
                    if (
                        connection_entered
                        and self._ready.is_set()
                        and self._err is None
                    ):
                        return
                    nursery.cancel_scope.cancel()

                nursery.start_soon(watch_stop)
                await session_main()
                nursery.cancel_scope.cancel()

        try:
            trio.run(main)
        except BaseException as e:                     # pragma: no cover
            if (
                self._single_interface_active
                and connection_started
                and not connection_entered
                and attempted_ssid is not None
            ):
                self._attempt_failed_ssid = attempted_ssid
            silent_type = (
                getattr(ldn_single, "NoMonitorFramesError", None)
                if ldn_single is not None
                else None
            )
            self._monitor_receive_silent = bool(
                silent_type is not None
                and any(
                    isinstance(leaf, silent_type)
                    for _depth, leaf in _flatten_exc(e)
                )
            )
            unsupported_type = (
                getattr(ldn_single, "UnsupportedLDNControlPortDriverError", None)
                if ldn_single is not None
                else None
            )
            if unsupported_type is not None and any(
                isinstance(leaf, unsupported_type)
                for _depth, leaf in _flatten_exc(e)
            ):
                # A missing custom-control-port implementation is a permanent
                # driver capability mismatch.  More scans cannot fix it and
                # would only mutate the user's radio repeatedly.
                self._join_retryable = False
            # trio wraps nursery failures in a (Base)ExceptionGroup whose str() is the useless
            # "Exceptions from Trio nursery (N sub-exceptions)"; unwrap to the real leaf cause(s).
            # Carry the real worker cause across the thread boundary. The
            # formatted string alone loses code sites, errno and group leaves.
            self._join_exception = e
            self._err = _format_join_error(e)
            self.log(f"[live] LDN join FAILED:\n{self._err}")
        finally:
            self._ready.set()

    def _raise_if_ldn_disconnected(self):
        if self._ldn_link_error is not None:
            raise ConnectionError(self._ldn_link_error)

    def _record_ldn_disconnect(self, detail):
        # Linearize link loss against sendto, including the diagnostic socket.
        # An already-in-flight send may finish, but none can start after loss
        # has been recorded. This is not an automatic reconnect/retry policy.
        with self._ldn_link_lock:
            if self._ldn_link_error is None:
                self._ldn_link_error = f"LDN connection lost: {detail}"
                self._ldn_disconnect_events += 1
                self.log(
                    "[diag] LDN terminal disconnect observed "
                    f"event_count={self._ldn_disconnect_events} detail={detail} "
                    "send_gate=closed retryable=false"
                )
                self._join_retryable = False
        self._raise_if_ldn_disconnected()

    async def _wait_for_ldn_stop_or_disconnect(self, ldn, network, stop_event):
        """Consume joined-network loss before any further Pia sends.

        Native JoinMesh checks LocalFacade's connection result before both
        SendJoinRequest and WaitJoinResponse (main+0x240FB3C/0x241012C;
        LocalFacade slot 0x108 -> 0x23D176C). ldn 0.0.17 reports DEL_STATION
        and proprietary disconnects as DisconnectEvent, not exceptions.
        Consume that terminal event rather than inferring loss from a timer,
        RSSI/CQM warning or transient participant snapshot. Native raw LDN
        status and its facade transition guards have no direct Linux-CQM
        equivalent; do not invent a new early-disconnect policy here.
        """
        import trio

        async def watch_events():
            while True:
                event = await network.next_event()
                if isinstance(event, ldn.DisconnectEvent):
                    self._record_ldn_disconnect(f"DisconnectEvent reason={event.reason}")

        async with trio.open_nursery() as nursery:
            nursery.start_soon(watch_events)
            while not stop_event.is_set():
                await trio.sleep(0.2)
            # Cancel only this event watcher. Leave upstream nl80211 receivers
            # alive while the surrounding connection context cleans up.
            nursery.cancel_scope.cancel()

    @staticmethod
    def _pname(p):
        try:
            return p.name.decode("utf-8", "replace").rstrip("\0")
        except Exception:
            return ""

    def _resolve_our_ip(self, participant):
        participant_ip = (
            getattr(participant, "ip_address", None)
            if participant is not None else None
        )
        iface_ip = self._iface_ip()
        fallback = self.host_ip.rsplit(".", 1)[0] + ".2"
        if self._single_interface_active:
            return participant_ip or iface_ip or fallback
        return iface_ip or participant_ip or fallback

    def _iface_mac(self):
        """Read the station interface's MAC as a last-resort fallback for our connection GUID."""
        try:
            with open(f"/sys/class/net/{self._session_ifname}/address") as f:
                return bytes.fromhex(f.read().strip().replace(":", ""))
        except OSError:
            return None

    def _iface_ip(self):
        """Read the IPv4 the ldn lib actually assigned to the station iface (ground truth)."""
        try:
            out = subprocess.check_output(["ip", "-4", "-o", "addr", "show", "dev", self._session_ifname],
                                          text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                parts = line.split()
                if "inet" in parts:
                    ip = parts[parts.index("inet") + 1].split("/")[0]
                    if ip.startswith("169.254."):
                        return ip
        except Exception:
            pass
        return None

    def _validated_host_neighbor(self):
        """Validate the authoritative LDN endpoint before changing the kernel."""
        if (
            not isinstance(self.iface, str)
            or not self.iface
            or "\0" in self.iface
            or self.iface != self._session_ifname
        ):
            raise RuntimeError(
                "LDN session interface does not match the joined interface"
            )
        try:
            host_ip = ipaddress.IPv4Address(self.host_ip)
            our_ip = ipaddress.IPv4Address(self.our_ip)
            broadcast = ipaddress.IPv4Address(self.broadcast)
        except (ipaddress.AddressValueError, TypeError) as exc:
            raise RuntimeError(
                "LDN host/participant/broadcast IPv4 metadata is invalid"
            ) from exc
        network = ipaddress.IPv4Network(f"{our_ip}/24", strict=False)
        if (
            not host_ip.is_link_local
            or not our_ip.is_link_local
            or host_ip not in network
            or host_ip in {our_ip, network.network_address, network.broadcast_address}
            or broadcast != network.broadcast_address
        ):
            raise RuntimeError(
                "LDN host IPv4 address is not a distinct unicast peer in the "
                "joined link-local /24"
            )
        if not isinstance(self.host_mac, (bytes, bytearray, memoryview)):
            raise RuntimeError("LDN host MAC address is invalid")
        host_mac = bytes(self.host_mac)
        if (
            len(host_mac) != 6
            or host_mac == b"\0" * 6
            or host_mac[0] & 1
        ):
            raise RuntimeError("LDN host MAC address is not a unicast MAC")
        if not isinstance(self.our_mac, (bytes, bytearray, memoryview)):
            raise RuntimeError("LDN participant MAC address is invalid")
        our_mac = bytes(self.our_mac)
        if (
            len(our_mac) != 6
            or our_mac == b"\0" * 6
            or our_mac[0] & 1
            or our_mac == host_mac
        ):
            raise RuntimeError(
                "LDN host MAC address is not distinct from a valid participant MAC"
            )
        return self.iface, str(host_ip), host_mac

    @staticmethod
    def _neighbor_replace_command(state):
        command = ["ip", "-4", "neigh", "replace", state["destination"]]
        if state["lladdr"] is not None:
            command.extend(["lladdr", state["lladdr"].hex(":")])
        command.extend(["nud", state["state"].lower(), "dev", state["iface"]])
        return command

    def _install_host_neighbor(self):
        """Install and verify the static LDN host mapping, retaining rollback."""
        if self._host_neighbor_restore_lease is not None:
            raise RuntimeError("a static LDN host-neighbor lease is already active")
        iface, host_ip, host_mac = self._validated_host_neighbor()
        original = interface_ipv4_neighbor_state(iface, host_ip)
        lease = {"iface": iface, "host_ip": host_ip, "original": original}
        # Publish the rollback capability before the first mutation.  Any
        # command or verification failure is then handled by start()->stop().
        self._host_neighbor_restore_lease = lease
        expected = {
            "destination": host_ip,
            "iface": iface,
            "lladdr": host_mac,
            "state": "PERMANENT",
        }
        try:
            result = _run(self._neighbor_replace_command(expected))
        except BaseException as exc:
            raise RuntimeError(
                f"could not install static LDN host neighbor on {iface}"
            ) from exc
        if getattr(result, "returncode", 0) != 0:
            raise RuntimeError(
                f"could not install static LDN host neighbor on {iface}"
            )
        actual = interface_ipv4_neighbor_state(iface, host_ip)
        if actual != expected:
            raise RuntimeError(
                f"kernel did not retain the static LDN host neighbor on {iface}"
            )
        verify_direct_ipv4_route(iface, host_ip, self.our_ip)
        self.log(
            "[diag] installed static LDN host neighbor "
            f"{host_ip} -> {host_mac.hex(':')} on {iface} (verified PERMANENT)"
        )
        self.log(
            f"[diag] verified direct LDN route {self.our_ip} -> {host_ip} "
            f"on {iface}"
        )

    def _restore_host_neighbor(self):
        """Restore exactly the entry replaced by :meth:`_install_host_neighbor`."""
        lease = self._host_neighbor_restore_lease
        if lease is None:
            return
        iface = lease["iface"]
        host_ip = lease["host_ip"]
        original = lease["original"]
        current = interface_ipv4_neighbor_state(iface, host_ip)
        command_error = None
        if current != original:
            command = (
                ["ip", "-4", "neigh", "del", host_ip, "dev", iface]
                if original is None
                else self._neighbor_replace_command(original)
            )
            try:
                result = _run(command)
                if getattr(result, "returncode", 0) != 0:
                    command_error = RuntimeError("ip neigh returned failure")
            except BaseException as exc:
                command_error = exc
        try:
            restored = interface_ipv4_neighbor_state(iface, host_ip)
        except BaseException as exc:
            raise RuntimeError(
                f"could not verify restoration of IPv4 neighbor {host_ip} on {iface}"
            ) from (command_error or exc)
        if restored != original:
            raise RuntimeError(
                f"IPv4 neighbor {host_ip} on {iface} was not restored"
            ) from command_error
        self._host_neighbor_restore_lease = None
        action = "removed" if original is None else "restored"
        self.log(f"[live] {action} temporary LDN host neighbor on {iface}")

    def _setup_sockets(self):
        if not isinstance(self.iface, str) or not self.iface:
            raise RuntimeError("LDN session interface is unavailable for UDP TX")
        if not isinstance(self.our_ip, str) or not self.our_ip:
            raise RuntimeError("LDN participant IPv4 address is unavailable for UDP TX")

        tx = None
        rx = None
        try:
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # Pia's AES-GCM nonce includes the source IPv4 address.  A wildcard
            # bind on a multi-homed Linux/WSL host can therefore create a valid
            # UDP packet which the Switch cannot authenticate.  Pin both the
            # device and source address; any inability to do so is fatal.
            try:
                tx.setsockopt(
                    socket.SOL_SOCKET,
                    SO_BINDTODEVICE,
                    os.fsencode(self.iface) + b"\0",
                )
            except OSError as exc:
                raise RuntimeError(
                    f"could not pin LDN UDP TX to interface {self.iface}"
                ) from exc
            try:
                tx.bind((self.our_ip, PIA_PORT))
            except OSError as exc:
                raise RuntimeError(
                    "could not bind LDN UDP TX to its assigned source endpoint "
                    f"{self.our_ip}:{PIA_PORT}"
                ) from exc
            bound = tx.getsockname()
            if (
                not isinstance(bound, tuple)
                or len(bound) < 2
                or bound[0] != self.our_ip
                or bound[1] != PIA_PORT
            ):
                raise RuntimeError(
                    "kernel did not keep the requested LDN UDP source endpoint"
                )

            # Linux's transmit tap (dev_queue_xmit_nit) is delivered through
            # ptype_all.  An ETH_P_IP packet socket receives inbound IPv4 but
            # can miss every PACKET_OUTGOING clone, making successful sends
            # look as if they never reached the device.  Subscribe to all
            # Ethernet protocols and retain the strict IPv4/UDP/PIA parsing in
            # recv(); non-IPv4 traffic is discarded before the protocol path.
            rx = socket.socket(
                socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL)
            )
            rx.bind((self.iface, 0))
            rx.setblocking(False)
            # Grow the kernel receive buffer so a burst of host frames between our ~60Hz recv() drains is
            # not dropped at the OS level (AF_PACKET ring overflow shows up as silent gaps in the reliable
            # stream -> recovery work; cutting OS drops cuts the recovery we depend on). Best-effort: the
            # kernel clamps to net.core.rmem_max, so log what we actually got. 8 MiB request.
            try:
                rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
                got = rx.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
                self.log(f"[live] rx socket SO_RCVBUF = {got} bytes "
                         f"(raise net.core.rmem_max if lower than requested 8 MiB)")
            except OSError as e:
                self.log(f"[live] could not enlarge rx SO_RCVBUF: {e}")
        except BaseException:
            # Socket setup is transactional.  In particular, never fall back
            # to wildcard UDP transmission after SO_BINDTODEVICE fails.
            for opened in (tx, rx):
                try:
                    if opened is not None:
                        opened.close()
                except OSError:
                    pass
            self._tx = None
            self._rx = None
            raise

        self._tx = tx
        self._rx = rx
        self.log(
            f"[diag] UDP TX pinned to {self.iface} at {self.our_ip}:{PIA_PORT}"
        )

    def _stage_egress_probe(
        self, datagram, destination, *, source_port=PIA_PORT
    ):
        wire = bytes(datagram)
        with self._tx_diag_lock:
            self._tx_probe_sequence += 1
            record = {
                "sequence": self._tx_probe_sequence,
                "payload": wire,
                "destination": destination,
                "source_port": source_port,
                "digest": hashlib.sha256(wire).hexdigest()[:16],
            }
            if len(self._tx_pending_egress) >= MAX_PENDING_EGRESS_PROBES:
                self._tx_pending_egress.pop(0)
                self._tx_egress_evicted += 1
            self._tx_pending_egress.append(record)
        return wire, record

    def _discard_egress_probe(self, record):
        with self._tx_diag_lock:
            for index, candidate in enumerate(self._tx_pending_egress):
                if candidate is record:
                    self._tx_pending_egress.pop(index)
                    break

    def _log_egress_diagnostic(self, message):
        # Observability must never become a new failure in the live data plane.
        try:
            self.log(message)
        except BaseException:
            pass

    def _observe_outgoing_udp(
        self, payload, src_ip, src_port, dst_ip, dst_port, ethernet_destination
    ):
        """Correlate one AF_PACKET PACKET_OUTGOING copy with a successful send.

        This proves that the exact encrypted UDP payload reached the kernel's
        interface egress path.  It deliberately does not claim RF transmission
        or an 802.11 acknowledgement, which require driver/air instrumentation.
        """

        wire = bytes(payload)
        with self._tx_diag_lock:
            match_index = next(
                (
                    index
                    for index, candidate in enumerate(self._tx_pending_egress)
                    if candidate["payload"] == wire
                ),
                None,
            )
            if match_index is None:
                self._tx_egress_unmatched += 1
                unmatched = self._tx_egress_unmatched
                record = None
            else:
                record = self._tx_pending_egress.pop(match_index)
                unmatched = 0

        if record is None:
            if unmatched <= 10:
                digest = hashlib.sha256(wire).hexdigest()[:16]
                self._log_egress_diagnostic(
                    "[diag] unmatched kernel UDP egress "
                    f"sha256={digest} len={len(wire)}"
                )
            return

        mismatches = []
        if src_ip != self.our_ip:
            mismatches.append(f"source {src_ip} != {self.our_ip}")
        expected_source_port = record["source_port"]
        if src_port != expected_source_port:
            mismatches.append(
                f"source port {src_port} != {expected_source_port}"
            )
        if dst_ip != record["destination"]:
            mismatches.append(
                f"destination {dst_ip} != {record['destination']}"
            )
        if dst_port != PIA_PORT:
            mismatches.append(f"destination port {dst_port} != {PIA_PORT}")
        expected_mac = None
        if record["destination"] == self.host_ip:
            try:
                candidate_mac = bytes(self.host_mac)
            except (TypeError, ValueError):
                candidate_mac = b""
            if len(candidate_mac) == 6:
                expected_mac = candidate_mac
        elif record["destination"] in (self.broadcast, "255.255.255.255"):
            expected_mac = b"\xff" * 6
        if expected_mac is not None and ethernet_destination != expected_mac:
            actual = (
                ethernet_destination.hex(":")
                if isinstance(ethernet_destination, bytes)
                else "unavailable"
            )
            mismatches.append(
                f"ethernet destination {actual} != {expected_mac.hex(':')}"
            )

        with self._tx_diag_lock:
            if mismatches:
                self._tx_egress_mismatched += 1
                detail_number = self._tx_egress_mismatched
            else:
                self._tx_egress_matched += 1
                detail_number = self._tx_egress_matched
        prefix = (
            "kernel UDP egress mismatch"
            if mismatches
            else "kernel UDP egress matched"
        )
        if detail_number <= MAX_EGRESS_DETAIL_LOGS:
            suffix = f" ({'; '.join(mismatches)})" if mismatches else ""
            self._log_egress_diagnostic(
                f"[diag] {prefix} send#{record['sequence']} "
                f"sha256={record['digest']} len={len(wire)}{suffix}"
            )

    @property
    def tx_egress_diagnostics(self):
        """Return a small snapshot suitable for tests and post-run reporting."""

        with self._tx_diag_lock:
            return {
                "successful_sends": self._tx_successful_sends,
                "matched": self._tx_egress_matched,
                "mismatched": self._tx_egress_mismatched,
                "unmatched": self._tx_egress_unmatched,
                "pending": len(self._tx_pending_egress),
                "evicted": self._tx_egress_evicted,
            }

    @property
    def last_receive_ports(self):
        """Destination UDP ports aligned with the most recent ``recv`` batch."""

        return tuple(self._last_receive_ports)

    # -- data plane ----------------------------------------------------------
    def open_handshake_diagnostic_source_port(
        self, source_port=PIA_DIAGNOSTIC_SOURCE_PORT
    ):
        """Bind the one alternate UDP endpoint used by a handshake-only oracle.

        The primary :12345 socket remains open for Net establishment.  The
        alternate socket exists only so one Session Join can be compared
        against the same deliberately rejected Join sent from the existing
        endpoint.  It is not used by normal handshake or trade traffic.
        """

        if source_port != PIA_DIAGNOSTIC_SOURCE_PORT:
            raise ValueError(
                "handshake diagnostic source port must be "
                f"{PIA_DIAGNOSTIC_SOURCE_PORT}"
            )
        if self._tx is None or not self.iface or not self.our_ip:
            raise RuntimeError(
                "primary LDN UDP endpoint must be ready before opening the "
                "handshake diagnostic endpoint"
            )
        existing = self._diagnostic_tx.get(source_port)
        if existing is not None:
            return

        diagnostic = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            diagnostic.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            diagnostic.setsockopt(
                socket.SOL_SOCKET,
                SO_BINDTODEVICE,
                os.fsencode(self.iface) + b"\0",
            )
            diagnostic.bind((self.our_ip, source_port))
            bound = diagnostic.getsockname()
            if (
                not isinstance(bound, tuple)
                or len(bound) < 2
                or bound[0] != self.our_ip
                or bound[1] != source_port
            ):
                raise RuntimeError(
                    "kernel did not keep the requested handshake diagnostic "
                    "UDP source endpoint"
                )
        except BaseException:
            try:
                diagnostic.close()
            except OSError:
                pass
            raise

        self._diagnostic_tx[source_port] = diagnostic
        self._accepted_udp_ports.add(source_port)
        self.log(
            "[diag] handshake-only UDP endpoint pinned to "
            f"{self.iface} at {self.our_ip}:{source_port}"
        )

    def send_from_handshake_diagnostic_port(
        self,
        datagram,
        dst_ip,
        source_port=PIA_DIAGNOSTIC_SOURCE_PORT,
    ):
        """Send one diagnostic datagram from the explicitly opened endpoint."""

        self._raise_if_ldn_disconnected()
        tx = self._diagnostic_tx.get(source_port)
        if tx is None:
            raise RuntimeError(
                "handshake diagnostic UDP endpoint has not been opened"
            )
        dst = (
            self.broadcast
            if dst_ip in (self.broadcast, "255.255.255.255")
            else dst_ip
        )
        wire, probe = self._stage_egress_probe(
            datagram, dst, source_port=source_port
        )
        try:
            with self._ldn_link_lock:
                self._raise_if_ldn_disconnected()
                sent = tx.sendto(wire, (dst, PIA_PORT))
            if sent != len(wire):
                raise OSError(
                    f"short UDP send: wrote {sent} of {len(wire)} bytes"
                )
        except BaseException as exc:
            self._discard_egress_probe(probe)
            if isinstance(exc, OSError):
                try:
                    self.log(f"[live] diagnostic sendto failed: {exc}")
                except BaseException:
                    pass
            raise
        with self._tx_diag_lock:
            self._tx_successful_sends += 1

    def send(self, datagram, dst_ip):
        self._raise_if_ldn_disconnected()
        dst = self.broadcast if dst_ip in (self.broadcast, "255.255.255.255") else dst_ip
        wire, probe = self._stage_egress_probe(datagram, dst)
        try:
            with self._ldn_link_lock:
                self._raise_if_ldn_disconnected()
                sent = self._tx.sendto(wire, (dst, PIA_PORT))
            if sent != len(wire):
                raise OSError(
                    f"short UDP send: wrote {sent} of {len(wire)} bytes"
                )
        except BaseException as e:
            self._discard_egress_probe(probe)
            if isinstance(e, OSError):
                try:
                    self.log(f"[live] sendto failed: {e}")
                except BaseException:
                    pass
            raise
        with self._tx_diag_lock:
            self._tx_successful_sends += 1

    def _accept_dst(self, dst_ip):
        """The host INITIATES by BROADCASTING its Net 0x11 to the subnet .255 (the reference capture seq 1 ->
        169.254.X.255), then unicasts to us. Accept our own IP, ANY 169.254.*.255 link-local
        broadcast (robust to imperfect subnet resolution), and the global broadcasts - so we never
        miss the host's broadcast outreach."""
        return (dst_ip == self.our_ip
                or (dst_ip.startswith("169.254.") and dst_ip.endswith(".255"))
                or dst_ip in ("255.255.255.255",))

    def recv(self):
        self._raise_if_ldn_disconnected()
        out = []
        self._last_receive_ports = []
        if self._rx is None:
            return out
        # Always yield control to the caller after a bounded amount of work so
        # cancellation/radio cleanup cannot be starved by a sustained flood.
        for _frame_index in range(MAX_LIVE_RECV_FRAMES):
            try:
                recvfrom = getattr(self._rx, "recvfrom", None)
                if callable(recvfrom):
                    data, packet_address = recvfrom(65535)
                else:
                    # Preserve compatibility with narrow test doubles and old
                    # socket adapters.  Production AF_PACKET sockets always
                    # provide recvfrom(), which is required for egress proof.
                    data = self._rx.recv(65535)
                    packet_address = None
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                try:
                    self.log(f"[live] raw receive failed: {e}")
                except BaseException:
                    pass
                raise
            parsed = self._parse_udp(data)
            if parsed is None:
                continue
            src_ip, src_port, dst_ip, dst_port, payload = parsed
            packet_type = (
                packet_address[2]
                if isinstance(packet_address, tuple) and len(packet_address) >= 3
                else None
            )
            if packet_type == PACKET_OUTGOING:
                # AF_PACKET provides a copy after IP routing and link-layer
                # resolution.  Match only Pia UDP; all outgoing frames remain
                # excluded from the incoming protocol path.
                if (
                    src_port in self._accepted_udp_ports
                    and dst_port == PIA_PORT
                ):
                    ethernet_destination = (
                        bytes(data[:6]) if len(data) >= 6 else None
                    )
                    self._observe_outgoing_udp(
                        payload,
                        src_ip,
                        src_port,
                        dst_ip,
                        dst_port,
                        ethernet_destination,
                    )
                continue
            if (
                src_ip == self.our_ip
                or dst_port not in self._accepted_udp_ports
                or not self._accept_dst(dst_ip)
            ):
                continue
            if len(payload) > self.max_datagram_size:
                continue
            self._rx_seen += 1
            if self._rx_seen <= 10:
                self.log(f"[live] RX #{self._rx_seen}: {src_ip} -> {dst_ip}:{dst_port} "
                         f"len={len(payload)} {payload[:4].hex()}")
            out.append((payload, src_ip))
            self._last_receive_ports.append(dst_port)
        return out

    @staticmethod
    def _parse_udp(frame):
        if len(frame) < 14 + 20 + 8 or struct.unpack_from("!H", frame, 12)[0] != ETH_P_IP:
            return None
        ip = frame[14:]
        if (ip[0] >> 4) != 4 or ip[9] != PROTO_UDP:
            return None
        ihl = (ip[0] & 0x0F) * 4
        src_ip = socket.inet_ntoa(ip[12:16])
        dst_ip = socket.inet_ntoa(ip[16:20])
        udp = ip[ihl:]
        if len(udp) < 8:
            return None
        src_port, dst_port, ulen = struct.unpack_from("!HHH", udp, 0)
        payload = udp[8:][:max(0, ulen - 8)] if ulen >= 8 else udp[8:]
        return src_ip, src_port, dst_ip, dst_port, payload

    def stop(self):
        self._signal_worker_stop()
        egress = self.tx_egress_diagnostics
        with self._tx_diag_lock:
            log_egress_summary = (
                bool(egress["successful_sends"])
                and not self._tx_egress_summary_logged
            )
            if log_egress_summary:
                self._tx_egress_summary_logged = True
        if log_egress_summary:
            self._log_egress_diagnostic(
                "[diag] UDP kernel-egress observations before shutdown: "
                f"sends={egress['successful_sends']} "
                f"matched={egress['matched']} "
                f"mismatched={egress['mismatched']} "
                f"unmatched={egress['unmatched']} "
                f"pending={egress['pending']} "
                f"evicted={egress['evicted']}"
            )
        diagnostic_sockets = tuple(self._diagnostic_tx.values())
        self._diagnostic_tx = {}
        self._accepted_udp_ports = {PIA_PORT}
        for s in (self._tx, *diagnostic_sockets, self._rx):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self._join_worker_or_raise()
        # Remove LDN virtual interfaces before restoring the host interface.
        # The keepalive thread has a bounded join, so restoring managed/up
        # state first can race a still-present LDN vif on the same PHY.
        self._teardown_radio()
