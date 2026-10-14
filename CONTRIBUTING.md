# Contributing

[Home](README.md) · [日本語ホーム](README_JA.md) · [Release checklist](docs/RELEASING.md) · [Security](SECURITY.md)

Issues and pull requests in English or Japanese are welcome. Use synthetic data and keep the offline tools usable without radio hardware, console keys or a personal save.

## Set up a development environment

Use the reviewed public source tree. Python 3.10+ is required; GUI tests need Tk and a desktop display or an owned virtual display. Linux/Python 3.12+ additionally supports the pinned live-stack imports, but the test suite still uses synthetic I/O.

Linux:

```sh
python3 -m venv .venv-dev
.venv-dev/bin/python -m pip install '.[test]' build
.venv-dev/bin/python -m pytest -q -rs -W error::ResourceWarning
```

For a headless Linux test run, with Xvfb installed:

```sh
xvfb-run -a .venv-dev/bin/python -m pytest -q -rs -W error::ResourceWarning
```

Windows (PowerShell; Python with Tcl/Tk):

```powershell
py -3 -m venv .venv-dev
.\.venv-dev\Scripts\python.exe -m pip install '.[test]' build
.\.venv-dev\Scripts\python.exe -m pytest -q -rs -W error::ResourceWarning
```

These commands name the venv explicitly; no activation or execution-policy change is needed. A skipped GUI test is not GUI verification. Record the OS, Python version, passes, skips and subtests; the authored CI matrix is not evidence that every platform has already passed.

## Keep changes focused

- `zasim` owns the Z-A UI, PA9, title protocol and relay. `zasim/transport_setup.py` is the single title-configuration boundary for all local LDN constructors; every entrypoint must pin its title, decoder and room guards.
- `zasim.ldn` owns transport, LDN/nickname validation, optional helper ownership and Pia handshaking. The public package has no old-title engine, replay adapter or legacy import shim. Archived development consumers are not distribution inputs.
- Add a synthetic regression for a bug fix. Preserve protocol, save, cancellation and restoration contracts; a post-FinalAgreement failure must retain received data and must not trigger an automatic repeat trade.
- Keep tests independent of developer paths, private captures and undocumented installed state. They must not contact a real console, change a radio, load a kernel module or reset USB controllers.
- Preserve component licenses and provenance. Do not introduce ROMs, keys, trainer records, Pokémon files, saves, captures, core dumps or personal hardware identifiers.
- Add new public files to the explicit export allowlist and appropriate package data. Do not broaden the export to include an entire working directory.

## Documentation and UI

Update Japanese and English instructions together. Keep command examples tied to the actual interpreter/entrypoint and distinguish source, wheel and runtime-bundled executables. Screenshots must use synthetic data and be labeled as examples, not hardware/legality evidence.

Name lookup tables are attributed display data, not permission to alter record identities, legality rules or core serialization. Exercise the real picker, menu and save workflow—not only direct calls to hidden buttons.

See [localization notes](docs/LOCALIZATION.md) for placeholder parity and translated controls. Check both languages at small/high-DPI sizes. A language switch must not save, convert, discard or approve a document or transaction.

日本語の報告も歓迎します。架空データで再現手順を示し、日英の説明・未保存入力・確認ダイアログを確認してください。実機で未検証の結果を「対応済み」と表示しないでください。

## Before opening an issue or pull request

Include expected/actual behavior, app version, OS/Python and a minimal synthetic reproducer. For hardware reports, add kernel, driver, adapter model and helper use—not private MACs, room codes or console keys. Remove private paths and identifiers from logs. [Security reports](SECURITY.md) follow a separate private route where available.

## Build and release

Follow the [release checklist](docs/RELEASING.md) for public export, tests, finalized artifacts and fresh-install checks. The version is declared once in `zasim/__init__.py`. Never publish the dirty development tree instead of the reviewed export.

A matching `vVERSION` tag runs the checks and creates a **draft prerelease**, not a stable publication. Publishing that draft is a separate maintainer action; PyPI upload is not automatic.

Preserve upstream AGPL copyright, the optional helper's GPL-2.0 notice and the PKHeX-derived-data notices in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
