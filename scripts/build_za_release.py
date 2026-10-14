"""Deterministic, explicit-allowlist source application ZIP; never scan work/keys."""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

MAX_RELEASE_BYTES = 64 * 1024 * 1024
MAX_RELEASE_MEMBERS = 4096

MODULES = {
    "zasim.ldn": "__init__ bss_seed ldn_name ldn_single pia_connect transport".split(),
    "zasim": "__init__ bss_helper broadcast capture client compatibility control doctor file_save gui gui_controller i18n handshake json_limits join_observation link_code live pa9 pa9_editor pa9_creation pa9_create_dialog peer_setup pia profile relay reliable session session_update sync_save text trade trade_protocol transport_setup ui_catalog catalog_picker user_data wireless radio_picker".split(),
}
PUBLIC_TESTS = "test_public_readiness test_release_boundary test_handshake_public test_capture test_debug_boundaries test_debug_runtime test_release_polish test_ldn_single test_transport_single test_ldn_factory_lifetime test_ldn_ip_predecessor test_ldn_auth_handoff test_ldn_station_event_rx test_bss_helper test_pia test_pa9 test_pa9_editor test_pa9_creation test_pa9_creation_gui test_trade_protocol test_trade test_reliable test_za_client test_gui test_gui_layout test_i18n test_gui_controller test_external_review test_review_hardening test_deep_debug test_deep_protocol test_radio_picker test_cli test_relay test_user_release test_broadcast test_peer_setup test_user_data test_initial_sync test_sync_save test_launcher test_wireless za_session_fixtures archive_shape".split()
FILES = tuple(sorted(
    [f"{package.replace('.', '/')}/{name}.py" for package, names in MODULES.items() for name in names]
    + [f"tests/{name}.py" for name in PUBLIC_TESTS]
    + ["tests/lab_user_data.py", "tests/test_cleanup_contracts.py",
       "tests/test_distribution_cleanup.py", "tests/test_origin_cleanup.py", "tests/test_usability.py", "scripts/za_build.py",
       "tests/test_runtime_polish.py", "tests/test_handshake_wire_equivalence.py",
       "tests/handshake_wire_reference.json", "tests/test_polish_safety.py",
       "tests/test_za_namespace.py", "tests/test_branding.py"]
    + ["RELEASE_NOTES.md", "docs/TESTING.md", "docs/TROUBLESHOOTING.md", "docs/RELEASING.md",
       "scripts/check_za_release.py", "scripts/smoke_installed.py",
       "zasim/data/bss_seed/ldn_bss_seed.c", "zasim/data/bss_seed/Kbuild",
       "scripts/setup_bss_helper.sh", "licenses/ldn-bss-seed-GPL-2.0.txt",
       "docs/BSS_HELPER.md", "docs/BSS_HELPER_EN.md",
       "tests/test_start_za_relay_wsl.ps1", "pytest.ini", "MANIFEST.in",
       ".github/workflows/test.yml", ".github/workflows/release.yml",
       "scripts/export_za_source.py", "scripts/normalize_za_artifacts.py", "scripts/release.gitignore",
       "LICENSE", "THIRD_PARTY_NOTICES.md", "licenses/PKHeX-GPL-3.0.txt",
       "zasim/locales/en.json", "zasim/data/za_creation.json", "zasim/data/za_creation_provenance.json",
       "tests/pa9_creation_reference.json", "tests/pa9_starter_reference.json",
       "zasim/data/ui_names.json", "zasim/data/ui_names_provenance.json", "scripts/build_ui_names.py",
       "scripts/pa9_reference/Program.cs", "scripts/pa9_reference/pa9_reference.csproj",
       "docs/images/editor-en.png", "docs/images/editor-ja.png", "README_JA.md", "docs/USER_GUIDE_EN.md", "docs/WIRELESS_COMPATIBILITY_EN.md", "docs/PA9_CREATION_EN.md", "docs/LOCALIZATION.md",
       ".editorconfig", ".github/ISSUE_TEMPLATE/bug_report.yml", ".github/ISSUE_TEMPLATE/feature_request.yml", ".github/ISSUE_TEMPLATE/config.yml", ".github/PULL_REQUEST_TEMPLATE.md",
       "docs/PA9_CREATION.md", "docs/WIRELESS_COMPATIBILITY.md", "README.md", "README_ZA.md", "requirements.txt", "requirements-za.txt",
       "pyproject.toml", "zatrade.py", "zatrade_gui.py", "Start-za-ldn-trade.cmd",
       "scripts/setup_za.sh", "scripts/start_za_gui.sh", "scripts/start_za_gui.ps1",
       "scripts/start_za_relay_wsl.ps1", "scripts/build_za_release.py",
       "docs/USER_GUIDE.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md"]
))


