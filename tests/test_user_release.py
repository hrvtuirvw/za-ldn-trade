from pathlib import Path
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import time
import zipfile

import pytest

from zasim import __version__
from zasim.client import LiveTradeResult
from zasim.doctor import check_installation
from zasim.gui import _DesktopApp
from zasim.gui_controller import ConnectionSettings, GuiController, TradeRequest
from zasim.user_data import UserData, make_pc_user_data

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('release_builder_for_tests', ROOT/'scripts/build_za_release.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


def test_public_profile_is_independent_and_native_sized():
    a=make_pc_user_data('ZA-Trade',trainer_id=123)
    assert a.trainer_id==123 and a.name_utf16.decode('utf-16-le').rstrip('\0')=='ZA-Trade'
    assert UserData.decode(a.encode())==a and len(a.native_object())==88
    assert a.entries==((0,0xfffffffe),)*3 and a.profile_text==bytes(32)


def test_profile_shortens_on_a_utf16_boundary():
    # Seven supplementary characters fit LDN's UTF8 limit but not native name.
    a=make_pc_user_data('😀'*7,trainer_id=1)
    assert a.name_utf16.decode('utf-16-le').rstrip('\0')=='😀'*6
    assert a.name_utf16[-2:]==b'\0\0'


@pytest.mark.parametrize('name',['','A'*21,'bad\nname','\ud800'])
def test_invalid_profile_never_reaches_network(name):
    with pytest.raises((ValueError,UnicodeError)):make_pc_user_data(name)


def test_gui_always_enables_native_setup_and_save_sync(tmp_path):
    offer=tmp_path/'outgoing.epa9';offer.write_bytes(b'fixture')
    calls=[]
    def run(path,**kwargs):
        calls.append(kwargs)
        return LiveTradeResult(b'fixture',0.1,tmp_path/'received.epa9',native_save_sync_complete=True)
    controller=GuiController(trade_runner=run)
    controller.start_trade(TradeRequest(ConnectionSettings(nickname='PC-Test'),offer,tmp_path/'received.epa9','12345678'))
    deadline=time.monotonic()+2
    while controller.is_busy and time.monotonic()<deadline:time.sleep(.002)
    assert len(calls)==1 and calls[0]['sync_save'] is True
    assert isinstance(calls[0]['peer_user_data'],UserData)
    assert calls[0]['timeout']==300 and calls[0]['auto_confirm'] is False
    assert calls[0]['peer_user_data'].name_utf16.decode('utf-16-le').rstrip('\0')=='PC-Test'


def test_cli_always_enables_native_setup_and_save_sync(monkeypatch,tmp_path,capsys):
    from zatrade import main
    from zasim.pa9 import PA9
    calls=[]
    def run(path,**kwargs):
        calls.append(kwargs)
        return LiveTradeResult(PA9().to_bytes(encrypted=True,party=True),.1,tmp_path/'received.epa9',native_save_sync_complete=True)
    monkeypatch.setattr('zasim.client.run_live_trade',run)
    assert main(['trade','--offer','synthetic.epa9','--output',str(tmp_path/'received.epa9'),'--relay','127.0.0.1:24872','--link-code','12345678','--yes'])==0
    assert calls[0]['sync_save'] is True and isinstance(calls[0]['peer_user_data'],UserData)
    assert calls[0]['timeout']==300
    assert json.loads(capsys.readouterr().out)['native_save_sync_complete'] is True


def test_explicit_interface_policy_reaches_live_runner():
    assert ConnectionSettings(single_interface=True).live_kwargs()['single_interface'] is True
    assert ConnectionSettings(single_interface=False).live_kwargs()['single_interface'] is False
    with pytest.raises(TypeError):ConnectionSettings(single_interface=1).validate()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink fixture; Windows reparse check is separate')
def test_received_default_never_reuses_existing_or_symlink(tmp_path):
    (tmp_path/'received.epa9').write_bytes(b'existing')
    (tmp_path/'received-001.epa9').symlink_to(tmp_path/'missing')
    assert _DesktopApp._next_received_path(tmp_path)==tmp_path/'received-002.epa9'
    assert (tmp_path/'received.epa9').read_bytes()==b'existing'


def test_doctor_does_not_execute_process_connect_or_read_keys(monkeypatch,tmp_path):
    def forbidden(*a,**kw):raise AssertionError('no external commands, sockets, or content reads')
    monkeypatch.setattr(subprocess,'run',forbidden)
    monkeypatch.setattr(socket,'socket',forbidden)
    monkeypatch.setattr(Path,'read_bytes',forbidden)
    monkeypatch.setattr(Path,'read_text',forbidden)
    r=check_installation(backend='direct',sysfs=tmp_path)
    assert r['keys_read'] is False and r['radio_mutated'] is False and r['live_connection_tested'] is False
    assert r['version']==__version__


def test_archive_is_reproducible_explicit_and_private_free(tmp_path):
    version,content=builder.public_payload(ROOT)
    a=builder.archive_bytes(version,content);b=builder.archive_bytes(version,content)
    assert a==b
    with zipfile.ZipFile(io.BytesIO(a)) as z:
        prefix='za-ldn-trade-'+__version__+'/'
        assert all(name.startswith(prefix) for name in z.namelist())
        for name in z.namelist():
            assert not any(part in name.split('/') for part in ('work','.git','.venv','__pycache__'))
            assert not name.endswith(('.keys','.pa9','.epa9','.ko','.nsp','.nca','.core','.pyc'))
        manifest=json.loads(z.read(prefix+'MANIFEST.json'))
        assert manifest['includes_personal_data_or_keys'] is False
        assert manifest['standard_radio_backend_physical_exchange_verified'] is False
        assert len(z.namelist())==len(manifest['files'])+1
    r1=builder.build(ROOT,tmp_path/'one');r2=builder.build(ROOT,tmp_path/'two')
    assert r1['sha256']==r2['sha256']
    with pytest.raises(FileExistsError):builder.build(ROOT,tmp_path/'one')


def copied_public_tree(tmp_path):
    dst=tmp_path/'source';dst.mkdir()
    for relative in builder.FILES:
        p=dst/relative;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/relative,p)
    return dst


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink fixture')
def test_protected_input_symlink_fails_before_any_content_read(tmp_path,monkeypatch):
    root=copied_public_tree(tmp_path)
    p=root/'zasim/client.py';p.unlink();p.symlink_to('/private/prod.keys')
    def forbidden(*a,**kw):raise AssertionError('content read before all name checks')
    monkeypatch.setattr(Path,'read_bytes',forbidden)
    with pytest.raises(ValueError,match='symlink'):builder.public_payload(root)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink fixture')
