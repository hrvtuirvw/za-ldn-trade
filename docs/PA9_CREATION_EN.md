# Create and edit PA9 files

**English** | [日本語](PA9_CREATION.md) · [User guide](USER_GUIDE_EN.md)

## No existing save required

**PA9 editor → New** creates a standalone Pokémon file from public species, growth and move definitions. It does not open a personal save, ROM, console key or wireless connection. The creation data's [provenance and licensing](../THIRD_PARTY_NOTICES.md) are bundled.

- **Starter:** six gift presets with fixed encounter data, including met level 5 and ball ID 4.
- **Custom:** 490 species/form drafts with encounter details supplied by you. It does not claim the combination can be legally obtained.
- New documents calculate stats automatically. Existing documents retain manual editing behavior unless you explicitly enable recalculation.

**Structure, computed stats, legality, game loading and successful physical trading are different claims.** Full legality is not implemented; game loading and physical trades of newly generated records are unverified.

## Create a file

Filter species by name (Japanese or English) or National Dex number. Search only filters the menu; it never silently changes the displayed selection. Choose the desired species from that menu. More options holds trainer gender, encounter date, nickname and moves; collapsing it retains input. Required custom encounter fields remain visible.

1. Choose **New**, an encounter model and species/form.
2. Enter the trainer name, **TID16** and **SID16**. These 0–65535 values are not the displayed six-digit trainer ID.
3. Choose the Pokémon's language, current level and encounter date.
4. For Custom, provide the required met location and level. IDs are internal game identifiers.
5. Select **Create & edit**, review the result, then **Save** (a new document asks for its filename).

Trainer/nickname limits depend on the Pokémon language: Japanese, Korean and Chinese use up to six UTF-16 units; supported Western languages use up to twelve. Supplementary Unicode characters take two UTF-16 units. Blank nickname uses the species name. UI language changes never translate existing trainer/nickname data.

The form defaults a **new** Pokémon's language to Japanese or English according to the current UI; its own language field remains explicit. A language switch does not modify a previously created/loaded document.

## Editing, calculation and saving

- **Apply** changes the in-memory record, not the file on disk. The tab's `*` marks unsaved changes.
- On the **Stats** tab, **Recalculate** updates level/experience and stats using public growth/personal data, IVs/EVs, stat nature and relevant flags. It also fully heals HP/status.
- Auto mode calculates on Apply/Save. Manual edits may leave cached party stats stale; the editor warns about this.
- Stored files are **328 bytes**; party files are **344 bytes**. Conversion to party format is explicit and confirmed. Normal saving preserves the current format.
- `.pa9` means decrypted output; `.epa9` and `.ea9` mean encrypted output.
- **Save** checks that the original file has not changed since it was loaded. Use Reload or Save as after an external change. Do not disable the check.
- **Revert** restores the last loaded/saved state after confirmation.
- Closing or replacing a dirty document offers Save / Discard / Cancel. Switching UI language does not discard edits or grant overwrite permission.

Move/ability/encounter/RNG correlations are not automatically repaired. The raw editor exposes IDs, not a comprehensive game database. Checksums and computed stat matches alone do not certify legality.

## CLI

Run from the extracted Linux source folder after setup. See [Windows/wheel command equivalents](USER_GUIDE_EN.md#which-python-to-use-for-cli-commands). Use new output names.

```sh
.venv-za/bin/python zatrade.py create-pa9 --species 152 --language 2 --ot-name Trainer --tid 12345 --sid 54321 --output new-chikorita.pa9
.venv-za/bin/python zatrade.py inspect-pa9 new-chikorita.pa9
.venv-za/bin/python zatrade.py edit-pa9 new-chikorita.pa9 --output edited.epa9 --set iv_hp=31 --recalculate
```

This creates, inspects and edits a synthetic example; it does not start a trade. Existing output files are refused unless replacement is explicitly authorized with `--force`.

```sh
.venv-za/bin/python zatrade.py list-species --help
.venv-za/bin/python zatrade.py create-pa9 --help
.venv-za/bin/python zatrade.py edit-pa9 --help
```

Use the command's current help for supported fields and creation options. The GUI and CLI share the same creation/editing model. JSON outputs retain stable English field names, including `legality: not_checked`; changing the display language does not change the file format or machine-readable keys.

Work with disposable copies first. Never attach personal PA9 records or saves to a public issue; use a synthetic reproducer.
