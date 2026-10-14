"""Setuptools module selection using the same allowlist as public source export.

Build from the reviewed export (see docs/RELEASING.md), never publish the
development checkout. Legacy research files may remain in a checkout without
being copied into its wheel. Tests and build tooling are not runtime packages.
"""
from pathlib import Path
from runpy import run_path

from setuptools.command.build_py import build_py

# Setuptools loads cmdclass by file spec before the source root is necessarily
# on sys.path (notably isolated PEP 517 builds). Do not import scripts as a
# runtime package or depend on the caller's working directory.
MODULES = run_path(str(Path(__file__).with_name("build_za_release.py")))["MODULES"]


class PublicBuildPy(build_py):
    def find_package_modules(self, package, package_dir):
        allowed = MODULES.get(package, ())
        return [item for item in super().find_package_modules(package, package_dir)
                if item[1] in allowed]
