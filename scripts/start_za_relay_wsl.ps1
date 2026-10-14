[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu",
    [string]$Keys = "~/.switch/prod.keys",
    [string]$Phy = "",
    [string]$Interface = "ldnclient",
    [string]$Python = "python3",
    [ValidateRange(1, 65535)]
    [int]$Port = 24872,
    [switch]$Setup,
    [switch]$SingleInterface,
    [switch]$Once
)

$ErrorActionPreference = "Stop"

function Invoke-WslChecked {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & wsl.exe -d $Distro --exec @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed (exit $LASTEXITCODE): $($Arguments -join ' ')"
    }
}

function Convert-ToWslPath {
    param([Parameter(Mandatory = $true)][string]$PathValue)

    if ($PathValue.StartsWith("/")) {
        return $PathValue
    }
    if ($PathValue.StartsWith("~/")) {
        return "$script:WslHome/$($PathValue.Substring(2))"
    }
    $absolute = [System.IO.Path]::GetFullPath($PathValue)
    $converted = (& wsl.exe -d $Distro --exec wslpath -a -u $absolute | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($converted)) {
        throw "Could not translate Windows path for WSL: $PathValue"
    }
    return $converted
}

& wsl.exe -d $Distro --exec true
if ($LASTEXITCODE -ne 0) {
    throw "WSL distribution '$Distro' is unavailable. Install/start WSL2 first."
}

$script:WslHome = (& wsl.exe -d $Distro --exec printenv HOME | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($script:WslHome)) {
    throw "Could not determine the WSL home directory for '$Distro'."
}

$pythonVersion = (& wsl.exe -d $Distro --exec $Python -c "import sys; print('.'.join(map(str, sys.version_info[:3]))); raise SystemExit(0 if sys.version_info >= (3, 12) else 3)" | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "The live LDN relay requires Python 3.12 or newer; '$Python' reported '$pythonVersion'. Use Ubuntu 24.04+ or pass -Python with a newer interpreter."
}

$repoWindows = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoWsl = Convert-ToWslPath $repoWindows
$keysWsl = Convert-ToWslPath $Keys
$venvWsl = "$script:WslHome/.venvs/zatrade"
$pythonWsl = "$venvWsl/bin/python"

if ($Setup) {
    Write-Host "Creating/updating the WSL Python environment..."
    Invoke-WslChecked -Arguments @($Python, "-m", "venv", $venvWsl)
    Invoke-WslChecked -Arguments @(
        $pythonWsl, "-m", "pip", "install", "--disable-pip-version-check",
        "-r", "$repoWsl/requirements.txt"
    )
}

& wsl.exe -d $Distro --exec test -x $pythonWsl
if ($LASTEXITCODE -ne 0) {
    throw "WSL environment is missing. Re-run this script once with -Setup."
}

$runtimePythonVersion = (& wsl.exe -d $Distro --exec $pythonWsl -c "import sys; print('.'.join(map(str, sys.version_info[:3]))); raise SystemExit(0 if sys.version_info >= (3, 12) else 3)" | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "The WSL relay environment requires Python 3.12 or newer; '$pythonWsl' reported '$runtimePythonVersion'. Re-run this script with -Setup using a newer -Python interpreter."
}

& wsl.exe -d $Distro --exec $pythonWsl -c "import Crypto, ldn, trio, zstandard"
if ($LASTEXITCODE -ne 0) {
    throw "WSL Python dependencies are incomplete. Re-run this script with -Setup."
}

& wsl.exe -d $Distro --exec sh -lc "command -v iw >/dev/null && command -v ip >/dev/null && command -v nmcli >/dev/null && command -v sysctl >/dev/null && command -v pkill >/dev/null"
if ($LASTEXITCODE -ne 0) {
    throw "WSL needs iw, iproute2, NetworkManager, procps, and sysctl. Install the missing distro packages first."
}