def no_links(path):
    path = Path(path)
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise ValueError("normalized absolute path required")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("symlink/reparse input or output rejected")
        if os.path.lexists(item):
            st = item.lstat()
            if getattr(st, "st_file_attributes", 0) & 0x400:
                raise ValueError("reparse point rejected")
    return path


def file_stamp(metadata):
    return tuple(getattr(metadata, field) for field in
                 ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode"))


def read_regular_file(path, *, limit=MAX_RELEASE_BYTES, expected=None):
    """Read a bounded, unchanged regular entry, rejecting raced links/FIFOs.

    Public export passes the identity from its all-names-first preflight.
    A changed descriptor is rejected before its contents are read.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError("release size limit must be a non-negative integer")
    path = no_links(Path(os.path.abspath(path)))
    before = path.lstat()
    identity = file_stamp(before) if expected is None else expected
    if not stat.S_ISREG(before.st_mode) or file_stamp(before) != identity:
        raise ValueError("release input is not an unchanged regular file")
    if before.st_size > limit:
        raise ValueError("release input exceeds size limit")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    fd = os.open(path, flags)
    try:
        if file_stamp(os.fstat(fd)) != identity:
            raise ValueError("release input changed before reading")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("release input exceeds size limit")
        if file_stamp(os.fstat(fd)) != identity or file_stamp(path.lstat()) != identity:
            raise ValueError("release input changed while reading")
        return raw
    finally:
        os.close(fd)


def source_version(raw):
    declarations = [node.value for node in ast.parse(raw).body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "__version__"
                        for target in node.targets)]
    if len(declarations) != 1:
        raise ValueError("one version declaration required")
    version = ast.literal_eval(declarations[0])
    if not isinstance(version, str) or re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version) is None:
        raise ValueError("strict release SemVer required")
    return version


def public_payload(root):
    root = no_links(Path(os.path.abspath(root)))
    # Names and every ancestor are validated before reading any content.
    identities = {}
    for relative in FILES:
        parts = PurePosixPath(relative)
        if parts.is_absolute() or ".." in parts.parts or "\\" in relative:
            raise ValueError("invalid public member name")
        if relative.startswith(("frlgsim/", "development/")) or relative == "frlgtrade.py":
            raise ValueError("archived compatibility is not a public input")
        p = no_links(root / relative)
        metadata = p.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("missing public input: " + relative)
        identities[relative] = file_stamp(metadata)
    if sum(identity[2] for identity in identities.values()) > MAX_RELEASE_BYTES:
        raise ValueError("public source exceeds release size limit")
    content = {relative: read_regular_file(root / relative, expected=identities[relative])
               for relative in FILES}
    content[".gitignore"] = content["scripts/release.gitignore"]
    version = source_version(content["zasim/__init__.py"])
    # Verify that local Markdown links are present in this exact layout.
    for relative, raw in content.items():
        if not relative.endswith(".md"):
            continue
        for href in re.findall(r"\]\(([^)]+)\)", raw.decode("utf-8")):
            href = href.split("#", 1)[0]
            if not href or ":" in href or href.startswith("//"):
                continue
            normalized = os.path.normpath(str(PurePosixPath(relative).parent / href)).replace(os.sep, "/")
            if normalized not in content:
                raise ValueError("missing packaged documentation link: " + relative + " -> " + href)
    manifest = {"schema": 1, "version": version,
                "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(content.items())},
                "includes_personal_data_or_keys": False,
                "windows_execution_verified": False,
                "standard_radio_backend_physical_exchange_verified": False}
    content["MANIFEST.json"] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    return version, content


def archive_bytes(version, content):
    root_name = "za-ldn-trade-" + version
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, raw in sorted(content.items()):
            info = zipfile.ZipInfo(root_name + "/" + name, (2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((0o100755 if name.endswith(".sh") else 0o100644) << 16)
            archive.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    raw = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.testzip() is not None or len(archive.infolist()) != len(content):
            raise ValueError("ZIP verification failed")
        for name, data in content.items():
            if archive.read(root_name + "/" + name) != data:
                raise ValueError("ZIP byte mismatch")
    return raw


def build(root, output):
    output = no_links(Path(os.path.abspath(output)))
    version, content = public_payload(root)
    archive = output / ("za-ldn-trade-" + version + ".zip")
    ledger = output / (archive.name + ".sha256")
    for p in (archive, ledger):
        no_links(p)
        if os.path.lexists(p):
            raise FileExistsError("release destination already exists")
    data = archive_bytes(version, content)
    digest = hashlib.sha256(data).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    # Exclusive, no-follow publication; never replace an existing release.
    for path, raw in ((archive, data), (ledger, (digest + "  " + archive.name + "\n").encode())):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    return {"version": version, "archive": str(archive), "sha256": digest,
            "bytes": len(data), "members": len(content)}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).absolute().parent.parent)
    p.add_argument("--output", required=True, type=Path)
    a = p.parse_args()
    print(json.dumps(build(a.root, a.output), indent=2))
