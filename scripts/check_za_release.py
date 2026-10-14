"""Check finalized ZIP/wheel/sdist against the explicit public-source allowlist.

Read-only by default. Does not extract archives, import product code, install
anything, contact the network, publish, or operate hardware.
"""
from __future__ import annotations

import argparse
import base64
import csv
import configparser
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import zipfile

from build_za_release import (archive_bytes, no_links, public_payload, read_regular_file,
                             MAX_RELEASE_BYTES, MAX_RELEASE_MEMBERS)
from normalize_za_artifacts import normalize_tar, normalize_wheel, safe_name, safe_zip_name, bounded_tar_data

LIMIT = MAX_RELEASE_BYTES
MAX_MEMBERS = MAX_RELEASE_MEMBERS
LICENSE_EXPRESSION = "AGPL-3.0-only AND GPL-2.0-only"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_artifact(path):
    return read_regular_file(path, limit=LIMIT)


def zip_files(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        require(len(entries) <= MAX_MEMBERS, "too many ZIP members")
        require(len({entry.filename for entry in entries}) == len(entries), "duplicate ZIP member")
        require(sum(entry.file_size for entry in entries) <= LIMIT, "ZIP expansion exceeds limit")
        files = {}
        for entry in entries:
            safe_zip_name(entry)
            require(entry.filename and not entry.is_dir(), "unexpected ZIP directory")
            kind = stat.S_IFMT(entry.external_attr >> 16)
            require(kind in (0, stat.S_IFREG), "nonregular ZIP member")
            files[entry.filename] = archive.read(entry)
        return files


def tar_files(raw):
    with tarfile.open(fileobj=io.BytesIO(bounded_tar_data(raw)), mode="r:") as archive:
        files = {}; names = set(); total = 0
        for index, entry in enumerate(archive):
            require(index < MAX_MEMBERS, "too many tar members")
            safe_name(entry.name)
            require(entry.name and entry.name not in names, "empty or duplicate tar member")
            names.add(entry.name)
            require(entry.isfile() or entry.isdir(), "nonregular tar member")
            require(entry.uid == entry.gid == 0 and not entry.uname and not entry.gname,
                    "tar owner metadata was not normalized")
            if entry.isfile():
                total += entry.size
                require(total <= LIMIT, "tar expansion exceeds limit")
                files[entry.name] = archive.extractfile(entry).read()
        return files


def check_metadata(raw, version):
    meta = BytesParser().parsebytes(raw)
    require(meta.get_all("Name") == ["za-ldn-trade"], "package name mismatch")
    require(meta.get_all("Version") == [version], "package version mismatch")
    require(meta.get_all("License-Expression") == [LICENSE_EXPRESSION], "package license metadata mismatch")
    require(meta.get_all("Requires-Python") == [">=3.10"], "package Python requirement mismatch")


def check_wheel(raw, version, payload):
    files = zip_files(raw)
    prefix = f"za_ldn_trade-{version}.dist-info/"
    code = {name: data for name, data in payload.items()
            if name.startswith("zasim/") or name in ("zatrade.py", "zatrade_gui.py")}
    licenses = {prefix + "licenses/" + name: data for name, data in payload.items()
                if name in ("LICENSE", "THIRD_PARTY_NOTICES.md") or name.startswith("licenses/")}
    generated = {prefix + name for name in ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt")}
    require(set(files) == set(code) | set(licenses) | generated, "wheel member set differs from the public package")
    for name, expected in {**code, **licenses}.items():
        require(files[name] == expected, "wheel source/data/license differs from the reviewed source")
    check_metadata(files[prefix + "METADATA"], version)
    wheel = BytesParser().parsebytes(files[prefix + "WHEEL"])
    require(wheel.get_all("Root-Is-Purelib") == ["true"] and wheel.get_all("Tag") == ["py3-none-any"], "unexpected wheel platform tag")
    entries = configparser.ConfigParser(interpolation=None)
    entries.optionxform = str
    try:
        entries.read_string(files[prefix + "entry_points.txt"].decode("utf-8"))
    except configparser.Error as exc:
        raise ValueError("invalid installed entrypoint metadata") from exc
    require(not entries.defaults() and
            {section: dict(entries[section]) for section in entries.sections()} == {
                "console_scripts": {"za-ldn-trade": "zatrade:main"},
                "gui_scripts": {"za-ldn-trade-gui": "zasim.gui:launch_gui"},
            }, "installed command entrypoints differ from the public application")
    seen = set()
    for row in csv.reader(io.StringIO(files[prefix + "RECORD"].decode("utf-8"))):
        require(len(row) == 3, "invalid wheel RECORD row")
        name, digest, size = row
        require(name in files and name not in seen, "invalid or duplicate RECORD path")
        seen.add(name)
        if name == prefix + "RECORD":
            require(not digest and not size, "RECORD self-entry must be unhashed")
        else:
            expected = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=").decode("ascii")
            require(digest == "sha256=" + expected and size == str(len(files[name])), "wheel RECORD checksum/size mismatch")
    require(seen == set(files), "wheel RECORD omits members")
    require(normalize_wheel(raw) == raw, "wheel metadata has not been normalized")
    return files[prefix + "METADATA"]


def check_sdist(raw, version, payload):
    files = tar_files(raw)
    prefix = f"za_ldn_trade-{version}/"
    require(all(name.startswith(prefix) for name in files), "sdist root/version mismatch")
    files = {name[len(prefix):]: data for name, data in files.items()}
    source = {name: data for name, data in payload.items() if name not in (".gitignore", "MANIFEST.json")}
    generated = {"PKG-INFO", "setup.cfg"} | {"za_ldn_trade.egg-info/" + name for name in (
        "PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "requires.txt", "top_level.txt")}
    require(set(files) == set(source) | generated, "sdist member set differs from the public source")
    require(all(files[name] == data for name, data in source.items()), "sdist content differs from the reviewed source")
    check_metadata(files["PKG-INFO"], version)
    check_metadata(files["za_ldn_trade.egg-info/PKG-INFO"], version)
    for name in files["za_ldn_trade.egg-info/SOURCES.txt"].decode("utf-8").splitlines():
        safe_name(name)
        require(name in files, "sdist source listing contains an unshipped/private path")
    require(normalize_tar(raw) == raw, "sdist metadata has not been normalized")
    return files["PKG-INFO"]


def check_release(root, directory):
    version, payload = public_payload(root)
    directory = no_links(Path(os.path.abspath(directory)))
    names = (f"za-ldn-trade-{version}.zip", f"za_ldn_trade-{version}-py3-none-any.whl", f"za_ldn_trade-{version}.tar.gz")
    blobs = {name: read_artifact(directory / name) for name in names}
    # Exact byte regeneration also verifies the embedded manifest and all names.
    require(blobs[names[0]] == archive_bytes(version, payload), "source ZIP is not the exact deterministic public export")
    wheel_metadata = check_wheel(blobs[names[1]], version, payload)
    source_metadata = check_sdist(blobs[names[2]], version, payload)
    require(wheel_metadata == source_metadata, "wheel and sdist dependency/description metadata differ")
    assets = {name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()} for name, raw in blobs.items()}
    sums = "".join(f"{assets[name]['sha256']}  {name}\n" for name in sorted(assets))
    if (directory / "SHA256SUMS").exists():
        require(read_artifact(directory / "SHA256SUMS").decode("ascii") == sums, "SHA256SUMS does not match finalized assets")
    zip_sum = directory / (names[0] + ".sha256")
    if zip_sum.exists():
        require(read_artifact(zip_sum).decode("ascii") == f"{assets[names[0]]['sha256']}  {names[0]}\n", "source ZIP checksum sidecar mismatch")
    return {"status": "PASS", "version": version, "public_files": len(payload),
            "artifact_integrity_checked": True, "source_zip_byte_reproducible": True,
            "wheel_RECORD_checked": True, "component_licenses_checked": True,
            "runtime_or_hardware_tested_by_this_check": False, "artifacts": assets}, sums


def write_new(path, text):
    path = no_links(Path(os.path.abspath(path)))
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush(); os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parent.parent)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--write-checksums", action="store_true", help="write a new SHA256SUMS after all checks pass; no overwrite")
    parser.add_argument("--report", type=Path, help="write a new machine-readable report; no overwrite")
    args = parser.parse_args(argv)
    try:
        # Preflight every requested output before creating any of them.
        outputs = ([args.directory / "SHA256SUMS"] if args.write_checksums else []) + ([args.report] if args.report else [])
        require(len({Path(os.path.abspath(p)) for p in outputs}) == len(outputs), "duplicate release-check output")
        for path in outputs:
            absolute = no_links(Path(os.path.abspath(path)))
            require(absolute.parent.is_dir(), "release-check output directory is missing")
            require(not os.path.lexists(absolute), "release-check output already exists")
        report, sums = check_release(args.root, args.directory)
        if args.write_checksums:
            write_new(args.directory / "SHA256SUMS", sums)
        text = json.dumps(report, indent=2) + "\n"
        if args.report:
            write_new(args.report, text)
        print(text, end="")
        return 0
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
        parser.exit(2, f"release check failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
