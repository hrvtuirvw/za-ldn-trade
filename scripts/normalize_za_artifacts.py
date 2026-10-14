"""Normalize build timestamps/owner metadata without changing package contents."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import zipfile

from build_za_release import (no_links, source_version, read_regular_file,
                             MAX_RELEASE_BYTES, MAX_RELEASE_MEMBERS)


def safe_name(name):
    path=PurePosixPath(name)
    if (not name or name == '.' or str(path) != name or path.is_absolute()
            or '..' in path.parts or '\\' in name or ':' in name
            or any(ord(character) < 32 or ord(character) == 127 for character in name)):
        raise ValueError('unsafe archive member')


def safe_zip_name(entry):
    # ZipInfo retains the decoded on-wire name before truncating NUL or
    # replacing host separators. Validate that value before any normalization.
    safe_name(entry.orig_filename)
    if entry.filename != entry.orig_filename:
        raise ValueError('unsafe normalized ZIP member')


def bounded_tar_data(data):
    """Cap the entire TAR stream, including PAX headers/padding, before parsing."""
    if len(data)>MAX_RELEASE_BYTES:raise ValueError('artifact exceeds size limit')
    with gzip.GzipFile(fileobj=io.BytesIO(data),mode='rb') as source:
        raw=source.read(MAX_RELEASE_BYTES+1)
    if len(raw)>MAX_RELEASE_BYTES:raise ValueError('tar expansion exceeds size limit')
    return raw


def normalize_tar(data):
    uncompressed=bounded_tar_data(data)
    before={};buffer=io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(uncompressed),mode='r:') as original:
        members=[];names=set();total=0
        for member in original:
            if len(members)>=MAX_RELEASE_MEMBERS:raise ValueError('too many tar members')
            safe_name(member.name)
            if member.name in names:raise ValueError('duplicate tar member')
            if not (member.isfile() or member.isdir()):raise ValueError('nonregular tar member')
            total+=member.size
            if member.size<0 or total>MAX_RELEASE_BYTES:raise ValueError('tar expansion exceeds size limit')
            members.append(member);names.add(member.name)
        with gzip.GzipFile(fileobj=buffer,mode='wb',filename='',mtime=0) as compressed:
            with tarfile.open(fileobj=compressed,mode='w',format=tarfile.PAX_FORMAT) as target:
                for old in sorted(members,key=lambda m:m.name):
                    safe_name(old.name)
                    if not (old.isfile() or old.isdir()):raise ValueError('nonregular tar member')
                    raw=original.extractfile(old).read() if old.isfile() else None
                    if raw is not None:before[old.name]=hashlib.sha256(raw).hexdigest()
                    item=copy.copy(old);item.uid=item.gid=0;item.uname=item.gname=''
                    item.mtime=1767225600;item.pax_headers={}
                    item.mode=0o755 if old.isdir() or old.mode&0o111 else 0o644
                    target.addfile(item,io.BytesIO(raw) if raw is not None else None)
    result=buffer.getvalue()
    with tarfile.open(fileobj=io.BytesIO(result),mode='r:gz') as archive:
        after={m.name:hashlib.sha256(archive.extractfile(m).read()).hexdigest() for m in archive.getmembers() if m.isfile()}
        assert all(not m.uname and not m.gname and m.uid==m.gid==0 for m in archive.getmembers())
    if before!=after:raise ValueError('normalization changed tar content')
    return result


def normalize_wheel(data):
    if len(data)>MAX_RELEASE_BYTES:raise ValueError('artifact exceeds size limit')
    buffer=io.BytesIO();before={}
    with zipfile.ZipFile(io.BytesIO(data)) as original:
        entries=original.infolist()
        if len(entries)>MAX_RELEASE_MEMBERS:raise ValueError('too many wheel members')
        if len({entry.filename for entry in entries})!=len(entries):raise ValueError('duplicate wheel member')
        if sum(entry.file_size for entry in entries)>MAX_RELEASE_BYTES:raise ValueError('wheel expansion exceeds size limit')
        for entry in entries:
            safe_zip_name(entry)
            if entry.is_dir() or stat.S_IFMT(entry.external_attr>>16) not in (0,stat.S_IFREG):
                raise ValueError('nonregular wheel member')
        with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as target:
            for name in sorted(original.namelist()):
                safe_name(name)
                raw=original.read(name);before[name]=hashlib.sha256(raw).hexdigest()
                info=zipfile.ZipInfo(name,(2026,1,1,0,0,0));info.create_system=3
                info.external_attr=(0o100644<<16);info.compress_type=zipfile.ZIP_DEFLATED
                target.writestr(info,raw,compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    result=buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        after={name:hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()}
        if archive.testzip() is not None:raise ValueError('normalized wheel CRC failed')
    if before!=after:raise ValueError('normalization changed wheel content')
    return result


def normalize(directory,version):
    directory=no_links(Path(os.path.abspath(directory)))
    paths=[directory/('za_ldn_trade-'+version+'.tar.gz'),directory/('za_ldn_trade-'+version+'-py3-none-any.whl')]
    for path in paths:
        no_links(path)
        if not path.is_file():raise ValueError('expected build artifact missing')
        no_links(Path(str(path)+'.normalized'))
        if os.path.lexists(str(path)+'.normalized'):raise ValueError('stale normalization staging file')
    # Parse/validate BOTH complete inputs before changing either artifact.
    # Publication is per-file atomic, not a cross-file filesystem transaction.
    planned=[]
    for path,transform in zip(paths,(normalize_tar,normalize_wheel)):
        old=read_regular_file(path);raw=transform(old)
        planned.append((path,old,raw))
    results=[]
    for path,old,raw in planned:
        temp=Path(str(path)+'.normalized')
        created=False
        try:
            with temp.open('xb') as stream:
                created=True;stream.write(raw);stream.flush();os.fsync(stream.fileno())
            if read_regular_file(path)!=old:raise ValueError('artifact changed during normalization')
            os.replace(temp,path)
            results.append({'file':path.name,'sha256':hashlib.sha256(raw).hexdigest(),'content_preserved':True})
        finally:
            if created:
                try:temp.unlink()
                except FileNotFoundError:pass
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',type=Path,required=True)
    args=parser.parse_args();root=Path(__file__).absolute().parent.parent
    version=source_version((root/'zasim/__init__.py').read_bytes())
    print(json.dumps(normalize(args.directory,version),indent=2))
