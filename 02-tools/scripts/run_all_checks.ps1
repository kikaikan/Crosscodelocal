#requires -Version 5.1
<#
.SYNOPSIS
  CrossCore 本地部署一键回归（P0.7）与 parity 报表生成（P0.10）。

.DESCRIPTION
  依次执行（编码门禁排在最前：最便宜，且它检查的对象包含本脚本自己）：
    1. 脚本编码门禁          python -B -X utf8 02-tools/scripts/check_script_encoding.py --json
    2. 全量服务端单测        python -B -X utf8 -m unittest discover -s 07-server/tests -p "test_*.py"
    3. 引导索引回归          python -B -X utf8 02-tools/scripts/test_bootstrap_index.py
    4. 断线风险审计（重跑）  python -B -X utf8 02-tools/scripts/audit_disconnect_risks.py
    5. 教程加载审计          python -B -X utf8 02-tools/scripts/audit_tutorial_loading.py
    6. 静态覆盖率            python -B -X utf8 02-tools/scripts/feature_coverage.py
    7. 生成 parity 报表      python -B -X utf8 02-tools/scripts/parity_report.py --steps-json <临时文件>

  python 解释器优先取 07-server/run/processes.json 的 python 字段，取不到再回退 PATH。
  任何一步失败：仍然尝试生成 parity 报表，随后打印失败步骤与尾部输出，并以非零退出。
  本脚本自身必须符合仓库编码契约：UTF-8 with BOM + CRLF（check_script_encoding.py 会检查它自己）。
  步骤 4 会重跑断线审计覆盖自己的输出（server.jsonl 已从 3.76MB 增长，旧 --check 基线已漂移）。

.PARAMETER PythonExe
  显式指定 python.exe，覆盖自动解析。

.PARAMETER StepsJsonPath
  步骤结果 JSON 的保存路径；默认写到系统临时目录并在结束时删除。

.PARAMETER KeepStepsJson
  保留步骤结果 JSON 并打印路径，便于人工复核。

.EXAMPLE
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File 02-tools/scripts/run_all_checks.ps1
#>

[CmdletBinding()]
param(
    [string]$PythonExe = '',
    [string]$StepsJsonPath = '',
    [switch]$KeepStepsJson
)

$ErrorActionPreference = 'Stop'
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    Write-Host ('[warn] 无法设置 UTF-8 控制台编码：' + $_.Exception.Message)
}

$Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
Set-Location -LiteralPath $Root

function Resolve-PythonInterpreter {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path -LiteralPath $Explicit -PathType Leaf)) {
            throw ('指定的 PythonExe 不存在：' + $Explicit)
        }
        return [pscustomobject]@{ Path = [System.IO.Path]::GetFullPath($Explicit); Source = 'command-line' }
    }
    $statePath = Join-Path $Root '07-server\run\processes.json'
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        try {
            $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($state.python -and (Test-Path -LiteralPath ([string]$state.python) -PathType Leaf)) {
                return [pscustomobject]@{ Path = [string]$state.python; Source = '07-server/run/processes.json' }
            }
        } catch {
            Write-Host ('[warn] processes.json 读取失败，回退 PATH：' + $_.Exception.Message)
        }
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        $resolved = $command.Source
        if (-not $resolved) { $resolved = $command.Path }
        if ($resolved) {
            return [pscustomobject]@{ Path = [string]$resolved; Source = 'PATH' }
        }
    }
    throw '找不到 python：07-server/run/processes.json 没有可用的 python 字段，PATH 里也没有 python。'
}

function Get-OutputTail {
    param([string]$Text, [int]$Lines = 40)
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    $rows = [regex]::Split($Text.TrimEnd(), "\r?\n")
    if ($rows.Count -le $Lines) { return ($rows -join [Environment]::NewLine) }
    $tail = $rows[($rows.Count - $Lines)..($rows.Count - 1)]
    return ($tail -join [Environment]::NewLine)
}

# 报告里不留本机绝对路径：替换时允许断字位置存在换行，因为 PowerShell 的错误流
# 会把 python 的长行按列宽折断，绝对路径可能被 CRLF 从中间切开。
function Replace-LooseText {
    param([string]$Value, [string]$Needle, [string]$Placeholder)
    if ([string]::IsNullOrEmpty($Value) -or [string]::IsNullOrEmpty($Needle)) { return $Value }
    $pattern = ''
    foreach ($character in $Needle.ToCharArray()) {
        $pattern += [regex]::Escape([string]$character) + '\r?\n?'
    }
    return [regex]::Replace($Value, $pattern, $Placeholder)
}

