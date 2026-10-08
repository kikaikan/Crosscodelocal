#Requires -Version 5.1
<#
.SYNOPSIS
    CrossCore PS 服务日志窗口：实时显示本地服务的输出、错误和关键事件。

.DESCRIPTION
    在独立终端里跟随下面这些文件，从文件末尾开始，只显示新产生的内容：
        07-server/run/bootstrap.stdout.log   资源与启动服务输出
        07-server/run/bootstrap.stderr.log   资源与启动服务错误
        07-server/run/server.stdout.log      业务服务输出
        07-server/run/server.stderr.log      业务服务错误
        07-server/logs/server.jsonl          结构化事件，默认只显示 started / unsupported_handler /
                                             connection_rejected / handler_failure 等关键行
    启动时会先回显每个文件最近的关键行，方便直接看到刚刚发生的报错。

.PARAMETER Root
    项目根目录，默认取本脚本所在目录。

.PARAMETER TailLines
    启动时每个文件回显的历史行数，默认 30；填 0 表示不回显历史。

.PARAMETER PollMilliseconds
    轮询间隔毫秒，默认 400。

.PARAMETER DurationSeconds
    运行指定秒数后自动退出，默认 0 表示一直运行到按 Ctrl+C。

.PARAMETER IncludeAllEvents
    server.jsonl 不做过滤，显示全部事件（心跳每 5 秒一条，会非常吵）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\view_logs.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\view_logs.ps1 -IncludeAllEvents -TailLines 0
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [int]$TailLines = 30,
    [int]$PollMilliseconds = 400,
    [int]$DurationSeconds = 0,
    [switch]$IncludeAllEvents
)

# Some host wrappers start this window with an empty $PSScriptRoot, which made
# Join-Path bind '' and fail. Resolve the project root explicitly instead.
if (-not $Root) {
    if ($PSScriptRoot) { $Root = $PSScriptRoot }
    elseif ($MyInvocation.MyCommand.Path) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
    else { $Root = (Get-Location).Path }
}
if (-not (Test-Path -LiteralPath (Join-Path $Root '07-server\run'))) {
    $candidate = (Get-Location).Path
    if (Test-Path -LiteralPath (Join-Path $candidate '07-server\run')) { $Root = $candidate }
}
if (-not (Test-Path -LiteralPath (Join-Path $Root '07-server\run'))) {
    Write-Host ('未找到 07-server\run，请用 -Root 指定项目根目录：' + $Root) -ForegroundColor Red
    exit 2
}

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { $Host.UI.RawUI.WindowTitle = 'CrossCore PS 服务日志' } catch { }

$runDir = Join-Path $Root '07-server\run'
$serverLog = Join-Path $Root '07-server\logs\server.jsonl'

# 结构化事件里值得打断读者的行。disconnect / response 这类高频行默认不显示。
$notablePattern = '"event":"(started|unsupported_handler|connection_rejected|handler_failure|battle_entry_rejected|item_use_denied|item_use_rejected|mail_operation_rejected)"'
$eventFilter = $null
if (-not $IncludeAllEvents) { $eventFilter = $notablePattern }

$targets = @(
    [pscustomobject]@{ Path = (Join-Path $runDir 'bootstrap.stderr.log'); Tag = 'RES '; Color = 'DarkYellow'; Filter = $null },
    [pscustomobject]@{ Path = (Join-Path $runDir 'bootstrap.stdout.log'); Tag = 'RES '; Color = 'DarkGray';   Filter = $null },
    [pscustomobject]@{ Path = (Join-Path $runDir 'server.stderr.log');    Tag = 'ERR '; Color = 'Red';        Filter = $null },
    [pscustomobject]@{ Path = (Join-Path $runDir 'server.stdout.log');    Tag = 'OUT '; Color = 'Gray';       Filter = $null },
    [pscustomobject]@{ Path = $serverLog;                                 Tag = 'EVT '; Color = 'Cyan';       Filter = $eventFilter }
)

function Get-TaskLineColor {
    param([string]$DefaultColor, [string]$Line)
    if ($Line -match 'unsupported_handler|handler_failure|Traceback|CodecError|ConnectionAborted') { return 'Red' }
    if ($Line -match 'connection_rejected|TimeoutError|StorageError|rejected') { return 'Yellow' }
    if ($Line -match '"event":"started"') { return 'Green' }
    return $DefaultColor
}

