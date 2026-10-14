#!/usr/bin/env python3
"""Convenience entry point for the za-ldn-trade desktop GUI."""

from __future__ import annotations

import sys

from zatrade import main


if __name__ == "__main__":
    raise SystemExit(main(["gui", *sys.argv[1:]]))
