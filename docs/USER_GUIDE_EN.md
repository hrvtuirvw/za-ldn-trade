# za-ldn-trade — User guide

**English** | [日本語](USER_GUIDE.md) · [Home](../README.md) · [Troubleshooting](TROUBLESHOOTING.md)

Start by creating or editing a PA9 offline. Prepare wireless only if you need it. Opening the app, creating a Pokémon and saving a file do not read console keys or start wireless communication.

[Install](#install) · [First file](#make-your-first-file) · [Language and controls](#language-and-controls) · [Wireless](#wireless-setup) · [Trading](#trade) · [Stopping and results](#stopping-and-checking-the-result)

## Install

- **Offline GUI/CLI:** Python 3.10+. The GUI also needs Tk and a desktop display.
- **Wireless backend:** Linux/Python 3.12+, dedicated compatible Wi-Fi, driver/firmware, `iw`, `ip` (iproute2), `nmcli` (NetworkManager), `sysctl` / `pkill` (procps), and radio privileges.
- Python is not bundled with the ZIP or wheel. First-time dependency installation may need an Internet connection.
- Local runtime verification is Linux/Python 3.14.8. Windows/WSL, other Python runtimes and this release's physical exchange/save/recovery remain unverified. See [verification boundaries](TESTING.md).

### Source ZIP: Linux

Extract the ZIP to a user-owned folder and open a terminal there. Install Python, venv support and Tk through your OS. Debian/Ubuntu commonly use `python3-venv` and `python3-tk`; Arch derivatives use `python` and `tk`.

```sh
sh scripts/setup_za.sh
sh scripts/start_za_gui.sh
```

Setup prepares `.venv-za` and Python dependencies in the extracted folder. It does not install drivers/OS packages or change wireless settings. If Tk is missing, install it through your OS and retry.

### Source ZIP: Windows

Install Python 3.10+ with Tcl/Tk and the Python launcher. In PowerShell, from the extracted folder:

```powershell
py -3 -m venv .venv-za
.\.venv-za\Scripts\python.exe -m pip install -r requirements-za.txt
.\Start-za-ldn-trade.cmd
```

After setup, double-click `Start-za-ldn-trade.cmd`. Offline editing does not need Administrator privileges. These steps do not require changing PowerShell execution policy.

### Wheel

Install the downloaded wheel in its own virtual environment. PyPI publication is not assumed; replace `VERSION` with the actual filename's version. Tk must still be provided by Python/your OS.

Linux (POSIX shell):

```sh
python3 -m venv .venv-za
.venv-za/bin/python -m pip install /path/to/za_ldn_trade-VERSION-py3-none-any.whl
.venv-za/bin/za-ldn-trade doctor --language en
.venv-za/bin/za-ldn-trade-gui
```

Windows (PowerShell):

```powershell
py -3 -m venv .venv-za
.\.venv-za\Scripts\python.exe -m pip install C:\Downloads\za_ldn_trade-VERSION-py3-none-any.whl
.\.venv-za\Scripts\za-ldn-trade.exe doctor --language en
.\.venv-za\Scripts\za-ldn-trade-gui.exe
```

Quote paths containing spaces. For Linux wireless, use Python 3.12+ and install `'/path/to/za_ldn_trade-VERSION-py3-none-any.whl[live]'` instead. Installing extras in a Windows GUI environment does not prepare the Linux backend.

### Which Python to use for CLI commands

Source-ZIP examples run **from the extracted folder** as `.venv-za/bin/python zatrade.py …`. Setup does not activate that environment in your current shell: plain `python` may run a different installation.

| Installation | Command prefix |
|---|---|
| Linux source ZIP | `.venv-za/bin/python zatrade.py` |
| Windows source ZIP | `.\.venv-za\Scripts\python.exe zatrade.py` |
| Linux wheel | `.venv-za/bin/za-ldn-trade` |
| Windows wheel | `.\.venv-za\Scripts\za-ldn-trade.exe` |

Use these equivalents for examples in the [CLI reference (Japanese)](../README_ZA.md).

## Make your first file

1. The initial screen is **PA9 editor**. Select **New…**. No wireless setup is needed.
2. Choose a species/preset, trainer name and Pokémon language. TID/SID are internal 0–65535 values, not the displayed six-digit trainer ID. **Generate new IDs** fills new values only when clicked; it does not recover your in-game identity.
3. Select **Create & edit** and review the record. It is not saved yet.
4. Select the fixed **Save** button. A new document asks for its filename. `.pa9` writes decrypted bytes; `.epa9` / `.ea9` writes encrypted bytes.

Use **Open** for an existing PA9. **Save** validates/applies pending input and writes the file, asking before replacing the original. **Apply** in **Actions** is optional and updates memory only; **Save as…** chooses another output. The tab's `*` marks unsaved changes. Closing or replacing that document offers Save / Discard / Cancel.

- Stored records are 328 bytes; party records are 344 bytes. Ordinary saves preserve format. Party conversion requires explicit confirmation and calculated stats.
- New documents have auto stats on; existing documents open in manual mode. Recalculate or enabling auto stats also fully heals HP/status.
- **Validate** checks checksums, structure and known ranges. It does not certify legality, in-game loading or a physical trade of generated Pokémon.

See the [PA9 guide](PA9_CREATION_EN.md) for presets, IDs and calculations. **Inspect** is also offline, but **recording a capture is a wireless operation**.

## Language and controls

Use **日本語 / English** in the header after finishing an operation or closing its modal dialog. Switching while idle retains pending edits, the document, tabs, logs and connection fields. It does not translate names or change an existing Pokémon's language ID.

Priority: explicit `gui --language`, `ZA_TRADE_LANG`, saved preference, system locale. Non-Japanese system locales default to English. With the source ZIP:

```sh
.venv-za/bin/python zatrade.py gui --language en
.venv-za/bin/python zatrade.py gui --language ja
```

Only display language is saved. The existing settings folder name is retained after the rename so preferences are not lost: `%APPDATA%\za-trade-lab\preferences.json` on Windows; `$XDG_CONFIG_HOME/za-trade-lab/preferences.json` on Linux (`~/.config` by default). Malformed preferences are not silently overwritten. Native file dialogs may use the OS language; technical logs and JSON keys may remain English.

**Ctrl+N** New · **Ctrl+O** Open · **Ctrl+S** Save · **Ctrl+Shift+S** Save as · **Ctrl+L** Language · **F1** Help · **Ctrl+Q** Exit.

Task navigation, language/help, Save/Save as, trade/capture start actions and Stop/Exit remain outside the scrollable content. A compact task dropdown replaces wrapped navigation buttons when needed. **Alt+1–5** selects Trade / PA9 editor / Capture / Inspect / Log; shortcuts do not cross an active modal dialog.

The editor starts with New/Open, then offers one primary Save action for both new and existing documents. **Actions** contains Apply, Validate, Reload, Revert and the advanced-field switch. The basic form shows names and level; raw flags, PID/EC and technical file details are disclosed explicitly. Auto stats and Recalculate remain on the Stats tab. Inspection results are read-only.

Use **Choose…** beside a species, move, item or ability. Search in Japanese/English or by number, select a row, then Select (keyboard: Down, Enter; Escape cancels). Searching never silently selects the first result. Numbers remain available for unknown values. The name catalog is a lookup aid, **not a list of legal moves/items for this Pokémon or title**. Invalid numeric input reveals and focuses the offending field without applying the record.

Only the editor form scrolls; section navigation and document actions stay fixed. Compact windows use a section selector and **File** menu. Find additional commands under **File → Actions**, and calculation/validation details under **Save info**. Ctrl+Tab switches editor sections.

Name lists support **Back / Next** (Alt+Left / Right) through every result, opening at the current value. A new query starts at the first page without automatically confirming a result. Long names have horizontal scrolling.

On small/high-DPI screens, use scrollbars or Tab / Shift+Tab. The wheel scrolls vertically; Shift+wheel scrolls horizontally. Focusing an input reveals it. There is no need to lower OS scaling. Stop and Exit sit outside the main scrollable area.

## Wireless setup

**This is the experimental part of the application.** Read the [hardware matrix](WIRELESS_COMPATIBILITY_EN.md) before buying or connecting an adapter. A dedicated single-radio configuration is supported in principle; two USB adapters are not required. Never use the radio carrying your Internet or SSH connection.

### Use a prepared connection (normal-user GUI)

Run only the radio backend with privileges on Linux and connect from a normal-user GUI. That backend is called **Relay**. Windows uses this arrangement.

On the Linux radio host, prepare Python 3.12+ and the OS tools listed above. From the source ZIP:

```sh
sh scripts/setup_za.sh --live
.venv-za/bin/python zatrade.py doctor --backend direct --radio --language en
```

Select a bootstrap interpreter with `PYTHON=python3.12 sh scripts/setup_za.sh --live`. **An existing venv keeps its original Python.** If rejected, preserve/rename the old environment and recreate it with a supported Python. Doctor does not read keys, scan, connect or change settings. With several PHYs, pass `--phy` using the current inventory, not a guessed number.

After checking prerequisites, use separate terminals. `phy0`, `wlan0` and the key path below are placeholders; this example reuses a supported existing interface.

```sh
# Linux terminal 1: only the radio-owning process runs with privileges.
sudo .venv-za/bin/python -s -B zatrade.py relay-serve \
  --keys /path/to/your/prod.keys --phy phy0 --interface wlan0 --single-interface

# Linux terminal 2: normal-user GUI.
sh scripts/start_za_gui.sh --backend relay --language en
```

For a supported temporary-interface configuration, omit `--single-interface` and use a new temporary interface name instead of the physical one. Selecting Relay in the GUI does not start the backend, elevate privileges or install drivers.

**Use a prepared connection** defaults to `127.0.0.1:24872`; changes are under Advanced settings. Relay accepts IPv4 loopback only and trusts local processes on that OS. Do not run it with untrusted local users or expose it directly to a LAN/Internet. For another Linux machine, forward its loopback port through SSH:

```sh
ssh -N -L 24872:127.0.0.1:24872 USER@LINUX_HOST
```

Keys stay on Linux and are not sent to the GUI. If needed, [build and select the optional BSS helper](BSS_HELPER_EN.md) on the radio host too. Otherwise leave it unset.

### This PC's Wi-Fi (direct Linux connection)

This is the initial Linux view. For a normal-user GUI, switch to the prepared-connection arrangement above. Direct wireless execution needs live dependencies and radio privileges; seeing an adapter in the list is not enough.

1. Connect the dedicated radio and click **Refresh list**. Select **Wi-Fi adapter to use**; no handwritten PHY is needed.
2. **Browse** for keys and optionally open **Check environment → Inspect local radio**. Refreshing/selecting does not start communication.
3. Before starting, the worker rechecks device identity, required features, routes/IP addresses and usage. Unknown, busy or unsupported conditions stop the operation.

Even one radio is not selected automatically, except an unambiguous explicit startup `--phy`. Sorting/language changes retain selection; unplug/replacement clears it. Selection is not saved for the next launch. **In use / Disabled / Not checked** are not compatibility certificates.

Advanced settings contains display name/game version, the selected read-only PHY, temporary-interface name and reuse policy. Reuse **Yes** uses the selected existing interface; **Auto** uses driver-specific defaults. Normally leave these alone. Local inventory/diagnosis **does not inspect a remote relay's radio**.

### Windows / WSL2

WSL virtual Ethernet cannot substitute for Wi-Fi. Forward a dedicated USB adapter and provide its driver/firmware inside WSL. Follow [Microsoft's USB instructions](https://learn.microsoft.com/windows/wsl/connect-usb); USB enumeration alone does not prove that a usable PHY exists.

Administrator PowerShell, with the adapter's actual BUSID:

```powershell
usbipd list
usbipd bind --busid BUSID
usbipd attach --wsl --busid BUSID
```

On Ubuntu 24.04+ WSL, prepare Python 3.12+ and the backend OS tools, then use the source-folder launcher:

```powershell
wsl -d Ubuntu -- sudo apt update
wsl -d Ubuntu -- sudo apt install python3-venv iw iproute2 network-manager procps
.\scripts\start_za_relay_wsl.ps1 -Setup -Keys ~/.switch/prod.keys
```

The launcher performs read-only preflight before radio operations. Use `-Phy phyN` when necessary; follow administrator execution-policy rules, not a blanket policy bypass. USB forwarding makes the adapter unavailable to Windows. Built-in PCIe/M.2 radios cannot normally be passed through with USB tooling.

After preparing Relay, open `Start-za-ldn-trade.cmd` on Windows. Kernel/driver support and reachability of the relay remain separate prerequisites. Windows/WSL execution of this release is unverified.

## Trade

Start with disposable test data, not valuable Pokémon or your only save. Automatic approval is off by default.

1. Open local Link Trade in **Z-A 2.0.2** on the console and wait with an eight-digit Link Code.
2. Enter the same code in the GUI. There is no default code.
3. Select a complete **344-byte party PA9** and a new received-file path. Zero-padding a 328-byte stored record is not a valid substitute.
4. Select **Start trade** and choose/confirm the outgoing Pokémon on the console too.
5. Review the peer's Pokémon in the PC confirmation dialog. The default is **No**. Once approved, the exchange may be irreversible.
6. Wait for save synchronization, PC storage and cleanup; the default timeout is 300 seconds. After the console's trade/saving indicators finish, inspect the received file and game state.

## Stopping and checking the result

**Stop** requests cooperative cancellation. Closing during a job also asks before stopping, then waits for radio restoration. Do not force-kill the process or unplug the adapter to cut cleanup short.

**An uncertain result or a post-FinalAgreement warning means: do not repeat the trade.** A timeout or cancellation does not prove that the console rolled back. Check the console and the received/recovery file named in the result first. A restoration failure also needs inspection of the original state, not another attempt over it.

For startup, external file changes and preferences, see [Troubleshooting](TROUBLESHOOTING.md). Remove addresses, codes, names and private paths from reports. Never attach keys, saves, Pokémon records, ROMs or raw captures.

<details>
<summary>Details: save protection, input limits and local failures</summary>

### Received-file and editor save protection

Prefer a new filename for every trade. New outputs are published with an atomic no-clobber operation; a file that appears after approval is not replaced. Editor and trade writers share per-path locks for the same user/host and temporary-directory configuration. Existing outputs require approval and are rechecked under that lock, including content. Do not concurrently edit an approved existing output from other software, another user/host, or a process with a different temporary-directory setting: advisory locks and identity checks are not a filesystem compare-and-swap against those writers.

If the approved destination cannot be used, the receiver preserves the data under a unique `*.recovered-*` name, or retains its fsynced write-ahead copy if recovery publication also fails. Read the actual path and warning in the result; do not assume the requested filename was used. The GUI presents queued trade results before closing, even if the worker has already finished. A dialog error keeps the result pending rather than silently acknowledging it.

Capture **Stop** checks every bounded input line, including blanks and ignored metadata.

### Input limits and recovery from local failures

PA9 inspection reads at most 344 bytes; offline command input files are limited to 64 KiB (matrix: 1,200 bytes; party PA9: 344 bytes). Save-target fingerprints are limited to 1 MiB. These are not limits on complete capture files: capture lines are limited to 64 KiB, and capture/preference JSON is limited to 64 nested containers. Preferences are limited to 64 KiB. Named pipes/devices and unsafe file replacements are rejected instead of waiting indefinitely during open. Normal symlink inputs remain supported by capture/CLI inspection, but editor targets and preferences reject symlinks.

Malformed language preferences are preserved; startup falls back to the OS language. Changing the display language still works, but an invalid preference file must be repaired or moved aside before the choice can be saved. A failed worker-thread start restores the relevant controls for retry. New operations cannot replace a result still waiting to be shown. Closing cancels this window's pending Tk callbacks.

Regular-file I/O can still block inside the OS; cooperative cancellation is not a hard real-time guarantee. The separate raw-dump `scan` command still reads its full input into memory and can use substantial RAM; it is not the bounded streaming capture analyzer.

The **Log** tab provides bounded diagnostic history. Clear log only clears the display; it neither cancels a worker nor changes a radio. Before reporting an issue, remove addresses, codes, names and private paths. Never upload keys, saves, Pokémon files, ROMs or raw captures.

</details>
