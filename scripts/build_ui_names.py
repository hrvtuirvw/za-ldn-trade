"""Regenerate editable display names from the pinned public PKHeX source tree."""

from pathlib import Path
import argparse
import hashlib
import json

COMMIT = "e15d2467b32da7bc26ce7cc8e5c4ede32740e20a"
PATHS = {
    kind: {
        lang: (
            f"PKHeX.Core/Resources/text/items/text_Items_{lang}.txt"
            if kind == "items"
            else f"PKHeX.Core/Resources/text/other/{lang}/text_{title}_{lang}.txt"
        )
        for lang in ("ja", "en")
    }
    for kind, title in (
        ("moves", "Moves"),
        ("abilities", "Abilities"),
        ("items", "Items"),
    )
}


EXPECTED_INPUTS = {
    "PKHeX.Core/Resources/text/other/ja/text_Moves_ja.txt": "eb9f4cec160f21bf0dee5029f1cf03c7606ef0e773349b9747ade0c121f6a296",
    "PKHeX.Core/Resources/text/other/en/text_Moves_en.txt": "0530e3b28b58bd674769375bb25fb2afd35ef49a522283e33ece6d1f2bcdb719",
    "PKHeX.Core/Resources/text/other/ja/text_Abilities_ja.txt": "14be9433da8d5849542711cf43893c00217a9df620d24471ca637b4d6d207e4b",
    "PKHeX.Core/Resources/text/other/en/text_Abilities_en.txt": "84a3caeaa9f238545225e8d9987e8fbefde4f074f7d5289cd575f8db8961b4e5",
    "PKHeX.Core/Resources/text/items/text_Items_ja.txt": "a22f6b5be3e4e5119cd340380e1041ffa29039aa693b8baf33c1ea47d01c3ac1",
    "PKHeX.Core/Resources/text/items/text_Items_en.txt": "ced422707941cb377098c7b82fa6e0a1cc1d93fe27e8f43915952dd1b3313831",
    "LICENSE": "ccb349b4132ed7737f25e5adebfe61f3d52dca33708df1e50352320438d1d4c2",
    "PKHeX.Core/PKHeX.Core.csproj": "d4c30cea1e4c24115798b1b11b1ad126123f286af1d8ade6c4e68920f2e6f17a",
}


def build(source, output):
    # Source archives use LF; Git for Windows may check out equivalent CRLF.
    raw_inputs = {
        name: (source / name).read_bytes().replace(b"\r\n", b"\n")
        for name in EXPECTED_INPUTS
    }
    for name, expected in EXPECTED_INPUTS.items():
        if hashlib.sha256(raw_inputs[name]).hexdigest() != expected:
            raise ValueError("source differs from pinned PKHeX revision: " + name)
    result = {
        "schema": 1,
        "source_commit": COMMIT,
        "source_license": "GPL-3.0-or-later",
    }
    inputs = {}
    for kind, languages in PATHS.items():
        result[kind] = {}
        for lang, relative in languages.items():
            raw = raw_inputs[relative]
            inputs[relative] = hashlib.sha256(raw).hexdigest()
            result[kind][lang] = raw.decode("utf-8-sig").splitlines()
        if len(result[kind]["ja"]) != len(result[kind]["en"]):
            raise ValueError("localized ID tables differ in length: " + kind)
    raw = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
    output.mkdir(parents=True, exist_ok=True)
    (output / "ui_names.json").write_bytes(raw)
    (output / "ui_names_provenance.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "project": "https://github.com/kwsch/PKHeX",
                "commit": COMMIT,
                "license": "GPL-3.0-or-later",
                "inputs": inputs,
                "data_sha256": hashlib.sha256(raw).hexdigest(),
                "purpose": "Display/search names only, not an encounter legality database",
                "input_normalization": "CRLF to LF; exact pinned hashes required before output",
                "exporter": "scripts/build_ui_names.py",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source", type=Path, help="public source at the documented pinned revision"
    )
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.source, args.output)
