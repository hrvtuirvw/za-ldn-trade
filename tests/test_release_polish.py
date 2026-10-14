"""Public release gate and usability regressions. No real radio operations."""
from pathlib import Path
import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import traceback
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
old_path = sys.path[:]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    spec = importlib.util.spec_from_file_location("za_release_gate_tests", ROOT / "scripts/check_za_release.py")
    gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
finally:
    sys.path[:] = old_path

VERSION = "0.1.0"
PAYLOAD = {"zasim/ldn/synthetic.py": b"# synthetic\n", "zasim/synthetic.py": b"# synthetic\n",
           "zasim/data/bss_seed/ldn_bss_seed.c": b"/* synthetic */\n",
           "zatrade.py": b"# synthetic CLI\n", "zatrade_gui.py": b"# synthetic GUI\n",
           "LICENSE": b"synthetic application license\n", "THIRD_PARTY_NOTICES.md": b"synthetic notices\n",
           "licenses/helper.txt": b"synthetic component license\n", "README.md": b"# synthetic\n",
           ".gitignore": b"work/\n", "MANIFEST.json": b"{}\n"}


def metadata(version=VERSION):
    return (f"Metadata-Version: 2.4\nName: za-ldn-trade\nVersion: {version}\n"
            f"License-Expression: {gate.LICENSE_EXPRESSION}\nRequires-Python: >=3.10\n\n").encode()


def zip_blob(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, raw in files.items(): z.writestr(name, raw)
    return gate.normalize_wheel(buf.getvalue())


def wheel_files():
    prefix = f"za_ldn_trade-{VERSION}.dist-info/"
    files = {n: b for n, b in PAYLOAD.items() if n.startswith("zasim/") or n in ("zatrade.py", "zatrade_gui.py")}
    files.update({prefix + "licenses/" + n: b for n, b in PAYLOAD.items() if n in ("LICENSE", "THIRD_PARTY_NOTICES.md") or n.startswith("licenses/")})
    files[prefix + "METADATA"] = metadata()
    files[prefix + "WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[prefix + "entry_points.txt"] = b"[console_scripts]\nza-ldn-trade = zatrade:main\n[gui_scripts]\nza-ldn-trade-gui = zasim.gui:launch_gui\n"
    files[prefix + "top_level.txt"] = b"zasim\nzatrade\nzatrade_gui\n"
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + digest, str(len(data))])
    writer.writerow([prefix + "RECORD", "", ""])
    files[prefix + "RECORD"] = record.getvalue().encode()
    return files


def sdist_blob(*, extra=None, omit=None, owner=None):
    files = {n: b for n, b in PAYLOAD.items() if n not in (".gitignore", "MANIFEST.json")}
    files.update({"PKG-INFO": metadata(), "setup.cfg": b"[egg_info]\n"})
    for name in ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "requires.txt", "top_level.txt"):
        files["za_ldn_trade.egg-info/" + name] = metadata() if name == "PKG-INFO" else b"synthetic\n"
    files["za_ldn_trade.egg-info/SOURCES.txt"] = ("\n".join(sorted(files)) + "\n").encode()
    if omit: files.pop(omit)
    if extra: files.update(extra)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, raw in files.items():
            item = tarfile.TarInfo(f"za_ldn_trade-{VERSION}/" + name); item.size = len(raw)
            item.uname = owner or ""; item.uid = 123 if owner else 0
            t.addfile(item, io.BytesIO(raw))
    return buf.getvalue() if owner else gate.normalize_tar(buf.getvalue())


def artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "public_payload", lambda _: (VERSION, PAYLOAD))
    out = tmp_path / "dist"; out.mkdir()
    (out / f"za-ldn-trade-{VERSION}.zip").write_bytes(gate.archive_bytes(VERSION, PAYLOAD))
    (out / f"za_ldn_trade-{VERSION}-py3-none-any.whl").write_bytes(zip_blob(wheel_files()))
    (out / f"za_ldn_trade-{VERSION}.tar.gz").write_bytes(sdist_blob())
    return out


