[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$GuiArguments
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repoRoot ".venv-za\Scripts\python.exe"
$entryPoint = Join-Path $repoRoot "zatrade.py"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw @"
The Windows GUI environment is missing: $python
Create it with:
  py -3 -m venv "$repoRoot\.venv-za"
  & "$python" -m pip install -r "$repoRoot\requirements-za.txt"
"@
}

$pythonVersion = (& $python -c "import sys; print('.'.join(map(str, sys.version_info[:3]))); raise SystemExit(0 if sys.version_info >= (3, 10) else 3)" | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "The Z-A GUI requires Python 3.10 or newer; the GUI environment reported '$pythonVersion'. Recreate .venv-za with a newer Python."
}

& $python -c "import Crypto, zstandard"
if ($LASTEXITCODE -ne 0) {
    throw "The Z-A GUI dependencies are incomplete. Run: `"$python`" -m pip install -r `"$repoRoot\requirements-za.txt`""
}

& $python -c "import tkinter"
if ($LASTEXITCODE -ne 0) {
    throw "This Python installation does not include Tkinter. Install a standard Python for Windows with Tcl/Tk support, recreate .venv-za, and try again."
}

& $python $entryPoint gui @GuiArguments
exit $LASTEXITCODE
