#!/usr/bin/env sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
requirements=requirements-za.txt
case "${1:-}" in
    --live) requirements=requirements.txt ;;
    "") ;;
    --help|-h) printf '%s\n' 'Usage: sh scripts/setup_za.sh [--live]' 'Creates .venv-za and installs Python dependencies only. Does not change Wi-Fi or install OS packages.'; exit 0 ;;
    *) printf '%s\n' 'Unknown option; use --help.' >&2; exit 2 ;;
esac
python=${PYTHON:-python3}
"$python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else "Python 3.10+ required")'
if [ "$requirements" = requirements.txt ]; then
    "$python" -c 'import sys; raise SystemExit(0 if sys.platform.startswith("linux") and sys.version_info >= (3, 12) else "Linux / Python 3.12+ required for live radio")'
fi
if [ ! -x "$root/.venv-za/bin/python" ]; then
    "$python" -m venv "$root/.venv-za"
fi
venv_python="$root/.venv-za/bin/python"
# PYTHON chooses the bootstrap interpreter, not an already-existing venv.
# Reject an incompatible existing environment before pip can modify it.
"$venv_python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else "Existing .venv-za requires Python 3.10+; recreate it with a supported Python.")'
if [ "$requirements" = requirements.txt ]; then
    "$venv_python" -c 'import sys; raise SystemExit(0 if sys.platform.startswith("linux") and sys.version_info >= (3, 12) else "Existing .venv-za requires Linux / Python 3.12+ for live radio; recreate it with a supported Python.")'
fi
"$root/.venv-za/bin/python" -m pip install -r "$root/$requirements"
"$root/.venv-za/bin/python" -s -B "$root/zatrade.py" doctor
printf '%s\n' "Start: sh \"$root/scripts/start_za_gui.sh\""
