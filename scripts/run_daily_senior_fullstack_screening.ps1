[CmdletBinding()]
param(
    [string]$InputDirectory = (Join-Path $env:USERPROFILE 'Downloads'),
    [datetime]$RunDate = (Get-Date),
    [switch]$PreviewOnly,
    [switch]$AllowExternalModelData,
    [int]$MaxTasks = 0
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DateTag = $RunDate.ToString('yyyy-MM-dd')
$BatchTag = "$DateTag-senior-v14"
$Database = Join-Path $ProjectRoot "var\screening-$BatchTag.sqlite3"
$OutputDirectory = Join-Path $ProjectRoot "outputs\screening-$BatchTag"
$JobPrefix = '【全栈工程师_深圳 15-25K】'

if (-not (Test-Path -LiteralPath $InputDirectory -PathType Container)) {
    throw "Résumé input directory does not exist: $InputDirectory"
}

$CandidatePdfs = @(
    Get-ChildItem -LiteralPath $InputDirectory -Filter '*.pdf' -File -Recurse |
        Where-Object {
            $_.LastWriteTime.Date -eq $RunDate.Date -and
            $_.Name.StartsWith($JobPrefix, [System.StringComparison]::Ordinal)
        } |
        Sort-Object FullName
)

if ($PreviewOnly) {
    Write-Output "date=$DateTag"
    Write-Output "role=senior-fullstack-engineer"
    Write-Output "same_day_prefixed_pdf_count=$($CandidatePdfs.Count)"
    Write-Output "database=$Database"
    Write-Output "output=$OutputDirectory"
    Write-Output 'preview_only=true; no files were enqueued and no model was called.'
    return
}

if ($CandidatePdfs.Count -eq 0) {
    Write-Output "No same-day full-stack PDF resumes found for $DateTag."
    return
}

if (-not $AllowExternalModelData) {
    throw 'External model submission is paused. Pass -AllowExternalModelData only after explicit authorization to send de-identified résumé text to MiniMax.'
}

$AllowedEnvNames = @('MINIMAX_API_KEY', 'MINIMAX_API_ENDPOINT', 'MINIMAX_API_BASE')
$PreviousProcessEnvironment = [ordered]@{}
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

    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Project Python environment was not found: $Python"
    }

    $ChunkSize = 25
    for ($StartIndex = 0; $StartIndex -lt $CandidatePdfs.Count; $StartIndex += $ChunkSize) {
        $EndIndex = [Math]::Min($StartIndex + $ChunkSize - 1, $CandidatePdfs.Count - 1)
        $Chunk = @($CandidatePdfs[$StartIndex..$EndIndex])
        $EnqueueArguments = @(
            '-B', '-m', 'resume_screening',
            '--database', $Database,
            'enqueue',
            '--role', 'senior-fullstack-engineer',
            '--today'
        ) + @($Chunk | ForEach-Object { $_.FullName })

        & $Python @EnqueueArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Resume enqueue failed with exit code $LASTEXITCODE."
        }
    }

    $WorkerArguments = @(
        '-B', '-m', 'resume_screening',
        '--database', $Database,
        '--output', $OutputDirectory,
        'worker', '--once'
    )
    if ($MaxTasks -gt 0) {
        $WorkerArguments += @('--max-tasks', [string]$MaxTasks)
    }
    & $Python @WorkerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Resume screening worker failed with exit code $LASTEXITCODE."
    }
}
finally {
    foreach ($Name in $AllowedEnvNames) {
        [Environment]::SetEnvironmentVariable($Name, $PreviousProcessEnvironment[$Name], 'Process')
    }
}
