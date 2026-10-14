"""Shared PA9 publication safeguards for the editor and trade receiver.

Cooperating writers with the same user/host/temp-directory configuration use
the same path-keyed, crash-released locks. Creation
is atomic/no-clobber even against non-cooperating writers. Approved replacement
is checked under the lock, but is not a filesystem CAS against arbitrary other
programs; callers needing that guarantee must choose a new unique output path.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import threading
from typing import BinaryIO, Callable, Iterator

_PROCESS_SAVE_LOCKS_GUARD = threading.Lock()
_PROCESS_SAVE_LOCKS: dict[str, threading.Lock] = {}
MAX_IDENTITY_BYTES = 1024 * 1024
_WINDOWS = os.name == "nt"


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Filesystem identity used to reject changed overwrite targets."""

    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    mode: int
    sha256: str


def _save_lock_key(path: Path) -> str:
    # Key by directory entry, not inode.  Atomic replacement changes the inode
    # before a first saver releases its lock; a second preflight must still map
    # to the same lock during that interval.
    parent = os.path.normcase(os.path.realpath(os.fspath(path.parent)))
    name = os.path.normcase(path.name)
    material = os.fsencode(parent + "\0" + name)
    return hashlib.sha256(material).hexdigest()


@contextmanager
def exclusive_save_lock(path: Path):
    """Hold a crash-released process and OS lock for one output target."""

    key = _save_lock_key(path)
    with _PROCESS_SAVE_LOCKS_GUARD:
        process_lock = _PROCESS_SAVE_LOCKS.setdefault(key, threading.Lock())
    if not process_lock.acquire(blocking=False):
        raise FileExistsError(
            f"another PA9 save is already in progress: {path}"
        )

    descriptor: int | None = None
    os_locked = False
    try:
        user_suffix = f"-{os.geteuid()}" if hasattr(os, "geteuid") else ""
        lock_root = Path(tempfile.gettempdir()) / f"zatrade-pa9-locks-v1{user_suffix}"
        lock_root.mkdir(mode=0o700, exist_ok=True)
        root_metadata = lock_root.lstat()
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise RuntimeError(f"PA9 save lock path is not a directory: {lock_root}")
        if hasattr(os, "geteuid") and root_metadata.st_uid != os.geteuid():
            raise RuntimeError(f"PA9 save lock directory has the wrong owner: {lock_root}")
        lock_path = lock_root / f"{key}.lock"
        open_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, open_flags, 0o600)
        lock_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(lock_metadata.st_mode):
            raise RuntimeError(f"PA9 save lock is not a regular file: {lock_path}")
        if hasattr(os, "geteuid") and lock_metadata.st_uid != os.geteuid():
            raise RuntimeError(f"PA9 save lock has the wrong owner: {lock_path}")
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size < 1:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise FileExistsError(
                    f"another PA9 save is already in progress: {path}"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise FileExistsError(
                    f"another PA9 save is already in progress: {path}"
                ) from exc
        os_locked = True
        yield
    finally:
        try:
            if descriptor is not None:
                if os_locked:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        finally:
            process_lock.release()


def _absolute_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    # ``Path.is_absolute()`` leaves Windows drive-relative inputs such as
    # ``C:record.pa9`` unresolved.  abspath handles those using Windows' per-
    # drive current directory and gives every later safety check one stable
    # spelling to compare.
    return Path(os.path.abspath(os.fspath(path)))


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
        int(metadata.st_mode),
    )