def test_gate_checks_all_formats_and_writes_portable_reports(tmp_path, monkeypatch, capsys):
    out = artifacts(tmp_path, monkeypatch)
    report = out / "VERIFICATION.json"
    assert gate.main(["--root", str(tmp_path), "--directory", str(out), "--write-checksums", "--report", str(report)]) == 0
    data = json.loads(report.read_text())
    assert data["status"] == "PASS" and data["runtime_or_hardware_tested_by_this_check"] is False
    assert str(tmp_path) not in report.read_text()
    assert (out / "SHA256SUMS").read_text().count("\n") == 3
    assert gate.check_release(tmp_path, out)[0] == data


@pytest.mark.parametrize("mutation", ["extra", "missing", "source", "version", "license", "record", "record_missing", "entrypoint", "extra_entrypoint", "tag"])
def test_wheel_gate_rejects_corruption(mutation):
    files = wheel_files(); prefix = f"za_ldn_trade-{VERSION}.dist-info/"
    if mutation == "extra": files["private/secret.keys"] = b"synthetic"
    elif mutation == "missing": files.pop("zasim/synthetic.py")
    elif mutation == "source": files["zasim/synthetic.py"] += b"# change\n"
    elif mutation == "version": files[prefix + "METADATA"] = metadata("9.9.9")
    elif mutation == "license": files[prefix + "METADATA"] = files[prefix + "METADATA"].replace(gate.LICENSE_EXPRESSION.encode(), b"MIT")
    elif mutation == "record": files[prefix + "RECORD"] = files[prefix + "RECORD"].replace(b"sha256=", b"sha512=", 1)
    elif mutation == "record_missing": files[prefix + "RECORD"] = b"\n".join(files[prefix + "RECORD"].splitlines()[1:])
    elif mutation == "entrypoint": files[prefix + "entry_points.txt"] = b"[console_scripts]\n"
    elif mutation == "extra_entrypoint": files[prefix + "entry_points.txt"] += b"unexpected = unrelated:main\n"
    elif mutation == "tag": files[prefix + "WHEEL"] += b"Tag: incompatible\n"
    with pytest.raises(ValueError): gate.check_wheel(zip_blob(files), VERSION, PAYLOAD)


@pytest.mark.parametrize("extra,omit,owner", [({"private/secret.keys": b"fake"},None,None),
    (None,"zasim/data/bss_seed/ldn_bss_seed.c",None), (None,None,"synthetic-build-owner")])
def test_sdist_gate_rejects_extra_missing_or_private_owner(extra, omit, owner):
    with pytest.raises(ValueError): gate.check_sdist(sdist_blob(extra=extra, omit=omit, owner=owner), VERSION, PAYLOAD)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "folder\\escape", "C:escape"])
def test_zip_names_are_rejected_without_extraction(name):
    raw = io.BytesIO()
    info = zipfile.ZipInfo('placeholder')
    # ZipInfo(name) normalizes backslashes on Windows before writing. Put the
    # actual adversarial name on the wire, rather than testing a safe rewrite.
    info.filename = info.orig_filename = name
    with zipfile.ZipFile(raw, "w") as z: z.writestr(info,b"synthetic")
    assert name.encode() in raw.getvalue()
    with pytest.raises(ValueError): gate.zip_files(raw.getvalue())


@pytest.mark.parametrize('consumer', ['gate', 'normalize'])
def test_zip_original_name_is_checked_before_windows_normalization(monkeypatch, consumer):
    raw = io.BytesIO()
    info = zipfile.ZipInfo('placeholder')
    info.filename = info.orig_filename = 'folder\\escape'
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr(info, b'synthetic')
    original = zipfile.ZipInfo
    class WindowsInfo(original):
        def __init__(self, filename='NoName', *args, **kwargs):
            super().__init__(filename, *args, **kwargs)
            self.filename = self.filename.replace('\\', '/')
    monkeypatch.setattr(zipfile, 'ZipInfo', WindowsInfo)
    operation = gate.zip_files if consumer == 'gate' else gate.normalize_wheel
    with pytest.raises(ValueError, match='unsafe'):
        operation(raw.getvalue())


