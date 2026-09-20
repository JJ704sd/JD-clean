[CmdletBinding()]
param(
    [string]$InputDirectory = (Join-Path $env:USERPROFILE 'Downloads'),
    [string]$RunDateTag = (Get-Date).ToString('yyyy-MM-dd'),
    [switch]$PreviewOnly,
    [switch]$AllowExternalModelData,
    [int]$MaxTasks = 0
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RoleMarkers = @(
    (@([char]0x4E1A, [char]0x52A1, [char]0x7CFB, [char]0x7EDF, [char]0x8FD0, [char]0x7EF4, [char]0x5DE5, [char]0x7A0B, [char]0x5E08) -join ''),
    (@([char]0x4E1A, [char]0x52A1, [char]0x7CFB, [char]0x7EDF, [char]0x8FD0, [char]0x7EF4) -join '')
)
$ExcludedMarker = @([char]0x5360, [char]0x4F20, [char]0x5B87) -join ''

try {
    $RunDate = [datetime]::ParseExact($RunDateTag, 'yyyy-MM-dd', [System.Globalization.CultureInfo]::InvariantCulture)
}
catch {
    throw "RunDateTag must use yyyy-MM-dd: $RunDateTag"
}

if ((Get-Date).Date -ne $RunDate.Date) {
    Write-Output "run_date_expired=$RunDateTag; no files were processed."
    return
}

$BatchTag = "today-business-system-operations-screening-$RunDateTag"
$Database = Join-Path $ProjectRoot "var\screening-today-business-system-operations-$RunDateTag.sqlite3"
$OutputDirectory = Join-Path $ProjectRoot "outputs\$BatchTag"
$ExportDirectory = Join-Path $ProjectRoot "exports\$BatchTag"

if (-not (Test-Path -LiteralPath $InputDirectory -PathType Container)) {
    throw "Resume input directory does not exist: $InputDirectory"
}

$CandidatePdfs = @(
    Get-ChildItem -LiteralPath $InputDirectory -Filter '*.pdf' -File -Recurse |
        Where-Object {
            $roleMatch = $false
            foreach ($marker in $RoleMarkers) {
                if ($_.Name -like "*$marker*") {
                    $roleMatch = $true
                    break
                }
            }
            $_.LastWriteTime.Date -eq $RunDate.Date -and
                $roleMatch -and
                $_.Name -notlike "*$ExcludedMarker*"
        } |
        Sort-Object FullName
)

if ($PreviewOnly) {
    Write-Output "date=$RunDateTag"
    Write-Output 'role=business-system-operations-engineer'
    Write-Output "same_day_business_system_operations_pdf_count=$($CandidatePdfs.Count)"
    Write-Output "excluded_filename_marker=$ExcludedMarker"
    Write-Output "database=$Database"
    Write-Output "output=$OutputDirectory"
    Write-Output "export=$ExportDirectory"
    Write-Output 'preview_only=true; no files were enqueued and no model was called.'
    return
}

if ($CandidatePdfs.Count -eq 0 -and -not (Test-Path -LiteralPath $Database -PathType Leaf)) {
    Write-Output "No same-day business-system operations PDF resumes found for $RunDateTag."
    return
}

if (-not $AllowExternalModelData) {
    throw 'External model submission is paused. Pass -AllowExternalModelData only after explicit authorization to send de-identified resume text to MiniMax.'
}

$AllowedEnvNames = @('MINIMAX_API_KEY', 'MINIMAX_API_ENDPOINT', 'MINIMAX_API_BASE')
$PreviousProcessEnvironment = [ordered]@{}
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python environment was not found: $Python"
}
foreach ($Name in $AllowedEnvNames) {
    $PreviousProcessEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
}

