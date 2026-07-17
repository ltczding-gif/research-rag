param(
    [string]$Workspace = $(if ($env:LOCALRAG_NOTES_DIR) { $env:LOCALRAG_NOTES_DIR } else { "$HOME\research-note" }),
    [Parameter(Mandatory = $true)]
    [string]$Session,
    [int]$RefreshSeconds = 20,
    [int]$MaxChecks = 0,
    [switch]$UntilBatchDone,
    [switch]$UntilPipelineDone,
    [switch]$AsJson
)

# Override these via env vars on a per-machine basis. Defaults rely on PATH.
$PYTHON = $(if ($env:LOCALRAG_MAIN_PYTHON) { $env:LOCALRAG_MAIN_PYTHON } else { "python3" })
$PYTHON_RAG = $(if ($env:LOCALRAG_RAG_PYTHON) { $env:LOCALRAG_RAG_PYTHON } else { "python3" })

$ErrorActionPreference = "Stop"

function Resolve-WorkspacePath {
    param([string]$PathText)
    return (Resolve-Path -LiteralPath $PathText).Path
}

function Get-MatchingFiles {
    param(
        [string]$DirectoryPath,
        [string]$Prefix,
        [string]$Pattern = "*.json"
    )

    if (-not (Test-Path -LiteralPath $DirectoryPath)) {
        return @()
    }

    return @(Get-ChildItem -LiteralPath $DirectoryPath -Filter $Pattern -File | Where-Object {
        $_.Name -like "*$Prefix*"
    } | Sort-Object LastWriteTime)
}

function Get-PipelineProcess {
    param([string]$SessionName)

    return Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "pwsh.exe" `
            -and $_.CommandLine -match [regex]::Escape("run_tagging_pipeline.ps1") `
            -and $_.CommandLine -match [regex]::Escape($SessionName)
    } | Sort-Object CreationDate -Descending | Select-Object -First 1
}

function Get-LatestGateReport {
    param(
        [string]$WorkspacePath,
        [string]$SessionName
    )

    $gateDir = Join-Path $WorkspacePath "progress\gate_reports"
    $reports = Get-MatchingFiles -DirectoryPath $gateDir -Prefix $SessionName -Pattern "*.json" | Where-Object {
        $_.Name -notlike "*.validator.json"
    }

    if (-not $reports -or $reports.Count -eq 0) {
        return $null
    }

    return $reports[-1]
}

function Get-LatestPipelineReport {
    param(
        [string]$WorkspacePath,
        [string]$SessionName
    )

    $pipeDir = Join-Path $WorkspacePath "progress\pipeline_reports"
    $reports = Get-MatchingFiles -DirectoryPath $pipeDir -Prefix $SessionName -Pattern "*.json"
    if (-not $reports -or $reports.Count -eq 0) {
        return $null
    }

    return $reports[-1]
}

function Get-LatestKimiBatchSession {
    param([string]$SessionName)

    $root = Join-Path $env:LOCALAPPDATA "kimi-supervision\sessions"
    if (-not (Test-Path -LiteralPath $root)) {
        return $null
    }

    $dirs = @(Get-ChildItem -LiteralPath $root -Directory | Where-Object {
        $_.Name -like "$SessionName*"
    } | Sort-Object LastWriteTime)

    if (-not $dirs -or $dirs.Count -eq 0) {
        return $null
    }

    return $dirs[-1]
}

