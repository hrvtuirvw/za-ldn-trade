#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
python="${ZA_TRADE_PYTHON:-$repo_root/.venv-za/bin/python}"
entry_point="$repo_root/zatrade.py"

if [ ! -x "$python" ]; then
    printf '%s\n' \
        "The Linux GUI environment is missing: $python" \
        "Create it with:" \
        "  python3 -m venv \"$repo_root/.venv-za\"" \
        "  \"$python\" -m pip install -r \"$repo_root/requirements-za.txt\"" >&2
    exit 2
fi

if ! python_version=$("$python" -c "import sys; print('.'.join(map(str, sys.version_info[:3]))); raise SystemExit(0 if sys.version_info >= (3, 10) else 3)"); then
    printf '%s\n' \
        "The Z-A GUI requires Python 3.10 or newer; the GUI environment reported '${python_version:-unknown}'." \
        "Recreate .venv-za with a newer Python." >&2
    exit 2
fi

if ! "$python" -c 'import Crypto, zstandard'; then
    printf '%s\n' \
        "The Z-A GUI dependencies are incomplete." \
        "Run: \"$python\" -m pip install -r \"$repo_root/requirements-za.txt\"" >&2
    exit 2
fi

if ! "$python" -c 'import tkinter'; then
    printf '%s\n' \
        "Tkinter is not available for this Python installation." \
        "On Debian/Ubuntu, install it with: sudo apt install python3-tk" \
        "Then recreate .venv-za if necessary and try again." >&2
    exit 2
fi

export PYTHONDONTWRITEBYTECODE=1
exec "$python" -s -B "$entry_point" gui "$@"
