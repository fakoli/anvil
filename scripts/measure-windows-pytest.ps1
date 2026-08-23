[CmdletBinding()]
param(
    [ValidateRange(0, 10)]
    [int]$Warmups = 1,

    [ValidateRange(1, 20)]
    [int]$Samples = 5,

    [string]$Modes = "serial,parallel",

    [ValidateRange(1, 256)]
    [int]$Workers = [Math]::Max(1, [Math]::Min(16, [Environment]::ProcessorCount)),

    [ValidateRange(1, 86400)]
    [int]$TimeoutSeconds = 1800,

    [string]$Output = "artifacts/windows-pytest-timing.json",

    [string]$ExpectedCommit = $env:ANVIL_EXPECTED_COMMIT,

    [string]$HostLabel = $env:ANVIL_TIMING_HOST_LABEL,

    [switch]$Probe
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "This measurement protocol requires native Windows."
}
if ([string]::IsNullOrWhiteSpace($HostLabel) -or $HostLabel -notmatch "^[A-Za-z0-9._-]{1,64}$") {
    throw "Set ANVIL_TIMING_HOST_LABEL to a sanitized host label (letters, digits, dot, underscore, or hyphen)."
}
if (-not [string]::IsNullOrEmpty($env:PYTEST_ADDOPTS)) {
    throw "PYTEST_ADDOPTS must be unset so ambient options cannot change the measured contract."
}

$repoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$arguments = @(
    "run", "--locked", "--exact", "--project", (Join-Path $repoRoot "bin"),
    "python", (Join-Path $PSScriptRoot "windows_pytest_timing.py"),
    "--repo", $repoRoot,
    "--warmups", "$Warmups",
    "--samples", "$Samples",
    "--modes", $Modes,
    "--workers", "$Workers",
    "--timeout-seconds", "$TimeoutSeconds",
    "--output", $Output,
    "--host-label", $HostLabel
)
if (-not [string]::IsNullOrWhiteSpace($ExpectedCommit)) {
    $arguments += @("--expected-commit", $ExpectedCommit)
}
if ($Probe) {
    $arguments += "--probe"
}

& uv @arguments
exit $LASTEXITCODE