$kernel = (& wsl.exe -d $Distro --exec uname -r | Out-String).Trim()
$selectedPhy = $Phy
if ([string]::IsNullOrWhiteSpace($Phy)) {
    # usbipd detach/attach may renumber the same adapter (for example phy0 to
    # phy11).  Select from interface ownership first, without using capability
    # output from an unselected radio.  Silently choosing among multiple radios
    # could take the user's normal Wi-Fi down.
    $iwDev = (& wsl.exe -d $Distro --exec /usr/bin/env iw dev | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "WSL could not enumerate Wi-Fi PHYs. Attach the adapter with usbipd-win or pass -Phy explicitly."
    }
    $seenPhys = @(
        [regex]::Matches($iwDev, "(?m)^\s*phy#(?<index>[0-9]+)\s*$") |
            ForEach-Object { "phy$($_.Groups['index'].Value)" } |
            Select-Object -Unique
    )
    if ($seenPhys.Count -eq 0) {
        throw "No WSL Wi-Fi PHY was found. Attach a dedicated compatible USB adapter with usbipd-win; see docs/WIRELESS_COMPATIBILITY.md."
    }
    if ($seenPhys.Count -ne 1) {
        throw "Multiple WSL Wi-Fi PHYs were found ($($seenPhys -join ', ')). Re-run with -Phy NAME to select the dedicated adapter."
    }
    $selectedPhy = $seenPhys[0]
    Write-Host "Auto-selected WSL Wi-Fi PHY: $selectedPhy"
}

# One shared read-only prerequisite check, on the selected PHY only.
# iw's OVER_NL80211 line does not establish custom EtherType support: doctor
# queries the separate GET_WIPHY flag and also checks channels/CCMP/busy state.
$radioJson = (& wsl.exe -d $Distro --exec $pythonWsl -s -B "$repoWsl/zatrade.py" doctor --backend direct --radio --phy $selectedPhy --json | Out-String)
$radioExit = $LASTEXITCODE
try {
    $radioReport = $radioJson | ConvertFrom-Json
} catch {
    throw "Could not parse the selected PHY's read-only diagnostic. Run zatrade.py doctor --backend direct --radio --phy $selectedPhy inside WSL."
}
if ($radioExit -ne 0 -or $radioReport.wireless.status -ne "requirements_present_not_verified" -or $radioReport.wireless.selected_phy -ne $selectedPhy -or $radioReport.live_runtime_ready -ne $true) {
    throw "Wireless prerequisites not met for '$selectedPhy' (status: $($radioReport.wireless.status)). Run zatrade.py doctor --backend direct --radio --phy $selectedPhy inside WSL; see docs/WIRELESS_COMPATIBILITY.md. No radio was changed."
}
Write-Host "Wireless prerequisite flags present; actual action frames, LDN join and exchange are NOT certified."
foreach ($note in $radioReport.wireless.driver_notes) { Write-Host $note }
$Phy = $selectedPhy

& wsl.exe -d $Distro --exec test -f $keysWsl
if ($LASTEXITCODE -ne 0) {
    throw "The key file does not exist inside WSL: $keysWsl"
}

Write-Host "WSL kernel: $kernel"
Write-Host "Starting loopback-only relay on 127.0.0.1:$Port"
Write-Host "Keep this terminal open, then run the Windows trade command from a second PowerShell window."

$relayArguments = @(
    "sudo", "-E", $pythonWsl, "$repoWsl/zatrade.py", "relay-serve",
    "--listen", "127.0.0.1:$Port", "--keys", $keysWsl,
    "--phy", $Phy, "--interface", $Interface
)
if ($SingleInterface) {
    $relayArguments += "--single-interface"
    Write-Host "Forcing single-interface reuse of the physical netdev on $Phy."
}
if ($Once) {
    $relayArguments += "--once"
    Write-Host "Diagnostic mode: the relay will exit after one client session."
}

Invoke-WslChecked -Arguments $relayArguments
