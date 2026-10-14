$ErrorActionPreference = "Stop"

$launcher = (Resolve-Path (
    Join-Path $PSScriptRoot "..\scripts\start_za_relay_wsl.ps1"
)).Path
$global:MockCalls = [System.Collections.Generic.List[string]]::new()
$global:MockScenario = ""
$global:VersionProbeCount = 0

function global:wsl.exe {
    $command = $args -join " "
    $global:MockCalls.Add($command)
    $global:LASTEXITCODE = 0

    if ($command -match " --exec printenv HOME$") {
        return "/home/mock"
    }
    if ($command -match " --exec wslpath -a -u ") {
        return "/mnt/d/mock"
    }
    if ($command -match " --exec .* -c import sys;") {
        $global:VersionProbeCount += 1
        if ($global:MockScenario -eq "old-python") {
            $global:LASTEXITCODE = 3
            return "3.11.9"
        }
        if (
            $global:MockScenario -eq "old-runtime-python" -and
            $global:VersionProbeCount -eq 2
        ) {
            $global:LASTEXITCODE = 3
            return "3.11.9"
        }
        return "3.12.8"
    }
    if ($command -match " --exec uname -r$") {
        return "6.6.87.2-microsoft-standard-WSL2"
    }
    if ($command -match " --exec /usr/bin/env iw dev$") {
        return @"
phy#0
	Interface wlan0
		type managed
phy#1
	Interface wlan1
		type managed
"@
    }
    if ($command -match " doctor --backend direct --radio --phy (?<phy>phy[0-9]+) --json$") {
        $phy = $Matches["phy"]
        $status = "requirements_present_not_verified"
        $reportedPhy = $phy
        if ($global:MockScenario -in @("missing-control-port", "missing-ethertype", "busy-radio")) {
            $status = "requirements_missing"
            $global:LASTEXITCODE = 2
        }
        if ($global:MockScenario -eq "unknown-capability") {
            $status = "unknown"
            $global:LASTEXITCODE = 3
        }
        if ($global:MockScenario -eq "wrong-phy") { $reportedPhy = "phy99" }
        if ($global:MockScenario -eq "bad-json") { return "invalid JSON" }
        return (@{
            live_runtime_ready = $true
            wireless = @{status = $status; selected_phy = $reportedPhy; driver_notes = @("Not a physical exchange test")}
        } | ConvertTo-Json -Depth 4 -Compress)
    }
}

function Reset-Mock([string]$Scenario) {
    $global:MockScenario = $Scenario
    $global:MockCalls.Clear()
    $global:VersionProbeCount = 0
    $global:LASTEXITCODE = 0
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) {
        throw "ASSERTION FAILED: $Message"
    }
}

function Invoke-ExpectFailure([scriptblock]$Action, [string]$Pattern) {
    $caught = $null
    try {
        & $Action
    } catch {
        $caught = $_.Exception.Message
    }
    Assert-True ($null -ne $caught) "launcher unexpectedly succeeded"
    Assert-True ($caught -match $Pattern) (
        "error '$caught' did not match '$Pattern'"
    )
}

# The base interpreter check must stop before importing ldn, whose syntax is
# not compatible with older Python versions.
Reset-Mock "old-python"
Invoke-ExpectFailure {
    & $launcher -Setup -Keys ".\mock-prod.keys"
} "Python 3\.12 or newer"
Assert-True (
    @($global:MockCalls | Where-Object { $_ -match "import Crypto" }).Count -eq 0
) "dependency import ran after the Python version check failed"
Assert-True (
    @($global:MockCalls | Where-Object { $_ -match " -m venv " }).Count -eq 0
) "venv setup ran before the base Python version check passed"
Assert-True (
    @($global:MockCalls | Where-Object { $_ -match " -m pip install " }).Count -eq 0
) "dependency setup ran before the base Python version check passed"

# A stale venv can differ from the interpreter used to create/update it.  Its
# own version must also be rejected before importing ldn.
Reset-Mock "old-runtime-python"
Invoke-ExpectFailure {
    & $launcher -Setup -Keys ".\mock-prod.keys"
} "relay environment requires Python 3\.12 or newer"
Assert-True ($global:VersionProbeCount -eq 2) "runtime Python was not checked"
Assert-True (
    @($global:MockCalls | Where-Object { $_ -match " -m venv " }).Count -eq 1
) "setup did not use the version-checked base interpreter"
Assert-True (
    @($global:MockCalls | Where-Object { $_ -match "import Crypto" }).Count -eq 0
) "dependency import ran after the runtime Python version check failed"