function ConvertTo-SanitizedOutput {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    $result = $Text
    if ($script:PythonPath) {
        $pythonDirectory = [System.IO.Path]::GetDirectoryName($script:PythonPath)
        if ($pythonDirectory) { $result = Replace-LooseText -Value $result -Needle $pythonDirectory -Placeholder '<python>' }
    }
    $needles = [ordered]@{}
    if ($script:PythonPath) { $needles[$script:PythonPath] = '<python>' }
    if ($env:USERPROFILE) { $needles[$env:USERPROFILE] = '<user-home>' }
    if ($env:TEMP) { $needles[$env:TEMP] = '<temp>' }
    if ($Root) { $needles[$Root] = '<root>' }
    if ($env:USERNAME) { $needles[[string]$env:USERNAME] = '<user>' }
    foreach ($needle in $needles.Keys) {
        $result = Replace-LooseText -Value $result -Needle $needle -Placeholder $needles[$needle]
    }
    return $result
}

$script:PythonPath = ''
$script:StepResults = New-Object System.Collections.ArrayList

function Invoke-RegressionStep {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$MetricKind = ''
    )
    $logFile = [System.IO.Path]::GetTempFileName()
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $exitCode = -1
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $script:PythonPath @Arguments *> $logFile
        if ($null -ne $LASTEXITCODE) { $exitCode = [int]$LASTEXITCODE } else { $exitCode = 0 }
    } catch {
        $exitCode = 1
        Add-Content -LiteralPath $logFile -Value ('[run_all_checks] 步骤执行异常：' + $_.Exception.Message)
    } finally {
        $ErrorActionPreference = $previous
        $timer.Stop()
    }
    $output = ''
    if (Test-Path -LiteralPath $logFile -PathType Leaf) {
        $output = Get-Content -LiteralPath $logFile -Raw
    }
    Remove-Item -LiteralPath $logFile -Force -ErrorAction SilentlyContinue

    $record = [ordered]@{
        name        = $Name
        title       = $Title
        command     = ('python ' + ($Arguments -join ' '))
        exit_code   = $exitCode
        seconds     = [math]::Round($timer.Elapsed.TotalSeconds, 2)
        status      = $(if ($exitCode -eq 0) { 'ok' } else { 'failed' })
        output_tail = (Get-OutputTail -Text (ConvertTo-SanitizedOutput -Text $output))
    }

    if ($MetricKind -eq 'unittest') {
        $record['tests_ran'] = $null
        $record['tests_failed'] = $null
        $match = [regex]::Match($output, 'Ran\s+(\d+)\s+tests?\s+in\s+([0-9.]+)s')
        if ($match.Success) {
            $record['tests_ran'] = [int]$match.Groups[1].Value
            $record['tests_internal_seconds'] = [double]$match.Groups[2].Value
        }
        $failedMatch = [regex]::Match($output, 'FAILED\s*\(([^)]*)\)')
        if ($failedMatch.Success) {
            $failures = 0
            $errors = 0
            $matchFailures = [regex]::Match($failedMatch.Groups[1].Value, 'failures=(\d+)')
            if ($matchFailures.Success) { $failures = [int]$matchFailures.Groups[1].Value }
            $matchErrors = [regex]::Match($failedMatch.Groups[1].Value, 'errors=(\d+)')
            if ($matchErrors.Success) { $errors = [int]$matchErrors.Groups[1].Value }
            $record['tests_failed'] = $failures + $errors
        } elseif ([regex]::IsMatch($output, '(?m)^OK\b')) {
            $record['tests_failed'] = 0
        }
    }

    if ($MetricKind -eq 'encoding') {
        $record['violations'] = $null
        $record['checked'] = $null
        try {
            $parsed = $output | ConvertFrom-Json
            if ($null -ne $parsed.violations) { $record['violations'] = [int]$parsed.violations }
            if ($null -ne $parsed.checked) { $record['checked'] = [int]$parsed.checked }
        } catch {
            Write-Host ('[warn] 无法解析编码门禁 JSON（仍以退出码判定）：' + $_.Exception.Message)
        }
    }

    [void]$script:StepResults.Add($record)
    return $record
}

$Python = Resolve-PythonInterpreter -Explicit $PythonExe
$script:PythonPath = $Python.Path
Write-Host ('python 解释器：' + $Python.Path + '（来源：' + $Python.Source + '）')
Write-Host ('仓库根目录：' + $Root)

$generatedAt = (Get-Date).ToString('o')
$common = @('-B', '-X', 'utf8')

