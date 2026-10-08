$ErrorActionPreference='Stop'
# Windows PowerShell 5.1 在重定向或非 UTF-8 代码页下会把中文输出变成乱码，
# 这里显式固定控制台编码；$OutputEncoding 覆盖管道/重定向场景。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$taskRoot=[System.IO.Path]::GetFullPath($PSScriptRoot)
$taskState=Join-Path $taskRoot '07-server\run\processes.json'
if (-not (Test-Path -LiteralPath $taskState)) { Write-Output '没有本项目启动器的服务记录。'; exit 0 }
$taskSaved=Get-Content -LiteralPath $taskState -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($taskRecord in $taskSaved.processes) {
    $taskProcess=Get-CimInstance Win32_Process -Filter ("ProcessId="+[int]$taskRecord.pid) -ErrorAction SilentlyContinue
    if ($null -eq $taskProcess) { continue }
    $taskScript=[System.IO.Path]::GetFullPath($taskRecord.script)
    if (-not $taskScript.StartsWith($taskRoot+[System.IO.Path]::DirectorySeparatorChar,[System.StringComparison]::OrdinalIgnoreCase)) { throw '进程记录不在本项目目录内。' }
    if ($taskProcess.ExecutablePath -ne $taskSaved.python -or -not $taskProcess.CommandLine.Contains($taskScript)) {
        Write-Output ('旧服务PID '+$taskRecord.pid+' 已被其他进程复用，跳过该进程。')
        continue
    }
    & taskkill.exe /PID ([int]$taskRecord.pid) /F
    if ($LASTEXITCODE -ne 0) { throw '未确认服务已停止，保留进程记录供检查。' }
}
Remove-Item -LiteralPath $taskState
Write-Output '本项目后台服务已停止，存档和客户端资源保留。'