def test_zip_links_and_duplicates_are_rejected():
    for kind in ("link", "duplicate"):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as z:
            info = zipfile.ZipInfo("synthetic"); info.create_system = 3
            info.external_attr = ((stat.S_IFLNK | 0o777) << 16) if kind == "link" else 0
            z.writestr(info,b"target")
            if kind == "duplicate":
                with pytest.warns(UserWarning): z.writestr("synthetic",b"again")
        with pytest.raises(ValueError): gate.zip_files(raw.getvalue())


def test_source_zip_or_checksums_cannot_be_silently_replaced(tmp_path, monkeypatch):
    out = artifacts(tmp_path,monkeypatch)
    (out / "SHA256SUMS").write_text("invalid\n")
    with pytest.raises(ValueError,match="SHA256SUMS"): gate.check_release(tmp_path,out)
    (out / "SHA256SUMS").unlink()
    path=out/f"za-ldn-trade-{VERSION}.zip";path.write_bytes(path.read_bytes()+b"trailing")
    with pytest.raises(ValueError,match="exact deterministic"): gate.check_release(tmp_path,out)


def test_gate_output_preflight_does_not_overwrite_or_write_partial_checksum(tmp_path,monkeypatch):
    out=artifacts(tmp_path,monkeypatch);report=out/'existing.json';report.write_text('old')
    with pytest.raises(SystemExit) as exc: gate.main(['--directory',str(out),'--write-checksums','--report',str(report)])
    assert exc.value.code==2 and report.read_text()=='old' and not (out/'SHA256SUMS').exists()


@pytest.mark.skipif(os.name=='nt',reason='POSIX FIFO and symlink')
def test_artifact_special_files_are_rejected_without_blocking(tmp_path):
    fifo=tmp_path/'fifo';os.mkfifo(fifo)
    with pytest.raises(ValueError): gate.read_artifact(fifo)
    file=tmp_path/'real';file.write_bytes(b'content');link=tmp_path/'link';link.symlink_to(file)
    with pytest.raises(ValueError): gate.read_artifact(link)


def test_artifact_size_is_checked_before_read(tmp_path,monkeypatch):
    big=tmp_path/'big'
    with big.open('wb') as f:f.truncate(gate.LIMIT+1)
    with pytest.raises(ValueError,match='size limit'):gate.read_artifact(big)


@pytest.mark.parametrize('kind',['timeout','spawn'])
def test_helper_errors_do_not_expose_room_identity(monkeypatch,kind):
    from zasim.ldn import bss_seed
    argv=['/usr/bin/insmod','/proc/self/fd/99','ssid=SYNTHETIC_PRIVATE_ROOM','bssid=02:00:00:00:00:01']
    def fail(*a,**k):
        if kind=='timeout':raise subprocess.TimeoutExpired(argv,2)
        raise OSError('SYNTHETIC_PRIVATE_ROOM')
    monkeypatch.setattr(subprocess,'run',fail)
    with pytest.raises(RuntimeError) as caught:bss_seed._run(argv)
    rendered=''.join(traceback.format_exception(type(caught.value),caught.value,caught.value.__traceback__))
    assert 'SYNTHETIC_PRIVATE_ROOM' not in rendered and '02:00:00:00:00:01' not in rendered
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize('text',['BSS ','BSS \n','BSS \nBSS ','','junk'])
def test_malformed_empty_bss_is_a_clean_miss(text):
    from zasim.ldn import bss_seed
    assert not bss_seed.exact_bss(text,'02:11:22:33:44:55','a'*32,2437)


