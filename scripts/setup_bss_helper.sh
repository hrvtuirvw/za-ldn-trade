#!/usr/bin/env sh
# Build an optional local helper bundle. Never load/install a kernel module.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "${1:-}" in
    --help|-h) printf '%s\n' 'Usage: sh scripts/setup_bss_helper.sh [NEW_OUTPUT_DIRECTORY]' 'Requires Linux, matching running-kernel headers and build tools.' 'Builds a local bundle only: no sudo, module load, driver replacement or autoload.'; exit 0 ;;
    -*) printf '%s\n' 'Unknown option; use --help.' >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then printf '%s\n' 'Only one optional output directory is accepted.' >&2; exit 2; fi
python=${PYTHON:-"$root/.venv-za/bin/python"}
if ! python=$(command -v "$python") || [ ! -x "$python" ]; then
    printf '%s\n' 'Run sh scripts/setup_za.sh --live first, or set PYTHON to your Python executable.' >&2
    exit 2
fi
output=${1:-"$root/bss-helper/$(uname -r)"}
"$python" -s -B "$root/zatrade.py" bss-helper build --output "$output"
printf '%s\n' "In the Linux GUI: Advanced settings → AWUS036ACH connection helper → Browse → $output" 'Leave the field empty to use the ordinary connection path.'