function Get-KimiBatchStatus {
    param([string]$BatchSessionName)

    if (-not $BatchSessionName) {
        return $null
    }

    # The kimi-supervision skill is a separate, optional helper. Set
    # $env:KIMI_SUPERVISION_VIEW_SCRIPT to its kimi-view-session.ps1 path
    # to enable this status enrichment; without it, batch status falls back
    # to gate-report-only.
    $viewScript = $env:KIMI_SUPERVISION_VIEW_SCRIPT
    if (-not $viewScript -or -not (Test-Path -LiteralPath $viewScript)) {
        return $null
    }

    try {
        $raw = & $viewScript -Session $BatchSessionName -View status
        if ($LASTEXITCODE -ne 0 -or -not $raw) {
            return $null
        }
        return ($raw | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Get-BatchIndexFromName {
    param([string]$Name)

    if (-not $Name) {
        return $null
    }
    $match = [regex]::Match($Name, "-b(\d{3})")
    if ($match.Success) {
        return [int]$match.Groups[1].Value
    }
    return $null
}

function New-Snapshot {
    param(
        [string]$WorkspacePath,
        [string]$SessionName
    )

    $process = Get-PipelineProcess -SessionName $SessionName
    $latestGate = Get-LatestGateReport -WorkspacePath $WorkspacePath -SessionName $SessionName
    $latestPipeline = Get-LatestPipelineReport -WorkspacePath $WorkspacePath -SessionName $SessionName
    $latestKimiDir = Get-LatestKimiBatchSession -SessionName $SessionName
    $latestKimiStatus = $null
    if ($latestKimiDir) {
        $latestKimiStatus = Get-KimiBatchStatus -BatchSessionName $latestKimiDir.Name
    }

    $gateData = $null
    if ($latestGate) {
        try {
            $gateData = Get-Content -LiteralPath $latestGate.FullName -Raw | ConvertFrom-Json
        }
        catch {
            $gateData = $null
        }
    }

    $pipelineData = $null
    if ($latestPipeline) {
        try {
            $pipelineData = Get-Content -LiteralPath $latestPipeline.FullName -Raw | ConvertFrom-Json
        }
        catch {
            $pipelineData = $null
        }
    }

    $gateDir = Join-Path $WorkspacePath "progress\gate_reports"
    $gateCount = 0
    if (Test-Path -LiteralPath $gateDir) {
        $gateCount = @(Get-MatchingFiles -DirectoryPath $gateDir -Prefix $SessionName -Pattern "*.json" | Where-Object {
            $_.Name -notlike "*.validator.json"
        }).Count
    }

    return [pscustomobject]@{
        timestamp = (Get-Date).ToString("s")
        session = $SessionName
        pipeline_running = [bool]$process
        pipeline_process_id = if ($process) { $process.ProcessId } else { $null }
        latest_gate_report = if ($latestGate) { $latestGate.FullName } else { $null }
        latest_gate_status = if ($gateData) { $gateData.status } else { $null }
        latest_gate_batch = if ($latestGate) { Get-BatchIndexFromName -Name $latestGate.BaseName } else { $null }
        latest_pipeline_report = if ($latestPipeline) { $latestPipeline.FullName } else { $null }
        latest_pipeline_status = if ($pipelineData) { $pipelineData.status } else { $null }
        latest_pipeline_stop_reason = if ($pipelineData) { $pipelineData.stop_reason } else { $null }
        latest_pipeline_batch_count = if ($pipelineData) { $pipelineData.batch_count } else { $null }
        remaining_queue_count = if ($pipelineData) { $pipelineData.remaining_queue_count } else { $null }
        gate_report_count = $gateCount
        latest_kimi_batch_session = if ($latestKimiDir) { $latestKimiDir.FullName } else { $null }
        latest_kimi_batch_name = if ($latestKimiDir) { $latestKimiDir.Name } else { $null }
        latest_kimi_result_classification = if ($latestKimiStatus) { $latestKimiStatus.result_classification } else { $null }
        latest_kimi_recovery_status = if ($latestKimiStatus) { $latestKimiStatus.recovery_status } else { $null }
        latest_kimi_updated_at = if ($latestKimiStatus) { $latestKimiStatus.updated_at } else { $null }
    }
}

function Write-Snapshot {
    param(
        [object]$Snapshot,
        [switch]$JsonMode
    )

    if ($JsonMode) {
        $Snapshot | ConvertTo-Json -Depth 6
        return
    }

    Write-Host ("[{0}] session={1}" -f $Snapshot.timestamp, $Snapshot.session)
    Write-Host ("  pipeline_running: {0}" -f $Snapshot.pipeline_running)
    if ($Snapshot.pipeline_process_id) {
        Write-Host ("  pipeline_process_id: {0}" -f $Snapshot.pipeline_process_id)
    }
    Write-Host ("  gate_report_count: {0}" -f $Snapshot.gate_report_count)
    Write-Host ("  latest_gate_status: {0}" -f $(if ($Snapshot.latest_gate_status) { $Snapshot.latest_gate_status } else { "<none>" }))
    Write-Host ("  latest_gate_batch: {0}" -f $(if ($null -ne $Snapshot.latest_gate_batch) { $Snapshot.latest_gate_batch } else { "<none>" }))
    Write-Host ("  latest_pipeline_status: {0}" -f $(if ($Snapshot.latest_pipeline_status) { $Snapshot.latest_pipeline_status } else { "<none>" }))
    Write-Host ("  latest_pipeline_stop_reason: {0}" -f $(if ($Snapshot.latest_pipeline_stop_reason) { $Snapshot.latest_pipeline_stop_reason } else { "<none>" }))
    Write-Host ("  remaining_queue_count: {0}" -f $(if ($null -ne $Snapshot.remaining_queue_count) { $Snapshot.remaining_queue_count } else { "<unknown>" }))
    Write-Host ("  latest_kimi_batch_name: {0}" -f $(if ($Snapshot.latest_kimi_batch_name) { $Snapshot.latest_kimi_batch_name } else { "<none>" }))
    Write-Host ("  latest_kimi_result_classification: {0}" -f $(if ($Snapshot.latest_kimi_result_classification) { $Snapshot.latest_kimi_result_classification } else { "<none>" }))
    Write-Host ("  latest_kimi_recovery_status: {0}" -f $(if ($Snapshot.latest_kimi_recovery_status) { $Snapshot.latest_kimi_recovery_status } else { "<none>" }))
    if ($Snapshot.latest_gate_report) {
        Write-Host ("  latest_gate_report: {0}" -f $Snapshot.latest_gate_report)
    }
    if ($Snapshot.latest_pipeline_report) {
        Write-Host ("  latest_pipeline_report: {0}" -f $Snapshot.latest_pipeline_report)
    }
}

if (-not $UntilBatchDone -and -not $UntilPipelineDone) {
    $UntilBatchDone = $true
}

if ($RefreshSeconds -le 0) {
    throw "RefreshSeconds must be greater than 0."
}

if ($UntilBatchDone -and $UntilPipelineDone) {
    throw "Use either -UntilBatchDone or -UntilPipelineDone, not both."
}

$workspacePath = Resolve-WorkspacePath -PathText $Workspace
$baseline = New-Snapshot -WorkspacePath $workspacePath -SessionName $Session
$baselineGateCount = $baseline.gate_report_count

$checkIndex = 0
while ($true) {
    $checkIndex += 1
    $snapshot = New-Snapshot -WorkspacePath $workspacePath -SessionName $Session
    Write-Snapshot -Snapshot $snapshot -JsonMode:$AsJson

    $batchDone = $snapshot.gate_report_count -gt $baselineGateCount
    $pipelineDone = [bool]$snapshot.latest_pipeline_report -or (-not $snapshot.pipeline_running -and $snapshot.gate_report_count -ge $baselineGateCount)

    if ($UntilBatchDone -and ($batchDone -or $pipelineDone)) {
        if ($snapshot.latest_gate_status -eq "failed" -or $snapshot.latest_pipeline_status -eq "failed") {
            exit 1
        }
        exit 0
    }

    if ($UntilPipelineDone -and $pipelineDone) {
        if ($snapshot.latest_pipeline_status -eq "failed") {
            exit 1
        }
        exit 0
    }

    if ($MaxChecks -gt 0 -and $checkIndex -ge $MaxChecks) {
        exit 2
    }

    Start-Sleep -Seconds $RefreshSeconds
}