@pytest.mark.skipif(os.name=='nt',reason='Linux helper shell launcher')
@pytest.mark.parametrize('selection',['path','absolute'])
def test_helper_launcher_python_selection_with_space_in_output(tmp_path,selection):
    fake=tmp_path/'fixture-python';fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n');fake.chmod(0o755)
    env={**os.environ,'PATH':str(tmp_path)+':'+os.environ['PATH'],'PYTHON':'fixture-python' if selection=='path' else str(fake)}
    output=tmp_path/'new folder'
    r=subprocess.run(['sh',str(ROOT/'scripts/setup_bss_helper.sh'),str(output)],capture_output=True,text=True,env=env,timeout=5)
    assert r.returncode==0,r.stderr
    assert f'bss-helper\nbuild\n--output\n{output}\n' in r.stdout


@pytest.mark.skipif(os.name=='nt',reason='Linux helper shell launcher')
def test_helper_launcher_default_visible_and_unknown_option_rejected(tmp_path):
    fake=tmp_path/'fixture-python';fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n');fake.chmod(0o755)
    env={**os.environ,'PYTHON':str(fake)}
    script=ROOT/'scripts/setup_bss_helper.sh'
    r=subprocess.run(['sh',str(script)],capture_output=True,text=True,env=env,timeout=5)
    assert r.returncode==0 and '/bss-helper/' in r.stdout and '/.bss-helper/' not in r.stdout
    r=subprocess.run(['sh',str(script),'--not-an-option'],capture_output=True,text=True,env=env,timeout=5)
    assert r.returncode==2 and 'Unknown option' in r.stderr


def test_workflows_gate_artifacts_and_create_only_draft_prerelease():
    test=(ROOT/'.github/workflows/test.yml').read_text()
    release=(ROOT/'.github/workflows/release.yml').read_text()
    assert 'check_za_release.py' in test and 'smoke_installed.py' in test and '[live]' in test
    assert 'export_za_source.py' in test and '-W error::ResourceWarning' in test
    assert '--draft --prerelease' in release and 'needs: verify' in release
    assert 'check_za_release.py' in release and 'smoke_installed.py' in release
    assert '--generate-notes' not in release


def test_helper_reuse_hides_irrelevant_interface_name(monkeypatch):
    from test_gui_layout import make_app,settle
    root,app,*_=make_app(monkeypatch)
    try:
        app.backend_var.set('direct');app.connection_advanced_var.set(True)
        app.single_interface_var.set(app.single_interface_combo.cget('values')[1])
        app.single_interface_combo.event_generate('<<ComboboxSelected>>');settle(root)
        assert not app.interface_entry.winfo_viewable()
        app.single_interface_var.set(app.single_interface_combo.cget('values')[0])
        app.single_interface_combo.event_generate('<<ComboboxSelected>>');settle(root)
        assert app.interface_entry.winfo_viewable()
    finally:root.destroy()


def test_gate_missing_report_directory_creates_no_checksum(tmp_path,monkeypatch):
    out=artifacts(tmp_path,monkeypatch)
    with pytest.raises(SystemExit) as exc:
        gate.main(['--directory',str(out),'--write-checksums','--report',str(tmp_path/'missing/report.json')])
    assert exc.value.code==2 and not (out/'SHA256SUMS').exists()


def _documentation_blocks(text):
    """The public guides use ordinary fenced sh/console/PowerShell examples."""
    return re.findall(r"(?ms)^```(sh|console|powershell)\n(.*?)^```[ \t]*$", text)


def _documentation_anchors(text):
    anchors = set(re.findall(r'<a\s+(?:id|name)="([^"]+)"', text))
    counts = {}
    # Ignore example headings inside code fences.
    prose = re.sub(r"(?ms)^```[^\n]*\n.*?^```[ \t]*$", "", text)
    for heading in re.findall(r"(?m)^#{1,6} (.+)$", prose):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        number = counts.get(slug, 0)
        counts[slug] = number + 1
        anchors.add(slug + (f"-{number}" if number else ""))
    return anchors


