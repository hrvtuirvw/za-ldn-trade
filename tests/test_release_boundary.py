"""Release tooling must reject malformed input before normalizing/publishing it."""
from __future__ import annotations

import gzip
import io
import os
from pathlib import Path
import stat
import sys
import tarfile
import zipfile

import pytest

from test_release_polish import gate

builder = sys.modules['build_za_release']
normalizer = sys.modules['normalize_za_artifacts']


@pytest.mark.parametrize('kind', [stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFDIR])
def test_wheel_normalizer_never_launders_nonregular_members(kind):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        item = zipfile.ZipInfo('zasim/source.py'); item.create_system = 3
        item.external_attr = (kind | 0o644) << 16
        archive.writestr(item, b'not a regular source member')
    with pytest.raises(ValueError):
        normalizer.normalize_wheel(output.getvalue())


@pytest.mark.parametrize('kind', ['wheel', 'tar'])
def test_normalizer_bounds_member_count_before_payload_extraction(kind):
    output = io.BytesIO()
    if kind == 'wheel':
        with zipfile.ZipFile(output, 'w') as archive:
            for n in range(gate.MAX_MEMBERS + 1):
                archive.writestr(str(n), b'')
        operation = normalizer.normalize_wheel
    else:
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for n in range(gate.MAX_MEMBERS + 1):
                archive.addfile(tarfile.TarInfo(str(n)), io.BytesIO())
        operation = normalizer.normalize_tar
    with pytest.raises(ValueError, match='members'):
        operation(output.getvalue())


def test_tar_normalizer_rejects_declared_expansion_before_reading_or_seeking_body():
    item = tarfile.TarInfo('oversized.bin'); item.size = gate.LIMIT + 1
    raw = gzip.compress(item.tobuf() + bytes(1024))
    # A read/seek-first implementation discovers only a truncated archive.
    with pytest.raises(ValueError, match='(expansion|size limit)'):
        normalizer.normalize_tar(raw)


def test_normalizer_validates_both_artifacts_before_changing_either(tmp_path):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        item = tarfile.TarInfo('original.txt'); item.size = 3
        item.uid = 123; item.uname = 'synthetic-builder'; item.mtime = 1234567
        archive.addfile(item, io.BytesIO(b'old'))
    source = tmp_path / 'za_ldn_trade-0.1.0.tar.gz'; source.write_bytes(output.getvalue())
    wheel = tmp_path / 'za_ldn_trade-0.1.0-py3-none-any.whl'; wheel.write_bytes(b'not-a-wheel')
    before = source.read_bytes()
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        normalizer.normalize(tmp_path, '0.1.0')
    assert source.read_bytes() == before
    assert wheel.read_bytes() == b'not-a-wheel'
    assert not list(tmp_path.glob('*.normalized'))


@pytest.mark.skipif(os.name == 'nt', reason='POSIX source symlink substitution')
def test_export_refuses_source_swapped_after_name_preflight(tmp_path, monkeypatch):
    root = tmp_path / 'public'; (root / 'zasim').mkdir(parents=True); (root / 'scripts').mkdir()
    (root / 'zasim/__init__.py').write_text('__version__ = "0.1.0"\n')
    target = root / 'payload.py'; target.write_bytes(b'approved public data')
    (root / 'scripts/release.gitignore').write_text('work/\n')
    private = tmp_path / 'synthetic-private'; private.write_bytes(b'SYNTHETIC-NOT-PUBLIC')
    monkeypatch.setattr(builder, 'FILES', ('zasim/__init__.py', 'payload.py', 'scripts/release.gitignore'))
    actual = builder.no_links
    swapped = []
    def checked(path):
        result = actual(path)
        if Path(path) == root / 'scripts/release.gitignore' and not swapped:
            swapped.append(True)
            target.unlink(); target.symlink_to(private)
        return result
    monkeypatch.setattr(builder, 'no_links', checked)
    with pytest.raises((ValueError, OSError, RuntimeError)):
        builder.public_payload(root)
    assert swapped == [True]
    assert private.read_bytes() == b'SYNTHETIC-NOT-PUBLIC'


@pytest.mark.parametrize('name', ['', '.', 'a//b', './a', 'a/./b', 'a\x00b', 'a\nb'])
def test_archive_names_are_canonical_before_transform(name):
    with pytest.raises(ValueError):
        normalizer.safe_name(name)