def test_late_output_symlink_preflight_writes_no_archive(tmp_path):
    output=tmp_path/'out';output.mkdir()
    name='za-ldn-trade-'+__version__+'.zip'
    (output/(name+'.sha256')).symlink_to(tmp_path/'protected')
    with pytest.raises(ValueError,match='symlink'):builder.build(ROOT,output)
    assert not (output/name).exists() and not (tmp_path/'protected').exists()


@pytest.mark.parametrize('version',['01.0.0','1.0','1.2.3-private','1.2.3\n'])
def test_version_rejects_nonrelease_identity(version):
    with pytest.raises(ValueError):builder.source_version('__version__='+repr(version))


@pytest.mark.skipif(os.name == 'nt', reason='Linux shell launcher')
def test_linux_gui_launcher_uses_user_environment_without_installing(tmp_path):
    root=tmp_path/'app';(root/'scripts').mkdir(parents=True)
    shutil.copyfile(ROOT/'scripts/start_za_gui.sh',root/'scripts/start_za_gui.sh')
    (root/'zatrade.py').write_text('')
    (root/'.venv-za/bin').mkdir(parents=True)
    fake=root/'.venv-za/bin/python'
    fake.write_text('#!/bin/sh\ncase "$1" in -c) exit 0 ;; esac\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    env=dict(os.environ);env.pop('ZA_TRADE_PYTHON',None)
    result=subprocess.run(['sh',str(root/'scripts/start_za_gui.sh'),'--backend','relay'],text=True,capture_output=True,env=env,timeout=10)
    assert result.returncode==0
    assert result.stdout.splitlines()==['-s','-B',str(root/'zatrade.py'),'gui','--backend','relay']


def test_reparse_input_is_rejected_before_read(tmp_path,monkeypatch):
    from types import SimpleNamespace
    p=tmp_path/'linked';p.write_text('sentinel');original=Path.lstat
    monkeypatch.setattr(Path,'is_symlink',lambda self:False)
    monkeypatch.setattr(Path,'lstat',lambda self:SimpleNamespace(st_file_attributes=0x400,st_mode=0o100644) if self==p else original(self))
    with pytest.raises(ValueError,match='reparse'):builder.no_links(p)


def test_normalization_removes_owner_metadata_and_preserves_contents(monkeypatch):
    import tarfile
    monkeypatch.syspath_prepend(str(ROOT/'scripts'))
    from normalize_za_artifacts import normalize_tar,normalize_wheel
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
        item=tarfile.TarInfo('package/source.py');item.uid=1234;item.gid=2345
        item.uname='synthetic-private-owner';item.gname='synthetic-private-group'
        item.mtime=123456789;item.size=5;archive.addfile(item,io.BytesIO(b'hello'))
    result=normalize_tar(buffer.getvalue())
    assert normalize_tar(result)==result
    with tarfile.open(fileobj=io.BytesIO(result),mode='r:gz') as archive:
        item=archive.getmembers()[0]
        assert item.uid==item.gid==0 and not item.uname and not item.gname
        assert archive.extractfile(item).read()==b'hello'
    wheel=io.BytesIO()
    with zipfile.ZipFile(wheel,'w') as z:z.writestr('package/source.py',b'hello')
    normalized=normalize_wheel(wheel.getvalue())
    assert normalize_wheel(normalized)==normalized
    with zipfile.ZipFile(io.BytesIO(normalized)) as z:assert z.read('package/source.py')==b'hello'


def test_public_export_has_no_private_workspace_dependency(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'scripts'))
    from export_za_source import export
    output=tmp_path/'portable';receipt=export(ROOT,output)
    assert receipt['copied_git_or_work'] is False
    assert (output/'.github/workflows/test.yml').is_file()
    assert not (output/'work').exists() and not (output/'.git').exists()
    assert (output/'README.md').read_text(encoding='utf-8').startswith('# za-ldn-trade')
    assert (output/'.gitignore').read_bytes()==(ROOT/'scripts/release.gitignore').read_bytes()
    with pytest.raises(FileExistsError):export(ROOT,output)


def test_public_path_scan_distinguishes_absolute_paths_from_comment_fragments(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'scripts'))
    import export_za_source as source_export
    synthetic_root = Path('/' + 'app').absolute()
    monkeypatch.setattr(source_export,'public_payload',lambda _root:('0.6.0',{'source.py':b'# comm/scene/app unchanged\n'}))
    assert source_export.export(synthetic_root,tmp_path/'allowed')['files']==1
    private_example = f'open("{synthetic_root}/private/config")\n'.encode()
    monkeypatch.setattr(source_export,'public_payload',lambda _root:('0.6.0',{'source.py':private_example}))
    with pytest.raises(ValueError,match='host-specific'):source_export.export(synthetic_root,tmp_path/'refused')
    assert not (tmp_path/'refused').exists()
