"""Build/check the optional helper as a local bundle, never load or install it.

The C source is identical to the successful development module. Kernel headers
and compiler come from the user's trusted OS installation; no download, root,
DKMS, modprobe configuration or persistent system change is performed here.
"""
from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import subprocess
import sys
import tempfile

from zasim.ldn.bss_seed import MODULE_NAME, SOURCE_SHA256, MAX_MODULE_BYTES, module_info, regular_bytes, validate_bundle


def build_bundle(output: str | Path, *, kernel_build: str | Path | None = None) -> dict:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("build the BSS helper on the Linux machine that owns the Wi-Fi adapter")
    release = platform.release()
    if re.fullmatch(r"[A-Za-z0-9_.+-]+", release) is None:
        raise ValueError("unsupported kernel release name")
    destination = Path(output).expanduser().absolute()
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError("helper output must not use symlinks")
    if os.path.lexists(destination):
        raise FileExistsError("choose a new helper bundle directory; existing bundles are never overwritten")
    headers = Path(kernel_build or f"/lib/modules/{release}/build").resolve()
    if re.fullmatch(r"/[A-Za-z0-9_./+-]+", str(headers)) is None:
        raise ValueError("kernel build path cannot contain whitespace or shell metacharacters")
    try:
        header_release = regular_bytes(headers / "include/config/kernel.release", 256).decode("ascii").strip()
        config = regular_bytes(headers / ".config", 2 * 1024 * 1024).decode("ascii")
    except OSError as exc:
        raise RuntimeError("matching kernel headers/build tree missing; install headers for the running kernel using your OS package manager") from exc
    if header_release != release:
        raise ValueError("kernel headers do not match the running kernel; no build attempted")
    source = files("zasim").joinpath("data/bss_seed/ldn_bss_seed.c").read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError("packaged BSS helper source checksum mismatch")
    use_clang = "CONFIG_CC_IS_CLANG=y" in config.splitlines()
    required = ("make", "clang", "ld.lld") if use_clang else ("make", "gcc", "ld")
    for tool in required:
        if shutil.which(tool) is None:
            raise RuntimeError(f"kernel module build requires {tool}; install the matching OS build tools")
    # M= must not inherit untrusted shell metacharacters through TMPDIR. User
    # output paths never reach Kbuild. Build only as the invoking user.
    with tempfile.TemporaryDirectory(prefix="za-bss-build-", dir="/tmp") as temporary:
        stage = Path(temporary)
        (stage / (MODULE_NAME + ".c")).write_bytes(source)
        (stage / "Kbuild").write_text("obj-m := ldn_bss_seed.o\n")
        command = [shutil.which("make"), "-C", str(headers), f"M={stage}"]
        command += ["LLVM=1"] if use_clang else ["CC=gcc", "LD=ld"]
        command += ["modules"]
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"MAKEFLAGS", "MFLAGS", "MAKEFILES", "KBUILD_OUTPUT", "KBUILD_EXTMOD", "ARCH", "CROSS_COMPILE", "CC", "LD", "LLVM", "KCONFIG_CONFIG", "KCFLAGS", "KCPPFLAGS"}}
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, env=environment, start_new_session=True)
            try:
                code = process.wait(timeout=180)
            except BaseException as exc:
                # Kbuild launches compiler children. Kill only our new process
                # group, not just make, before removing its temporary files.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise RuntimeError("BSS helper build exceeded 180 seconds") from exc
                raise
            if code:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 1600))
                detail = log.read(1600).decode("utf-8", "replace")
                raise RuntimeError("BSS helper compilation failed (no module loaded):\n" + detail)
        image = regular_bytes(stage / (MODULE_NAME + ".ko"), MAX_MODULE_BYTES)
        info = module_info(image)
        if info.get("name") != MODULE_NAME or not info.get("vermagic", "").startswith(release + " ") or info.get("license") != "GPL":
            raise RuntimeError("compiled helper identity does not match this kernel")
        if regular_bytes(headers / "include/config/kernel.release", 256).decode("ascii").strip() != release or platform.release() != release:
            raise RuntimeError("kernel/build identity changed during compilation")
        receipt = {"schema": 1, "kernel_release": release, "source_sha256": SOURCE_SHA256,
                   "module_sha256": hashlib.sha256(image).hexdigest(),
                   "compiler_family": "clang" if use_clang else "gcc", "loaded": False}
        # Publish to a fresh private directory only after a successful build.
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name, data in ((MODULE_NAME + ".ko", image), ("BUILD.json", (json.dumps(receipt, indent=2) + "\n").encode())):
            fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
    validate_bundle(destination)
    return {"bundle": str(destination), **receipt, "system_installation_changed": False}


def command(args) -> int:
    if args.helper_action == "build":
        result = build_bundle(args.output, kernel_build=args.kernel_build)
    else:
        bundle = validate_bundle(args.bundle)
        result = {"valid": True, "bundle": str(bundle.directory), "kernel_release": bundle.kernel_release,
                  "module_sha256": bundle.sha256, "module_load_attempted": False,
                  "physical_operation_verified": False}
    print(json.dumps(result, indent=2))
    return 0