def test_public_documentation_local_links_images_and_anchors():
    _, payload = gate.public_payload(ROOT)
    checked = 0
    for name, raw in payload.items():
        if not name.endswith(".md"):
            continue
        text = raw.decode("utf-8")
        for href in re.findall(r"\]\(([^)]+)\)", text):
            if ":" in href or href.startswith("//"):
                continue
            target, _, anchor = href.partition("#")
            relative = os.path.normpath(str(Path(name).parent / target)).replace(os.sep, "/") if target else name
            assert relative in payload, (name, href)
            if anchor and relative.endswith(".md"):
                assert anchor in _documentation_anchors(payload[relative].decode("utf-8")), (name, href)
            checked += 1
    assert checked > 100  # Do not silently stop discovering the existing links.


@pytest.mark.parametrize("name", ["README.md", "README_JA.md", "docs/USER_GUIDE.md", "docs/USER_GUIDE_EN.md"])
def test_documented_first_run_uses_prepared_source_environment(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    blocks = _documentation_blocks(text)
    assert any("sh scripts/setup_za.sh\nsh scripts/start_za_gui.sh" in body
               for language, body in blocks if language == "sh")
    assert any("py -3 -m venv .venv-za" in body
               and r".\.venv-za\Scripts\python.exe -m pip install -r requirements-za.txt" in body
               and r".\Start-za-ldn-trade.cmd" in body
               for language, body in blocks if language == "powershell")
    # No OS-Python install masquerading as the app's prepared environment.
    for language, body in blocks:
        if language != "powershell":
            assert not re.search(r"(?m)^python(?:3)? (?:-m pip install|zatrade\.py)", body)


@pytest.mark.parametrize("name", ["README_ZA.md", "docs/PA9_CREATION.md", "docs/PA9_CREATION_EN.md"])
def test_documented_offline_cli_examples_execute_in_order(name, tmp_path):
    from zasim.pa9 import PA9
    from zasim.pa9_creation import CreateRequest, create_pa9
    raw = create_pa9(CreateRequest(152, "Test", 1, 2, language=2, seed=91)).to_bytes()
    (tmp_path / "input.pa9").write_bytes(raw)
    (tmp_path / "stored.pa9").write_bytes(raw[:328])
    text = (ROOT / name).read_text(encoding="utf-8")
    if name == "README_ZA.md":
        text = text.split("## PA9を新規作成・編集・検査", 1)[1].split("## 通常ユーザーGUI", 1)[0]
    else:
        text = text.split("## データ出自", 1)[0]
    commands = []
    for language, body in _documentation_blocks(text):
        if language == "powershell":
            continue
        for line in body.replace("\\\n", " ").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            args = shlex.split(line)
            if "zatrade.py" not in args:
                continue
            assert args[:2] == [".venv-za/bin/python", "zatrade.py"], line
            assert args[2] in {"list-species", "create-pa9", "inspect-pa9", "edit-pa9"}, line
            # Never run a shell or execute a live example extracted from Markdown.
            commands.append(args[2:])
    assert len(commands) >= 3
    for args in commands:
        result = subprocess.run([sys.executable, "-B", str(ROOT / "zatrade.py"), *args],
                                cwd=tmp_path, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, (name, args, result.stderr)
        if "--output" in args:
            output = tmp_path / args[args.index("--output") + 1]
            assert output.parent == tmp_path and output.is_file()
            # The public PA9 reader handles encrypted/decrypted output alike.
            mon = PA9(output.read_bytes())
            assert mon.size == 344 and mon.checksum_valid


def test_release_checkout_preserves_hash_checked_resource_bytes():
    attributes = (ROOT / '.gitattributes').read_text(encoding='utf-8')
    assert '* -text' in [line.strip() for line in attributes.splitlines()]
    assert '.gitattributes' in (ROOT / 'MANIFEST.in').read_text(encoding='utf-8').split()
    from zasim import ui_catalog
    raw = (ROOT / 'zasim/data/ui_names.json').read_bytes()
    provenance = json.loads((ROOT / 'zasim/data/ui_names_provenance.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(raw).hexdigest() == provenance['data_sha256']
    assert ui_catalog._names()['schema'] == 1
