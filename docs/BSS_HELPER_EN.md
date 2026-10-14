# Optional AWUS036ACH connection helper (experimental)

**English** | [日本語](BSS_HELPER.md)

`ldn_bss_seed` is an optional kernel module that supplies missing network information on Linux. Build the bundled C source for the radio-owning PC; it does not replace the Wi-Fi driver.

**Target: Linux, AWUS036ACH / RTL8812AU with `rtw88_8812au`.** Not ACHM / `mt76x0u`, and not legacy `88XXau`. For Windows clients, build/configure it on the Linux/WSL machine that owns the radio. USB passthrough and a suitable WSL kernel remain separate requirements.

## Purpose

The helper constructs a Probe Response from the selected room's BSSID, SSID and channel and passes it to the local mac80211 receive path to populate a temporary BSS entry. That response is not transmitted over the air; the surrounding normal Wi-Fi scan and connection still transmit. It does not replace keys, skip authentication, or implement the game trade/save protocol.

It is unnecessary when the normal connection works. An existing exact BSSID/SSID/frequency cache entry is used without loading the module. There is no automatic fallback from the normal path: the user must opt in.

## Build a local bundle on the radio-owning PC

1. Prepare Python using `sh scripts/setup_za.sh --live` as described in the [user guide](USER_GUIDE_EN.md).
2. Install trusted OS build tools and **headers matching the running kernel**. Compare `uname -r` with `/lib/modules/$(uname -r)/build/include/config/kernel.release`. Ubuntu/Debian commonly uses `build-essential` and `linux-headers-$(uname -r)`; Arch derivatives need their actual running kernel's headers package and make/GCC or Clang/LLD. Clang-built CachyOS kernels require Clang/LLD. Do not substitute newer/different headers.
3. As an ordinary user (no sudo needed to build):

```sh
sh scripts/setup_bss_helper.sh
```

The default destination is `bss-helper/<running-kernel>/` in the extracted application directory. Existing directories are never overwritten; an optional argument selects a different NEW output directory.

For a wheel installation:

```sh
zatrade bss-helper build --output "$HOME/za-bss-$(uname -r)"
zatrade bss-helper check --bundle "$HOME/za-bss-$(uname -r)"
```

The package includes its source; no developer workspace, previous binary, game ROM, keys or save is needed for building. Build/check never download dependencies, install OS packages, load modules, configure DKMS or change autoload settings. Keep `BUILD.json` and `ldn_bss_seed.ko` together. After a kernel update, rebuild into a new directory and select it again.

## Use

In the Linux GUI, choose the dedicated AWUS036ACH from the adapter list, then **Advanced settings → AWUS036ACH connection helper (optional) → Browse** and select the built directory. While selected, the app reuses the existing physical interface. Clear the field to return to the default path; the choice is not persisted across app restarts. Fill the normal keys, room code and trade inputs. Editing PA9 files does not require this helper or administrator access.

The `trade`, `handshake`, `probe` and Linux-side `relay-serve` commands accept `--bss-helper /absolute/bundle/directory`. Choose your own dedicated `--phy` and existing physical `--interface`. This mode rejects `--no-single-interface`. Relay CLIENTS cannot forward this path: configure the helper on the server that owns the radio.

Actual use needs Linux administrator privileges allowing kernel module loading as well as ordinary radio operations. The app never elevates itself. Secure Boot, lockdown or module-signing policy may reject loading; it stops rather than disabling protection or forcing a load. This builder does not sign modules or enroll keys, so signing-required environments are not supported by these instructions alone.

## Ownership, cleanup and evidence limits

- One global module lock; never borrow or unload a previously loaded foreign module.
- Check selected device identity/driver, running kernel, ELF metadata and recorded SHA-256. A locally generated receipt is NOT a third-party signature; build from the included source rather than trusting another person's binary and receipt.
- Subscribe before scanning; insert only after an exact matching scan-start event, then require scan completion and an exact BSSID/SSID/frequency cache entry. Supported channels: 1/6/11.
- One connection attempt per explicit helper session. Stop the communication worker, then non-forcibly unload only the owned module. Cleanup failure retains the lease/recovery state and is not reported as success. Do not immediately start another trade.
- No persistent recovery daemon: forced process termination/power loss is not automatically recovered. A leftover module blocks the next start. An OS administrator must inspect ownership/use before a normal `rmmod ldn_bss_seed`; never remove another process's module.
- **Physical trade/save/recovery through this packaged helper path is unverified.** Historical configurations and connection-only observations are separated in [verification boundaries](TESTING.md).

The C helper is GPL-2.0-only ([license](../licenses/ldn-bss-seed-GPL-2.0.txt)); the Python application is AGPL-3.0-only. Kernel-specific compiled `.ko` files are not included in release archives.