def test_entire_tar_stream_is_bounded_including_extended_headers(monkeypatch):
    monkeypatch.setattr(normalizer, 'MAX_RELEASE_BYTES', 1024)
    bomb = gzip.compress(bytes(2048))
    assert len(bomb) < 1024
    with pytest.raises(ValueError, match='expansion'):
        normalizer.normalize_tar(bomb)
    with pytest.raises(ValueError, match='expansion'):
        gate.tar_files(bomb)


@pytest.mark.parametrize('mutation', ['replacement', 'growth'])
def test_public_reader_refuses_changed_descriptor_before_read(tmp_path, monkeypatch, mutation):
    target = tmp_path / 'source.py'; target.write_bytes(b'original')
    expected = builder.file_stamp(target.stat())
    if mutation == 'replacement':
        replacement = tmp_path / 'new'; replacement.write_bytes(b'new file')
        replacement.replace(target)
    else:
        target.write_bytes(b'original grows')
    with pytest.raises(ValueError, match='unchanged'):
        builder.read_regular_file(target, expected=expected)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX FIFO and O_NONBLOCK')
def test_public_reader_rejects_fifo_at_actual_open_without_blocking(tmp_path, monkeypatch):
    target = tmp_path / 'source.py'; target.write_bytes(b'original')
    original = os.open
    reached = []
    def swapped(path, flags, *args, **kwargs):
        if Path(path) == target:
            reached.append(True)
            assert flags & os.O_NONBLOCK
            target.unlink(); os.mkfifo(target)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', swapped)
    with pytest.raises(ValueError, match='changed'):
        builder.read_regular_file(target)
    assert reached == [True]


def test_public_reader_closes_fd_after_read_failure(tmp_path, monkeypatch):
    target = tmp_path / 'source.py'; target.write_bytes(b'original')
    seen = []
    def failed(fd, *args, **kwargs):
        seen.append(fd)
        raise OSError('synthetic fdopen failure')
    monkeypatch.setattr(os, 'fdopen', failed)
    with pytest.raises(OSError):
        builder.read_regular_file(target)
    assert len(seen) == 1
    with pytest.raises(OSError):
        os.fstat(seen[0])


@pytest.mark.parametrize('limit', [-1, True, 1.5])
def test_public_reader_rejects_invalid_limit(tmp_path, limit):
    with pytest.raises(ValueError, match='size limit'):
        builder.read_regular_file(tmp_path / 'missing', limit=limit)


@pytest.mark.parametrize('suffix', ['.txt', '.cmd', '.CMD', '.exe', '.bat', '.com'])
def test_public_reader_accepts_unchanged_native_file_and_preflight(tmp_path, suffix):
    target = tmp_path / ('source' + suffix)
    raw = b'unchanged\r\n\x00binary\xff'
    target.write_bytes(raw)
    expected = builder.file_stamp(target.lstat())
    assert builder.read_regular_file(target, expected=expected, limit=len(raw)) == raw
    assert builder.file_stamp(target.lstat()) == expected


def windows_metadata(*, descriptor=False, modern=True, **changes):
    from types import SimpleNamespace
    data = dict(st_dev=123, st_ino=456, st_size=8, st_mtime_ns=100,
                st_ctime_ns=300 if descriptor and modern else 200,
                st_mode=stat.S_IFREG | (0o666 if descriptor else 0o777),
                st_file_attributes=32, st_reparse_tag=0)
    if modern:
        data['st_birthtime_ns'] = 200
    data.update(changes)
    return SimpleNamespace(**data)


@pytest.mark.parametrize('modern', [False, True])
@pytest.mark.parametrize('field', [None, 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns',
                                  'creation', 'st_mode', 'st_file_attributes', 'st_reparse_tag'])
def test_windows_cross_api_stat_contract(monkeypatch, modern, field):
    # CPython 3.10 uses creation-time ctime; 3.12+ also has birthtime and
    # fstat may expose change-time ctime. Only known API representation differs.
    monkeypatch.setattr(builder, '_WINDOWS', True)
    before = windows_metadata(modern=modern)
    opened = windows_metadata(descriptor=True, modern=modern)
    if field:
        key = ('st_birthtime_ns' if modern else 'st_ctime_ns') if field == 'creation' else field
        setattr(opened, key, getattr(opened, key) ^ (0o200 if key == 'st_mode' else 1))
    assert builder._descriptor_matches(Path('source.CmD'), before, opened) is (field is None)
    if field is None:
        assert not builder._descriptor_matches(Path('source.txt'), before, opened)


