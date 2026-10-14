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