try {
    $DotEnvPath = Join-Path $ProjectRoot '.env'
    if (Test-Path -LiteralPath $DotEnvPath -PathType Leaf) {
        foreach ($Line in [System.IO.File]::ReadLines($DotEnvPath)) {
            if ($Line -notmatch '^\s*(?:export\s+)?(?<Name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<Value>.*)$') {
                continue
            }
            $Name = $Matches['Name']
            if ($Name -notin $AllowedEnvNames -or
                -not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Name, 'Process'))) {
                continue
            }
            $Value = $Matches['Value'].Trim()
            if ($Value.Length -ge 2 -and $Value[0] -in @([char]34, [char]39)) {
                $Quote = [string]$Value[0]
                $ClosingQuote = $Value.LastIndexOf($Quote)
                if ($ClosingQuote -gt 0) {
                    $Value = $Value.Substring(1, $ClosingQuote - 1)
                }
            }
            else {
                $Value = ($Value -replace '\s+#.*$', '').Trim()
            }
            [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:MINIMAX_API_KEY)) {
        throw 'MINIMAX_API_KEY is not configured in the process environment or project .env; no resumes were enqueued.'
    }

    & $Python -X utf8 -B -m resume_screening --database $Database retry-failed
    if ($LASTEXITCODE -ne 0) {
        throw "Retry-failed command failed with exit code $LASTEXITCODE."
    }

    $EnqueueArguments = @(
        '-X', 'utf8', '-B', '-m', 'resume_screening',
        '--database', $Database,
        'enqueue',
        '--role', 'business-system-operations-engineer',
        '--today'
    ) + @($CandidatePdfs | ForEach-Object { $_.FullName })
    & $Python @EnqueueArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Resume enqueue failed with exit code $LASTEXITCODE."
    }

    $WorkerArguments = @(
        '-X', 'utf8', '-B', '-m', 'resume_screening',
        '--database', $Database,
        '--output', $OutputDirectory,
        'worker', '--once'
    )
    if ($MaxTasks -gt 0) {
        $WorkerArguments += @('--max-tasks', [string]$MaxTasks)
    }
    & $Python @WorkerArguments
    $WorkerCode = $LASTEXITCODE

    & $Python -X utf8 -B -m resume_screening --database $Database export --directory $ExportDirectory
    $ExportCode = $LASTEXITCODE
    if ($ExportCode -ne 0) {
        throw "Resume export failed with exit code $ExportCode."
    }

    $ScreeningFiles = @(Get-ChildItem -LiteralPath $OutputDirectory -Filter 'screening.json' -File -Recurse -ErrorAction SilentlyContinue)
    foreach ($ScreeningFile in $ScreeningFiles) {
        & $Python -X utf8 -B -m resume_screening validate $ScreeningFile.FullName
        if ($LASTEXITCODE -ne 0) {
            throw "Invalid screening result: $($ScreeningFile.FullName)"
        }
    }

    $ReviewCsv = Join-Path $ExportDirectory 'review_queue.csv'
    $NoScoreCsv = Join-Path $ExportDirectory 'review_queue_no_scores.csv'
    $ReviewMarkdown = Join-Path $ExportDirectory 'review-list-no-scores.md'
    $LegacyReviewMarkdown = Join-Path $ExportDirectory 'review.md'
    $ReviewRows = @()
    if (Test-Path -LiteralPath $ReviewCsv -PathType Leaf) {
        $ReviewRows = @(Import-Csv -LiteralPath $ReviewCsv)
        $ReviewRows |
            Select-Object task_id, candidate_id, candidate_name, role, recommendation, required_review, error_code |
            Export-Csv -LiteralPath $NoScoreCsv -NoTypeInformation -Encoding utf8
    }
    else {
        @() |
            Select-Object task_id, candidate_id, candidate_name, role, recommendation, required_review, error_code |
            Export-Csv -LiteralPath $NoScoreCsv -NoTypeInformation -Encoding utf8
    }
    $MarkdownLines = @(
        '# Business-system operations resume review queue',
        '',
        "Run date: $RunDateTag (Asia/Shanghai)",
        'This queue contains qualitative evidence and review state only; it omits scores and does not replace the recruiter decision.',
        ''
    )
    foreach ($Row in $ReviewRows) {
        $Name = if ([string]::IsNullOrWhiteSpace($Row.candidate_name)) { 'unidentified' } else { $Row.candidate_name }
        $Recommendation = if ([string]::IsNullOrWhiteSpace($Row.recommendation)) { 'pending_human_action' } else { $Row.recommendation }
        $RequiredReview = if ([string]::IsNullOrWhiteSpace($Row.required_review)) { 'human_action' } else { $Row.required_review }
        $ErrorText = if ([string]::IsNullOrWhiteSpace($Row.error_code)) { '' } else { "; process_marker=$($Row.error_code)" }
        $MarkdownLines += "- ${Name}: qualitative_result=$Recommendation; review=$RequiredReview$ErrorText"
    }
    $ReviewMarkdownText = (($MarkdownLines -join "`r`n") + "`r`n")
    [System.IO.File]::WriteAllText($ReviewMarkdown, $ReviewMarkdownText, [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText($LegacyReviewMarkdown, $ReviewMarkdownText, [System.Text.UTF8Encoding]::new($false))

    Write-Output "run_date=$RunDateTag"
    Write-Output "candidate_pdf_count=$($CandidatePdfs.Count)"
    Write-Output "screening_json_count=$($ScreeningFiles.Count)"
    Write-Output "database=$Database"
    Write-Output "output=$OutputDirectory"
    Write-Output "export=$ExportDirectory"
    Write-Output "review_csv=$NoScoreCsv"
    Write-Output "review_markdown=$ReviewMarkdown"
    Write-Output "worker_code=$WorkerCode"
    Write-Output "export_code=$ExportCode"
    if ($WorkerCode -ne 0) {
        exit $WorkerCode
    }
}
finally {
    foreach ($Name in $AllowedEnvNames) {
        [Environment]::SetEnvironmentVariable($Name, $PreviousProcessEnvironment[$Name], 'Process')
    }
}