# Auto-selection must remain ambiguous when two PHYs exist.  It must not probe
# both radios and use their capabilities to silently choose one of them, even
# when only one would be capable.
Reset-Mock "one-of-two-capable"
Invoke-ExpectFailure {
    & $launcher -Keys ".\mock-prod.keys"
} "Multiple WSL Wi-Fi PHYs were found \(phy0, phy1\)"
Assert-True (
    @($global:MockCalls | Where-Object { $_ -match " doctor --backend direct --radio --phy " }).Count -eq 0
) "an unselected PHY's capabilities were queried"

# An explicit choice is queried exactly once and the selected PHY's capability
# record alone is sufficient for a successful preflight and relay launch.
Reset-Mock "selected-capable"
& $launcher -Keys ".\mock-prod.keys" -Phy "phy1"
$phyCalls = @($global:MockCalls | Where-Object { $_ -match " doctor --backend direct --radio --phy " })
Assert-True ($phyCalls.Count -eq 1) "selected PHY was not queried exactly once"
Assert-True (
    $phyCalls[0] -match " doctor --backend direct --radio --phy phy1 --json$"
) "capability query was not scoped to the selected PHY"
Assert-True (
    @($global:MockCalls | Where-Object { $_ -match " doctor --backend direct --radio --phy phy0 --json$" }).Count -eq 0
) "unselected capable PHY was queried"
$relayCalls = @(
    $global:MockCalls |
        Where-Object { $_ -match "(?:^|/)zatrade\.py relay-serve " }
)
Assert-True ($relayCalls.Count -eq 1) "relay was not launched exactly once"
Assert-True (
    $relayCalls[0] -notmatch " --single-interface(?: |$)"
) "single-interface was forced without -SingleInterface"
Assert-True (
    $relayCalls[0] -notmatch " --once(?: |$)"
) "one-session mode was enabled without -Once"

# The launcher must provide an explicit escape hatch for drivers such as
# rtw_8812au where creating a virtual interface is not the desired path.  The
# physical netdev name and diagnostic one-session mode must reach relay-serve
# as separate argv entries.
Reset-Mock "selected-capable"
& $launcher `
    -Keys ".\mock-prod.keys" `
    -Phy "phy11" `
    -Interface "wlx00c0cabab563" `
    -SingleInterface `
    -Once
$relayCalls = @(
    $global:MockCalls |
        Where-Object { $_ -match "(?:^|/)zatrade\.py relay-serve " }
)
Assert-True ($relayCalls.Count -eq 1) "explicit relay was not launched exactly once"
Assert-True (
    $relayCalls[0] -match " --phy phy11 --interface wlx00c0cabab563(?: |$)"
) "selected PHY/interface were not forwarded to relay-serve"
Assert-True (
    $relayCalls[0] -match " --single-interface(?: |$)"
) "-SingleInterface was not forwarded to relay-serve"
Assert-True (
    $relayCalls[0] -match " --once(?: |$)"
) "-Once was not forwarded to relay-serve"

# managed+monitor alone is insufficient: LDN authentication needs the exact
# nl80211 control-port feature on that same Wiphy.
Reset-Mock "missing-control-port"
Invoke-ExpectFailure {
    & $launcher -Keys ".\mock-prod.keys" -Phy "phy1"
} "Wireless prerequisites not met"
$phyCalls = @($global:MockCalls | Where-Object { $_ -match " doctor --backend direct --radio --phy " })
Assert-True ($phyCalls.Count -eq 1) "control-port failure queried extra PHYs"
Assert-True (
    $phyCalls[0] -match " doctor --backend direct --radio --phy phy1 --json$"
) "control-port check was not scoped to the selected PHY"

# Unknown/missing custom EtherType, busy devices, malformed results and a
# different PHY must all stop before any privileged relay or key-file check.
foreach ($scenario in @("missing-ethertype", "busy-radio", "unknown-capability", "wrong-phy", "bad-json")) {
    Reset-Mock $scenario
    Invoke-ExpectFailure { & $launcher -Keys ".\mock-prod.keys" -Phy "phy1" } "prerequisites|parse"
    Assert-True (@($global:MockCalls | Where-Object { $_ -match "zatrade\.py relay-serve " }).Count -eq 0) "relay started after failed radio diagnostic"
    Assert-True (@($global:MockCalls | Where-Object { $_ -match " --exec test -f " }).Count -eq 0) "key-file check ran before radio prerequisite gate"
}
Write-Output "start_za_relay_wsl.ps1 mock tests passed"
