# Wireless adapter and configuration evidence

**English** | [日本語](WIRELESS_COMPATIBILITY.md) · [Setup](USER_GUIDE_EN.md)

Last physical observation: 2026-10-10 (not verification of a newer release).

## AWUS036ACH is the reference hardware, not an unconditional guarantee

**Reference hardware: AWUS036ACH / RTL8812AU / `rtw88_8812au`. Physical trade/save with this distribution is unverified.** Development trade success and the helper-free 0.8.6 handshake are historical, configuration-specific observations; see [conditions and evidence](TESTING.md).

The optional [BSS helper](BSS_HELPER_EN.md) is included, but is neither a driver replacement nor a compatibility guarantee. Other adapters remain unverified. Monitor support alone is insufficient; offline PA9 tools need no radio or keys.

| Adapter / chipset | Linux driver | Evidence | This distribution |
|---|---|---|---|
| ALFA **AWUS036ACH** / RTL8812AU | `rtw88_8812au` | Physical trade/save in the recorded development setup; 0.8.6 helper-free full handshake, normal shutdown and recovery | Reference hardware; physical trade/save with this distribution unverified |
| The same AWUS036ACH | Legacy `88XXau` / `8812au` / `rtl8812au` / `rtl88xxau` | Required custom control-port support was missing | This legacy configuration cannot authenticate LDN |
| ALFA **AWUS036ACHM** / MT7610U | `mt76x0u` | Upstream FRLG demo/high-reliability reports only | Unverified here; a different product from ACH |
| Realtek RTL8821CE / PCIe | `rtw88_8821ce` | Upstream FRLG high-reliability report only | Unverified; no guarantee |
| AMD RZ616 / M.2 | `mt7921e` | Upstream FRLG latency/stall reports | Unverified; no guarantee |
| Intel AX200 / M.2 | `iwlwifi` | Upstream FRLG IP-assignment problems | Unverified; no guarantee |
| Atheros AR9271 / USB | `ath9k_htc` | Upstream FRLG IP-assignment problems | Unverified; no guarantee |
| Other adapters | Various | Brand, Wi-Fi generation or monitor support is insufficient | Unverified; no guarantee |

Different hardware revisions, drivers, firmware and kernels are different configurations. Use in a past successful setup does not establish that the helper is always required; a matching USB ID alone is not a compatibility proof.

Pinned reference: [FRLG adapter reports at 13809c21](https://github.com/tornadus/frlg-ldn-trade/blob/13809c21b6e992097f98453b7cbc9e2bc30bbf7c/README.md#tested-wifi-cards). These concern **a different game**, not this project's Z-A validation or a purchasing recommendation.

## Required capabilities

This client **joins** a Switch LDN network. It is not a home-router connection, Bluetooth service or Switch Online client.

- Linux with the matching driver/firmware, Python 3.12+, live dependencies and a privileged wireless backend.
- Both **managed** and **monitor** modes.
- Correct 802.11 action-frame reception/transmission. Advertised flags alone cannot establish this.
- Authentication/association, **CCMP** and custom control-port **EtherType `0x88B7`**.
- **`CONTROL_PORT_OVER_NL80211`**, independently of custom EtherType support. Both are required.
- Usable **2.4 GHz channels 1, 6 and 11**, within local regulatory limits. Standard discovery does not support 5/6 GHz-only setups.
- A dedicated PHY not carrying Internet/SSH or another active connection. Virtual interfaces do not make one radio independently operate on arbitrary channels.

The normal path creates temporary monitor/station interfaces. Supported drivers may reuse an existing sole netdev with `--single-interface`. This is an interface strategy within **one physical radio**, not a requirement for two USB adapters. AP mode and a two-radio host configuration are not client prerequisites.

Native Linux PCIe/M.2 adapters are not categorically excluded. A dedicated USB adapter plus wired Internet is often easier to isolate. Do not fake a country code, disable regulations or remove required CONNECT attributes to bypass a failed check.

## Windows / WSL2

- The GUI and PA9 editor run without a radio. Live communication uses Linux/WSL2 Relay.
- WSL `eth0` / virtual Ethernet cannot expose the Windows Wi-Fi adapter's raw `nl80211` capabilities.
- A dedicated USB Wi-Fi adapter must be forwarded with `usbipd-win`, with its actual Linux driver/firmware present in the WSL kernel. USB enumeration does not itself prove a PHY exists.
- Built-in PCIe/M.2 Wi-Fi cannot normally be forwarded by USB tooling. Use native Linux and an SSH-forwarded loopback relay instead.
- The forwarded adapter cannot simultaneously serve Windows. Updating WSL is not a guarantee of driver support.
- Run radio diagnostics on the **backend machine**. A Windows GUI check does not inspect a remote relay's hardware.

See [setup instructions](USER_GUIDE_EN.md) and [Microsoft's USB documentation](https://learn.microsoft.com/windows/wsl/connect-usb). This version has not been execution-tested on WSL hardware.

## Read-only preflight

Run from the extracted Linux source folder after [setup](USER_GUIDE_EN.md#install).

```sh
.venv-za/bin/python zatrade.py doctor --language en
.venv-za/bin/python zatrade.py doctor --backend direct --radio --language en
.venv-za/bin/python zatrade.py doctor --backend direct --radio --phy phy1 --language en --json
```

A wheel installation uses `.venv-za/bin/za-ldn-trade` instead of `.venv-za/bin/python zatrade.py`. Replace `phy1` with your current dedicated PHY. The GUI exposes the same check under **Check environment → Inspect local radio**.

Normal `doctor` lists dependencies/sysfs only. `--radio` additionally uses read-only `iw`, route/address/sysfs queries and bounded local netlink **GETFAMILY/GET_WIPHY** requests. `iw` does not display the custom EtherType flag, so it is queried separately.

**No scan, RF transmission, join, key loading, driver installation, rfkill changes, disconnection or network-setting mutation occurs. No sudo is needed.** Multiple PHYs are not auto-selected. Unknown, contradictory or changing inventory is not a success. MAC/SSID/IP values, USB serials and keys are not included in the diagnostic result.

| Exit | Meaning |
|---|---|
| `0` | Required flags, live dependencies and read-only dedicated-state checks present; **not physical success** |
| `2` | Missing capabilities/dependencies/device, busy or blocked adapter, or non-Linux backend |
| `3` | Unknown, unreadable or ambiguous selection |

`requirements_present_not_verified` means exactly that. The read-only check always reports physical action-frame, connection and trade validation as false. It does not replace execution-time ownership/restoration checks.

## Reporting verified support

Name the adapter, driver, application version and connection mode, and follow the [physical verification stages](TESTING.md#before-calling-a-configuration-physically-verified) with the distributed artifacts. Separate connection-only tests from irreversible trades; incomplete configurations remain experimental/unverified.

References: [LDN requirements/design](https://github.com/kinnay/LDN/blob/6ee37bf0fa9f7ab1baa0c67e696ed28b9e2cb120/README.md), [Linux nl80211](https://docs.kernel.org/networking/netlink_spec/nl80211.html).