$definition = @(
    [ordered]@{ name = 'script_encoding';  title = '脚本编码门禁 (check_script_encoding.py)'; args = @('02-tools/scripts/check_script_encoding.py', '--json'); metric = 'encoding' },
    [ordered]@{ name = 'unittest';         title = '全量服务端单测 (07-server/tests)';      args = @('-m', 'unittest', 'discover', '-s', '07-server/tests', '-p', 'test_*.py'); metric = 'unittest' },
    [ordered]@{ name = 'bootstrap_index';  title = '引导索引回归 (test_bootstrap_index.py)'; args = @('02-tools/scripts/test_bootstrap_index.py'); metric = 'unittest' },
    [ordered]@{ name = 'disconnect_audit'; title = '断线风险审计（重跑基线）';               args = @('02-tools/scripts/audit_disconnect_risks.py'); metric = '' },
    [ordered]@{ name = 'tutorial_audit';   title = '教程加载审计';                          args = @('02-tools/scripts/audit_tutorial_loading.py'); metric = '' },
    [ordered]@{ name = 'feature_coverage'; title = '静态覆盖率 (feature_coverage.py)';       args = @('02-tools/scripts/feature_coverage.py'); metric = '' }
)

$failed = New-Object System.Collections.ArrayList
Write-Host ''
foreach ($item in $definition) {
    Write-Host ('[run ] ' + $item.title)
    $record = Invoke-RegressionStep -Name $item.name -Title $item.title -Arguments ($common + $item.args) -MetricKind $item.metric
    $detail = ''
    if ($record.Contains('tests_ran') -and $null -ne $record['tests_ran']) {
        $detail = ' | tests=' + $record['tests_ran'] + ' failed=' + $record['tests_failed']
    } elseif ($record.Contains('violations') -and $null -ne $record['violations']) {
        $detail = ' | violations=' + $record['violations'] + ' checked=' + $record['checked']
    }
    if ($record['exit_code'] -ne 0) {
        [void]$failed.Add($record)
        Write-Host ('[FAIL] ' + $item.title + ' exit=' + $record['exit_code'] + ' (' + $record['seconds'] + 's)' + $detail)
    } else {
        Write-Host ('[ok  ] ' + $item.title + ' (' + $record['seconds'] + 's)' + $detail)
    }
}

$stepsPath = $StepsJsonPath
$temporarySteps = $false
if (-not $stepsPath) {
    $stepsPath = [System.IO.Path]::GetTempFileName()
    $temporarySteps = $true
} else {
    $stepsPath = [System.IO.Path]::GetFullPath($stepsPath)
}
$stepsDoc = [ordered]@{
    generated_at  = $generatedAt
    root          = '.'
    python        = '<python>'
    python_source = $Python.Source
    steps         = $script:StepResults
}
$stepsJson = $stepsDoc | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($stepsPath, $stepsJson, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ''
Write-Host ('步骤结果：' + $stepsPath)

Write-Host ''
Write-Host '[run ] 生成 parity 报表 (parity_report.py)'
$reportArgs = $common + @('02-tools/scripts/parity_report.py', '--steps-json', $stepsPath, '--generated-at', $generatedAt)
$reportRecord = Invoke-RegressionStep -Name 'parity_report' -Title '生成 parity 报表 (parity_report.py)' -Arguments $reportArgs -MetricKind ''
if ($reportRecord['exit_code'] -ne 0) {
    [void]$failed.Add($reportRecord)
    Write-Host ('[FAIL] 生成 parity 报表 exit=' + $reportRecord['exit_code'] + ' (' + $reportRecord['seconds'] + 's)')
} else {
    Write-Host ('[ok  ] 生成 parity 报表 (' + $reportRecord['seconds'] + 's)')
    Write-Host $reportRecord['output_tail']
}

if ($failed.Count -gt 0) {
    Write-Host ''
    Write-Host ('回归失败：' + $failed.Count + ' 个步骤非零退出（详见下方尾部输出）。')
    foreach ($item in $failed) {
        Write-Host ''
        Write-Host ('--- ' + $item.title + ' (exit=' + $item.exit_code + ', ' + $item.seconds + 's) ---')
        Write-Host $item.output_tail
    }
    if ($temporarySteps -and -not $KeepStepsJson) {
        Remove-Item -LiteralPath $stepsPath -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host ''
        Write-Host ('保留步骤结果：' + $stepsPath)
    }
    exit 1
}

Write-Host ''
Write-Host '全部检查通过。parity 报表：90-notes/parity-report.json / 90-notes/parity-report.md'
if ($temporarySteps -and -not $KeepStepsJson) {
    Remove-Item -LiteralPath $stepsPath -Force -ErrorAction SilentlyContinue
} else {
    Write-Host ('保留步骤结果：' + $stepsPath)
}
exit 0