def _opened_path_identity(path: Path, metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Represent an opened descriptor like CPython's Windows path stat.

    Path stat infers executable bits from the suffix and reports creation-time
    ctime. Modern fstat can report change-time ctime instead. This conversion
    is ONLY for cross-API comparisons; descriptor-to-descriptor checks retain
    the unmodified change time and mode via _metadata_identity.
    """
    values = list(_metadata_identity(metadata))
    if _WINDOWS:
        values[4] = int(getattr(metadata, "st_birthtime_ns", metadata.st_ctime_ns))
        if stat.S_ISREG(metadata.st_mode) and path.suffix.lower() in (".exe", ".com", ".bat", ".cmd"):
            values[5] |= 0o111
    return tuple(values)


@contextmanager
def open_regular_binary(
    path: str | Path, *, max_size: int | None = None, allow_symlink: bool = False,
) -> Iterator[BinaryIO]:
    """Open a pinned regular input, never wait for a substituted FIFO/device.

    O_NONBLOCK has no effect on ordinary disk reads. It prevents open itself
    from blocking on a special file raced into place; fstat then rejects it.
    This does not promise to interrupt an OS-blocked regular-file read.
    """
    if max_size is not None and (type(max_size) is not int or max_size < 0):
        raise ValueError("max_size must be a non-negative integer or None")
    target = _absolute_path(path)
    before = target.stat() if allow_symlink else target.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"input is not a regular file: {target}")
    if max_size is not None and before.st_size > max_size:
        raise ValueError(f"input exceeds {max_size} bytes: {target}")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
    if not allow_symlink:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"input is not a regular file: {target}")
        if _opened_path_identity(target, opened) != _metadata_identity(before):
            raise RuntimeError(f"input changed while opening: {target}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_regular_bytes(
    path: str | Path, *, max_size: int, allow_symlink: bool = False,
) -> bytes:
    """Bound the read itself as well as the pre-open size check."""
    with open_regular_binary(path, max_size=max_size, allow_symlink=allow_symlink) as stream:
        raw = stream.read(max_size + 1)
    if len(raw) > max_size:
        raise ValueError(f"input exceeds {max_size} bytes: {path}")
    return raw


def file_identity(path: str | Path) -> FileIdentity | None:
    """Return a regular file identity, rejecting links and special files."""

    target = _absolute_path(path)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"PA9 path is not a regular file: {target}")
    if metadata.st_size > MAX_IDENTITY_BYTES:
        raise ValueError(f"PA9 identity input exceeds {MAX_IDENTITY_BYTES} bytes: {target}")
    expected = _metadata_identity(metadata)
    with open_regular_binary(target, max_size=MAX_IDENTITY_BYTES) as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _opened_path_identity(target, opened) != expected:
            raise RuntimeError(f"file changed while checking identity: {target}")
        opened_identity = _metadata_identity(opened)
        raw = stream.read(MAX_IDENTITY_BYTES + 1)
        if len(raw) > MAX_IDENTITY_BYTES:
            raise ValueError(f"PA9 identity input exceeds {MAX_IDENTITY_BYTES} bytes: {target}")
        if _metadata_identity(os.fstat(stream.fileno())) != opened_identity:
            raise RuntimeError(f"file changed while checking identity: {target}")
    try:
        after = target.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"file changed while checking identity: {target}") from exc
    if _metadata_identity(after) != expected or not stat.S_ISREG(after.st_mode):
        raise RuntimeError(f"file changed while checking identity: {target}")
    return FileIdentity(*expected, hashlib.sha256(raw).hexdigest())


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _publish_no_clobber(temporary: Path, target: Path) -> None:
    """Atomically publish *temporary* only while *target* is absent."""

    if os.name == "nt":
        try:
            os.rename(temporary, target)
        except OSError as exc:
            if _path_entry_exists(target):
                raise FileExistsError(
                    f"PA9 output appeared after approval; refusing to overwrite: {target}"
                ) from exc
            raise
        return

    try:
        # POSIX rename replaces an existing destination.  A same-filesystem
        # hard link gives the required atomic no-clobber publication instead.
        os.link(temporary, target)
    except FileExistsError as exc:
        raise FileExistsError(
            f"PA9 output appeared after approval; refusing to overwrite: {target}"
        ) from exc
    except OSError as exc:
        raise OSError(
            "the output filesystem does not support atomic no-clobber "
            f"PA9 saves: {target.parent}"
        ) from exc


def _sync_output_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, flags)
        os.fsync(descriptor)
    except OSError as exc:
        unsupported = {
            errno.EINVAL,
            errno.ENOSYS,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if exc.errno not in unsupported:
            raise OSError(
                exc.errno,
                "PA9 output was published, but its directory durability sync "
                f"failed; verify the saved file: {directory}",
            ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _cleanup_owned_temporary(temporary: Path) -> Exception | None:
    """Remove our temp, reporting cleanup failure without a pathname retry."""

    try:
        temporary.unlink()
        return None
    except FileNotFoundError:
        return None
    except OSError as cleanup_error:
        # A chmod-and-retry sequence would introduce a check/use race where a
        # different same-user file could be substituted and deleted.  Existing
        # read-only Windows targets are rejected before temp creation instead.
        return cleanup_error



def publish_approved(
    temporary: Path,
    target: Path,
    expected_identity: FileIdentity | None,
    *,
    read_identity: Callable[[Path], FileIdentity | None] = file_identity,
) -> None:
    """Publish under ``exclusive_save_lock``, never clobber an absent target.

    The identity reader is injectable so callers can retain their existing
    fault tests without weakening the real file identity contract.
    """
    current = read_identity(target)
    if current != expected_identity:
        raise FileExistsError(
            f"PA9 output changed after approval; refusing to overwrite: {target}"
        )
    if expected_identity is None:
        _publish_no_clobber(temporary, target)
    else:
        os.replace(temporary, target)
