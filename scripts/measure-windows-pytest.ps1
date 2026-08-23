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

$modeOrder = @($Modes.Split(",") | ForEach-Object { $_.Trim().ToLowerInvariant() })
if ($modeOrder.Count -ne 2 -or
    $modeOrder[0] -eq $modeOrder[1] -or
    @($modeOrder | Where-Object { $_ -notin @("serial", "parallel") }).Count -ne 0) {
    throw "Modes must contain serial and parallel exactly once."
}

$repoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$artifactsRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot "artifacts"))
$outputPath = if ([IO.Path]::IsPathRooted($Output)) {
    [IO.Path]::GetFullPath($Output)
} else {
    [IO.Path]::GetFullPath((Join-Path $repoRoot $Output))
}
if ((Split-Path -Parent $outputPath) -ne $artifactsRoot) {
    throw "Output must be a direct child of the repository artifacts directory."
}
if ([IO.Path]::GetFileName($outputPath) -notmatch "^[A-Za-z0-9._-]+\.json$") {
    throw "Output must have a safe JSON filename."
}
if (Test-Path -LiteralPath $outputPath) {
    throw "Refusing to overwrite an existing timing artifact: $Output"
}
if ((Get-Item -LiteralPath $repoRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Repository root cannot be a reparse point."
}
if (Test-Path -LiteralPath $artifactsRoot) {
    if ((Get-Item -LiteralPath $artifactsRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Artifacts directory cannot be a reparse point."
    }
} else {
    [void](New-Item -ItemType Directory -Path $artifactsRoot)
}

function Invoke-TextCommand {
    param(
        [Parameter(Mandatory)] [string]$FilePath,
        [Parameter(Mandatory)] [string[]]$Arguments
    )

    $text = & $FilePath @Arguments 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed during measurement preflight."
    }
    return $text.Trim()
}

$outputRelative = [IO.Path]::GetRelativePath($repoRoot, $outputPath).Replace("\", "/")
& git -C $repoRoot ls-files --error-unmatch -- $outputRelative *> $null
if ($LASTEXITCODE -eq 0) {
    throw "Timing output must be an absent, untracked artifact before measurement."
}
$trackedStatus = Invoke-TextCommand -FilePath "git" -Arguments @(
    "-C", $repoRoot, "status", "--porcelain=v1", "--untracked-files=no"
)
if (-not [string]::IsNullOrEmpty($trackedStatus)) {
    throw "Tracked worktree changes must be committed before measurement."
}

$commit = Invoke-TextCommand -FilePath "git" -Arguments @("-C", $repoRoot, "rev-parse", "HEAD")
if (-not [string]::IsNullOrEmpty($ExpectedCommit) -and $ExpectedCommit -ne $commit) {
    throw "HEAD does not match ANVIL_EXPECTED_COMMIT."
}

$cacheRelative = ".anvil-build/windows-pytest-uv-cache"
$cachePath = Join-Path $repoRoot $cacheRelative
[void](New-Item -ItemType Directory -Force -Path $cachePath)
$runId = "{0}-{1}" -f [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssfffZ"), [guid]::NewGuid().ToString("N").Substring(0, 10)
$logRelativeRoot = "artifacts/windows-pytest-timing-logs/$runId"
$logRoot = Join-Path $repoRoot $logRelativeRoot
[void](New-Item -ItemType Directory -Path $logRoot)
& git -C $repoRoot check-ignore -q -- "$logRelativeRoot/probe.log"
if ($LASTEXITCODE -ne 0) {
    throw "Raw timing logs must be ignored by Git before measurement starts."
}

function Get-Sha256Text {
    param([Parameter(Mandatory)] [string]$Text)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    try {
        return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
    } finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Get-DefenderExclusionDigest {
    $preference = Get-MpPreference
    $groups = [ordered]@{
        paths = @($preference.ExclusionPath)
        processes = @($preference.ExclusionProcess)
        extensions = @($preference.ExclusionExtension)
    }
    $serialized = $groups | ConvertTo-Json -Compress -Depth 4
    $unobservable = @($groups.Values | ForEach-Object { $_ } | Where-Object {
        "$_" -like "N/A:*administrator*"
    }).Count -gt 0
    return [ordered]@{
        observable = -not $unobservable
        sha256 = Get-Sha256Text -Text $serialized
        path_count = @($groups.paths).Count
        process_count = @($groups.processes).Count
        extension_count = @($groups.extensions).Count
    }
}

function Get-ControlSnapshot {
    $status = Get-MpComputerStatus
    $power = (powercfg /getactivescheme 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read the active Windows power scheme."
    }
    return [ordered]@{
        power_scheme = $power
        defender = [ordered]@{
            antivirus_enabled = [bool]$status.AntivirusEnabled
            realtime_protection_enabled = [bool]$status.RealTimeProtectionEnabled
            behavior_monitor_enabled = [bool]$status.BehaviorMonitorEnabled
            ioav_protection_enabled = [bool]$status.IoavProtectionEnabled
            tamper_protected = [bool]$status.IsTamperProtected
            exclusions = Get-DefenderExclusionDigest
        }
    }
}

function Get-ControlFingerprint {
    param([Parameter(Mandatory)] $Snapshot)
    return Get-Sha256Text -Text ($Snapshot | ConvertTo-Json -Compress -Depth 8)
}

function Get-Median {
    param([Parameter(Mandatory)] [double[]]$Values)
    $sorted = @($Values | Sort-Object)
    $middle = [Math]::Floor($sorted.Count / 2)
    if ($sorted.Count % 2 -eq 1) {
        return [double]$sorted[$middle]
    }
    return ([double]$sorted[$middle - 1] + [double]$sorted[$middle]) / 2
}

function Invoke-MeasuredProcess {
    param(
        [Parameter(Mandatory)] [string]$Mode,
        [Parameter(Mandatory)] [string]$Phase,
        [Parameter(Mandatory)] [int]$Sequence,
        [AllowNull()] [Nullable[int]]$Pair,
        [AllowNull()] [Nullable[int]]$OrderInPair
    )

    $controlBefore = Get-ControlSnapshot
    $controlBeforeFingerprint = Get-ControlFingerprint -Snapshot $controlBefore
    $logName = "{0:d2}-{1}-{2}.log" -f $Sequence, $Phase, $Mode
    $logPath = Join-Path $logRoot $logName
    $logRelative = "$logRelativeRoot/$logName"
    $process = $null
    $stdoutTask = $null
    $stderrTask = $null
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $timedOut = $false
    $exitCode = $null
    $processError = $null
    $stdout = ""
    $stderr = ""

    if ($Probe) {
        $filePath = (Get-Command pwsh).Source
        $arguments = @(
            "-NoProfile", "-Command",
            "Write-Output '1 passed in 0.01s'; exit 0"
        )
        $publicCommand = @("pwsh", "-NoProfile", "-Command", "<fixed-probe>")
    } else {
        $filePath = (Get-Command uv).Source
        $arguments = @("run", "--project", "bin", "pytest", "tests", "-q")
        if ($Mode -eq "serial") {
            $arguments += @("-n", "0")
        } else {
            $arguments += @("-n", "$Workers")
        }
        $publicCommand = @("uv") + $arguments
    }

    try {
        $startInfo = [Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $filePath
        $startInfo.WorkingDirectory = $repoRoot
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        foreach ($argument in $arguments) {
            $startInfo.ArgumentList.Add($argument)
        }
        $startInfo.Environment["UV_CACHE_DIR"] = $cachePath
        [void]$startInfo.Environment.Remove("PYTEST_ADDOPTS")
        $startInfo.Environment["ANVIL_TIMING_RUN_ID"] = $runId

        $process = [Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            throw "ProcessStartFailed"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $timedOut = $true
            $process.Kill($true)
            if (-not $process.WaitForExit(15000)) {
                throw "ProcessTreeTerminationFailed"
            }
        }
        if (-not $stdoutTask.Wait(15000) -or -not $stderrTask.Wait(15000)) {
            throw "ProcessOutputDrainFailed"
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        $exitCode = $process.ExitCode
    } catch {
        $processError = $_.Exception.GetType().Name
        if ($null -ne $process) {
            try {
                if (-not $process.HasExited) {
                    $process.Kill($true)
                    [void]$process.WaitForExit(15000)
                }
            } catch {
                $processError = "ProcessContainmentFailed"
            }
        }
    } finally {
        $stopwatch.Stop()
        if ($null -ne $process) {
            $process.Dispose()
        }
    }

    $rawLog = @(
        "command: $($publicCommand -join ' ')",
        "exit_code: $exitCode",
        "timed_out: $timedOut",
        "--- stdout ---",
        $stdout,
        "--- stderr ---",
        $stderr
    ) -join [Environment]::NewLine
    [IO.File]::WriteAllText($logPath, $rawLog, [Text.UTF8Encoding]::new($false))
    $passedMatch = [regex]::Match("$stdout`n$stderr", "(?m)(\d+) passed(?:[,\s]|$)")
    $passed = if ($passedMatch.Success) { [int]$passedMatch.Groups[1].Value } else { 0 }
    $controlAfter = Get-ControlSnapshot
    $controlAfterFingerprint = Get-ControlFingerprint -Snapshot $controlAfter
    $controlsMatch = (
        $controlBeforeFingerprint -eq $script:baselineControlFingerprint -and
        $controlAfterFingerprint -eq $script:baselineControlFingerprint
    )
    $exclusionsObservable = [bool]$controlBefore.defender.exclusions.observable
    $valid = (
        -not $timedOut -and
        $null -eq $processError -and
        $exitCode -eq 0 -and
        $passed -gt 0 -and
        $controlsMatch -and
        $exclusionsObservable
    )

    return [ordered]@{
        sequence = $Sequence
        phase = $Phase
        pair = $Pair
        order_in_pair = $OrderInPair
        mode = $Mode
        worker_count = if ($Mode -eq "serial") { 0 } else { $Workers }
        command = $publicCommand
        elapsed_seconds = if ($Phase -eq "measured") { [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3) } else { $null }
        exit_code = $exitCode
        timed_out = $timedOut
        tests_passed = $passed
        process_error = $processError
        controls_match = $controlsMatch
        exclusions_observable = $exclusionsObservable
        valid = $valid
        raw_log = [ordered]@{
            relative_path = $logRelative
            sha256 = (Get-FileHash -LiteralPath $logPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
}

$baselineControls = Get-ControlSnapshot
$script:baselineControlFingerprint = Get-ControlFingerprint -Snapshot $baselineControls
$computer = Get-CimInstance Win32_ComputerSystem
$processor = Get-CimInstance Win32_Processor | Select-Object -First 1
$operatingSystem = Get-CimInstance Win32_OperatingSystem
$versionsJson = Invoke-TextCommand -FilePath "uv" -Arguments @(
    "run", "--project", (Join-Path $repoRoot "bin"), "python", "-c",
    "import importlib.metadata as m,json,platform; print(json.dumps({'python':platform.python_version(),'pytest':m.version('pytest'),'pytest_xdist':m.version('pytest-xdist'),'anvil_state':m.version('anvil-state')}))"
)
$versions = $versionsJson | ConvertFrom-Json
$uvVersion = Invoke-TextCommand -FilePath "uv" -Arguments @("--version")

$runs = [Collections.Generic.List[object]]::new()
$sequence = 0
foreach ($mode in $modeOrder) {
    for ($warmup = 1; $warmup -le $Warmups; $warmup++) {
        $sequence++
        [void]$runs.Add((Invoke-MeasuredProcess -Mode $mode -Phase "warmup" -Sequence $sequence -Pair $null -OrderInPair $null))
    }
}
for ($pair = 1; $pair -le $Samples; $pair++) {
    $pairOrder = if ($pair % 2 -eq 1) { $modeOrder } else { @($modeOrder[1], $modeOrder[0]) }
    for ($position = 0; $position -lt $pairOrder.Count; $position++) {
        $sequence++
        [void]$runs.Add((Invoke-MeasuredProcess -Mode $pairOrder[$position] -Phase "measured" -Sequence $sequence -Pair $pair -OrderInPair ($position + 1)))
    }
}

foreach ($run in $runs) {
    if ($run -isnot [Collections.IDictionary] -or -not $run.Contains("phase")) {
        throw "Internal harness error: a scheduled run did not return one structured record."
    }
}
$measured = @($runs.ToArray() | Where-Object { $_.phase -eq "measured" })
$allMeasuredValid = @($measured | Where-Object { -not $_.valid }).Count -eq 0
$serialMedian = $null
$parallelMedian = $null
$speedupPercent = $null
$speedupRatio = $null
$resultStatus = "insufficient"
$insufficientReasons = [Collections.Generic.List[string]]::new()
if (-not [bool]$baselineControls.defender.exclusions.observable) {
    [void]$insufficientReasons.Add("defender_exclusions_unobservable_without_administrator")
}
if (@($measured | Where-Object { $_.timed_out }).Count -gt 0) {
    [void]$insufficientReasons.Add("one_or_more_timeouts")
}
if (@($measured | Where-Object { $_.exit_code -ne 0 }).Count -gt 0) {
    [void]$insufficientReasons.Add("one_or_more_nonzero_exit_codes")
}
if (@($measured | Where-Object { -not $_.controls_match }).Count -gt 0) {
    [void]$insufficientReasons.Add("control_settings_changed")
}
if (@($measured | Where-Object { $_.tests_passed -le 0 }).Count -gt 0) {
    [void]$insufficientReasons.Add("executed_test_count_unverified")
}
if ($allMeasuredValid) {
    $serialValues = [double[]]@($measured | Where-Object { $_.mode -eq "serial" } | ForEach-Object { $_.elapsed_seconds })
    $parallelValues = [double[]]@($measured | Where-Object { $_.mode -eq "parallel" } | ForEach-Object { $_.elapsed_seconds })
    if ($serialValues.Count -eq $Samples -and $parallelValues.Count -eq $Samples) {
        $serialMedian = [Math]::Round((Get-Median -Values $serialValues), 3)
        $parallelMedian = [Math]::Round((Get-Median -Values $parallelValues), 3)
        $speedupRatio = [Math]::Round($serialMedian / $parallelMedian, 4)
        $speedupPercent = [Math]::Round((1 - ($parallelMedian / $serialMedian)) * 100, 2)
        $resultStatus = "valid"
    } else {
        [void]$insufficientReasons.Add("sample_count_mismatch")
    }
}

$artifact = [ordered]@{
    schema_version = 1
    generated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    commit = $commit
    host = [ordered]@{
        label = $HostLabel
        native_windows = $true
        os = [ordered]@{
            caption = $operatingSystem.Caption
            version = $operatingSystem.Version
            build_number = $operatingSystem.BuildNumber
        }
        cpu = [ordered]@{
            name = $processor.Name.Trim()
            physical_cores = [int]$processor.NumberOfCores
            logical_processors = [int]$processor.NumberOfLogicalProcessors
        }
        ram_bytes = [int64]$computer.TotalPhysicalMemory
    }
    versions = [ordered]@{
        python = $versions.python
        uv = $uvVersion
        pytest = $versions.pytest
        pytest_xdist = $versions.pytest_xdist
        anvil_state = $versions.anvil_state
    }
    protocol = [ordered]@{
        warmups_per_mode = $Warmups
        measured_pairs = $Samples
        modes = $modeOrder
        pair_order = "odd pairs use declared order; even pairs reverse it"
        timeout_seconds = $TimeoutSeconds
        parallel_workers = $Workers
        uv_cache = $cacheRelative
        inherited_environment = "one harness process; PYTEST_ADDOPTS rejected; fixed per-run overrides"
        raw_logs = "$logRelativeRoot/"
        process_containment = "System.Diagnostics.Process.Kill(entireProcessTree=true) with a 15-second exit deadline"
        probe = [bool]$Probe
    }
    controls = [ordered]@{
        fingerprint_sha256 = $script:baselineControlFingerprint
        power_scheme = $baselineControls.power_scheme
        defender = $baselineControls.defender
    }
    result = [ordered]@{
        status = $resultStatus
        insufficient_reasons = @($insufficientReasons)
        serial_median_seconds = $serialMedian
        parallel_median_seconds = $parallelMedian
        parallel_speedup_ratio = $speedupRatio
        parallel_speedup_percent = $speedupPercent
        threshold_percent_for_issue_118 = 40
        threshold_met = if ($resultStatus -eq "valid") { $speedupPercent -ge 40 } else { $null }
        all_scheduled_runs_recorded = $runs.Count -eq (($Warmups * 2) + ($Samples * 2))
    }
    runs = @($runs.ToArray())
}

$json = $artifact | ConvertTo-Json -Depth 12
[IO.File]::WriteAllText($outputPath, "$json`n", [Text.UTF8Encoding]::new($false))
Write-Output "Timing artifact: $outputRelative"
Write-Output "Result: $resultStatus"
exit 0