function Format-TaskLogLine {
    param([string]$Line)
    # started 事件会带上完整 handler 列表，压成一行摘要，否则刷屏。
    if ($Line -match '"event":"started"') {
        $summary = 'started'
        if ($Line -match '"bind":"([^"]+)"') { $summary = $summary + ' bind=' + $Matches[1] }
        if ($Line -match '"query_port":(\d+)') { $summary = $summary + ' query=' + $Matches[1] }
        if ($Line -match '"game_port":(\d+)') { $summary = $summary + ' game=' + $Matches[1] }
        $handlers = [regex]::Match($Line, '"handlers":\[[^\]]*\]')
        if ($handlers.Success) { $summary = $summary + ' handlers=' + (($handlers.Value -split ',').Count) }
        return $summary + '（完整列表见 07-server/logs/server.jsonl）'
    }
    if ($Line.Length -gt 400) { return $Line.Substring(0, 400) + ' …（该行共 ' + $Line.Length + ' 字符）' }
    return $Line
}

function Write-TaskLogLine {
    param([string]$Tag, [string]$DefaultColor, [string]$Line)
    $rendered = Format-TaskLogLine -Line $Line
    $color = Get-TaskLineColor -DefaultColor $DefaultColor -Line $Line
    Write-Host ('[' + (Get-Date -Format 'HH:mm:ss') + '] [' + $Tag + '] ') -NoNewline -ForegroundColor $color
    Write-Host $rendered
}

function Read-TaskNewLines {
    param($Target, $State)
    if (-not (Test-Path -LiteralPath $Target.Path -PathType Leaf)) { return }
    $length = (Get-Item -LiteralPath $Target.Path).Length
    if ($length -lt $State.Position) {
        # 文件被截断或重建，从头重新跟随。
        $State.Position = 0L
        $State.Partial = ''
    }
    if ($length -eq $State.Position) { return }
    $count = [int]($length - $State.Position)
    $stream = [System.IO.File]::Open($Target.Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
        $null = $stream.Seek($State.Position, [System.IO.SeekOrigin]::Begin)
        $buffer = New-Object byte[] $count
        $read = $stream.Read($buffer, 0, $count)
    } finally {
        $stream.Dispose()
    }
    $State.Position = $State.Position + $read
    $text = $State.Partial + [System.Text.Encoding]::UTF8.GetString($buffer, 0, $read)
    $parts = $text -split '[\r\n]+'
    $State.Partial = $parts[-1]
    for ($index = 0; $index -lt ($parts.Count - 1); $index++) {
        $line = $parts[$index]
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($Target.Filter -and ($line -notmatch $Target.Filter)) { continue }
        Write-TaskLogLine -Tag $Target.Tag -DefaultColor $Target.Color -Line $line
    }
}

Write-Host ''
Write-Host 'CrossCore PS 服务日志' -ForegroundColor White
Write-Host ('  项目: ' + $Root)
Write-Host ('  时间: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
foreach ($target in $targets) {
    $exists = Test-Path -LiteralPath $target.Path -PathType Leaf
    $state = '缺失'
    if ($exists) { $state = '存在' }
    Write-Host ('  [' + $target.Tag + '] ' + $target.Path + '  ' + $state) -ForegroundColor DarkGray
}
if ($IncludeAllEvents) {
    Write-Host '  说明: -IncludeAllEvents 已开启，server.jsonl 显示全部事件。' -ForegroundColor DarkGray
} else {
    Write-Host '  说明: server.jsonl 只显示 started / unsupported_handler / connection_rejected / handler_failure 等关键行。' -ForegroundColor DarkGray
}

if ($TailLines -gt 0) {
    foreach ($target in $targets) {
        if (-not (Test-Path -LiteralPath $target.Path -PathType Leaf)) { continue }
        $lines = @(Get-Content -LiteralPath $target.Path -Encoding UTF8 -ErrorAction SilentlyContinue)
        if ($target.Filter) { $lines = @($lines | Where-Object { $_ -match $target.Filter }) }
        $tail = @($lines | Select-Object -Last $TailLines)
        if ($tail.Count -eq 0) { continue }
        Write-Host ''
        Write-Host ('--- ' + (Split-Path -Path $target.Path -Leaf) + ' 最近 ' + $tail.Count + ' 行 ---') -ForegroundColor DarkGray
        foreach ($line in $tail) { Write-TaskLogLine -Tag $target.Tag -DefaultColor $target.Color -Line $line }
    }
}

$state = @{}
foreach ($target in $targets) {
    $position = 0L
    if (Test-Path -LiteralPath $target.Path -PathType Leaf) { $position = (Get-Item -LiteralPath $target.Path).Length }
    $state[$target.Path] = [pscustomobject]@{ Position = $position; Partial = '' }
}

Write-Host ''
Write-Host '正在跟随新日志，按 Ctrl+C 关闭本窗口。' -ForegroundColor Green

$deadline = $null
if ($DurationSeconds -gt 0) { $deadline = (Get-Date).AddSeconds($DurationSeconds) }

while ($true) {
    if ($deadline -and ((Get-Date) -gt $deadline)) { break }
    foreach ($target in $targets) { Read-TaskNewLines -Target $target -State $state[$target.Path] }
    Start-Sleep -Milliseconds $PollMilliseconds
}

Write-Host '日志跟随已结束。' -ForegroundColor Green
