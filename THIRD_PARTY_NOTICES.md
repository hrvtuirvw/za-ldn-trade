# Third-party notices

The Python application is distributed under [AGPL-3.0-only](LICENSE). The
separately compiled optional kernel helper is GPL-2.0-only. Package metadata
uses `AGPL-3.0-only AND GPL-2.0-only` to identify these separate components;
it does not relicense the kernel helper under the application license. It is not affiliated
with Nintendo, Game Freak, The Pokémon Company, Project Pokémon or PKHeX.

## frlg-ldn-trade — tornadus and contributors

Source: https://github.com/tornadus/frlg-ldn-trade

`za-ldn-trade` is inspired by this project and adapts its LDN/Pia communication
implementation. The required shared communication code is now under
`zasim/ldn/`; the historical FRLG-specific simulator is not distributed here.
Upstream attribution and the AGPL-3.0-only application license are retained.
Renaming the tool does not imply that the inherited implementation is original
work or that upstream endorses this derivative.

## PKHeX.Core — Kaphotics and contributors

Source: https://github.com/kwsch/PKHeX

Pinned revision: `e15d2467b32da7bc26ce7cc8e5c4ede32740e20a` (Core 26.08.26).
The upstream `PKHeX.Core.csproj` declares **GPL-3.0-or-later**. The complete
upstream license is included as [PKHeX-GPL-3.0.txt](licenses/PKHeX-GPL-3.0.txt).

`zasim/data/za_creation.json` contains a lossless, editable JSON projection of
public PKHeX personal/species, growth, level-up move, PP, starter-gift,
species-name and species-ID tables.
`zasim/pa9_creation.py` adapts its documented PA9, stat, starter-gift and
xoroshiro/Lumiose generation rules. These PKHeX-derived portions retain their
GPL-3.0-or-later attribution. GPLv3 section 13 expressly permits their
combination with AGPLv3-covered code; the combined application is conveyed
under the project's AGPLv3 terms, not as proprietary or permissively licensed code.
The pre-existing PA9 codec/editor also follows the PKHeX format and algorithms.

`zasim/data/ui_names.json` projects the same pinned public source's Japanese/
English move, ability and item labels for display and search only. These labels
are not an encounter/learnability or title-availability database. Their input
hashes and generated-data hash are recorded in
[name provenance](zasim/data/ui_names_provenance.json). Regenerate them with
`python scripts/build_ui_names.py /path/to/pinned/PKHeX zasim/data`; the included
exporter verifies the pinned text/license/project hashes before writing output
and normalizes Git-for-Windows CRLF to LF. Attribution and GPL-3.0-or-later
terms above apply to this projection as well.

The data projection is built by the included source
[scripts/pa9_reference/Program.cs](scripts/pa9_reference/Program.cs).
[data provenance](zasim/data/za_creation_provenance.json) records the public
archive SHA-256, revision, selected source-input hashes and generated-data hash.
[Reproduction instructions](docs/PA9_CREATION.md) explain how to obtain the exact
public source and regenerate the projection and independent test vectors.
No upstream DLL, executable, save, ROM, graphics, keys or personal individual
records are bundled. The C# reference tool is optional maintainer tooling,
**not** an install/runtime dependency of the Python GUI or CLI.

The public upstream license declaration and source provenance were inspected;
this is not a warranty that upstream can license every third-party game datum,
nor a trademark license or approval by the game publisher. Pokémon names and
associated marks remain the property of their respective owners. Review the
intended publication's legal requirements separately before publishing.

Existing upstream simulator attribution and the root license remain unchanged.

## Optional ldn_bss_seed kernel helper

The separately compiled `zasim/data/bss_seed/ldn_bss_seed.c` is GPL-2.0-only; see [the license](licenses/ldn-bss-seed-GPL-2.0.txt). Its source is unchanged from the verified development helper. It is not a prebuilt Wi-Fi driver. The Python application remains AGPL-3.0-only.