@pytest.mark.parametrize('field', ['st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode'])
def test_public_reader_retains_full_descriptor_change_detection(tmp_path, monkeypatch, field):
    from types import SimpleNamespace
    target = tmp_path / 'source.cmd'; target.write_bytes(b'original')
    actual = os.fstat
    calls = []
    def changed(fd):
        original = actual(fd)
        calls.append(fd)
        if len(calls) == 1:
            return original
        values = {f: getattr(original, f) for f in
                  ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode')}
        values[field] ^= 1
        return SimpleNamespace(**values)
    monkeypatch.setattr(os, 'fstat', changed)
    with pytest.raises(ValueError, match='changed while'):
        builder.read_regular_file(target)
    assert len(calls) == 2
    with pytest.raises(OSError):
        actual(calls[0])


@pytest.mark.parametrize('phase', ['open', 'read'])
@pytest.mark.parametrize('mutation', ['replacement', 'same-size-write', 'growth'])
def test_public_reader_rejects_actual_file_races(tmp_path, monkeypatch, phase, mutation):
    target = tmp_path / 'source.cmd'; target.write_bytes(b'original')
    reached = []
    original_open, original_fdopen = os.open, os.fdopen
    def mutate():
        reached.append(True)
        if mutation == 'replacement':
            other = tmp_path / 'new'; other.write_bytes(b'replaced'); other.replace(target)
        else:
            target.write_bytes(b'modified' if mutation == 'same-size-write' else b'original grows')
            # Deterministic on filesystems with coarser timestamps, no sleep.
            st = target.stat(); os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    def opening(path, flags, *args, **kw):
        if Path(path) == target and phase == 'open': mutate()
        return original_open(path, flags, *args, **kw)
    def reading(fd, *args, **kw):
        if phase == 'read': mutate()
        return original_fdopen(fd, *args, **kw)
    monkeypatch.setattr(os, 'open', opening)
    monkeypatch.setattr(os, 'fdopen', reading)
    with pytest.raises((ValueError, PermissionError)) as caught:
        builder.read_regular_file(target)
    assert reached == [True]
    if isinstance(caught.value, PermissionError):
        assert os.name == 'nt' and phase == 'read' and mutation == 'replacement'
    else:
        assert 'changed' in str(caught.value)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink creation; reparse rejection modeled separately')
def test_public_reader_rejects_symlink_at_actual_open(tmp_path, monkeypatch):
    target = tmp_path / 'source.cmd'; target.write_bytes(b'original')
    other = tmp_path / 'synthetic-private'; other.write_bytes(b'private')
    original = os.open; reached = []
    def opening(path, flags, *args, **kw):
        if Path(path) == target:
            reached.append(True); target.unlink(); target.symlink_to(other)
        return original(path, flags, *args, **kw)
    monkeypatch.setattr(os, 'open', opening)
    with pytest.raises((OSError, ValueError)):
        builder.read_regular_file(target)
    assert reached == [True] and other.read_bytes() == b'private'


@pytest.mark.parametrize('modern', [False, True])
@pytest.mark.parametrize('change', [None, 'st_ctime_ns', 'st_mode'])
def test_windows_reader_keeps_separate_raw_path_and_descriptor_stamps(tmp_path, monkeypatch, modern, change):
    target = tmp_path / 'source.cmd'; target.write_bytes(b'original')
    before = windows_metadata(modern=modern)
    opened = windows_metadata(modern=modern, descriptor=True)
    actual_lstat, actual_fstat = Path.lstat, os.fstat
    monkeypatch.setattr(builder, '_WINDOWS', True)
    monkeypatch.setattr(Path, 'lstat', lambda p, *a, **kw: before if p == target else actual_lstat(p, *a, **kw))
    seen = []
    def measured(fd):
        seen.append(fd)
        if len(seen) > 1 and change:
            return windows_metadata(modern=modern, descriptor=True, **{change: getattr(opened, change) ^ 1})
        return opened
    monkeypatch.setattr(os, 'fstat', measured)
    if change:
        with pytest.raises(ValueError, match='changed while'):
            builder.read_regular_file(target, expected=builder.file_stamp(before))
    else:
        assert builder.read_regular_file(target, expected=builder.file_stamp(before)) == b'original'
    assert len(seen) == 2
    with pytest.raises(OSError):
        actual_fstat(seen[0])
