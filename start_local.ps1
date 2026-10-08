param(
    [string]$PythonExe = (Get-ChildItem "$env:USERPROFILE\.cache\*\*\dependencies\python\python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName,
    [string[]]$Handlers = @('handlers.player_state','handlers.initialization','handlers.gacha','handlers.item_pool','handlers.sub_talent','handlers.sign_in','handlers.cards_items','handlers.item_exchange','handlers.battle','handlers.tasks','handlers.shop','handlers.mail','handlers.download_reward','handlers.gifts','handlers.skins','handlers.local_gaps','handlers.equipment','handlers.building','handlers.dorm','handlers.ability','handlers.panels'),
    [switch]$RestrictPools,
    [switch]$RestrictActivities,
    [switch]$RestrictIllustrations,
    [switch]$OpenControl,
    [switch]$NoConsole
)
$ErrorActionPreference='Stop'
# 解释器按「USERPROFILE 运行期缓存目录通配 → PATH → 裸命令名」解析：运行期目录名随宿主
# （codex / dsh 等）变化，仓库里不写死任何本机路径或宿主名；显式传入 -PythonExe 时保持原校验。
if (-not $PSBoundParameters.ContainsKey('PythonExe') -and (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf))) {
    $taskPythonCommand=Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $taskPythonCommand) {
        $PythonExe=$taskPythonCommand.Source
        if (-not $PythonExe) { $PythonExe=$taskPythonCommand.Path }
    }
}
if (-not $PythonExe) { $PythonExe='python' }
# Windows PowerShell 5.1 在重定向或非 UTF-8 代码页下会把中文输出变成乱码，
# 这里显式固定控制台编码；$OutputEncoding 覆盖管道/重定向场景。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
function Start-TaskLogWindow {
    param([string]$TaskRoot)
    $taskViewer=Join-Path $TaskRoot 'view_logs.ps1'
    if (-not (Test-Path -LiteralPath $taskViewer -PathType Leaf)) {
        Write-Output '未找到 view_logs.ps1，跳过服务日志窗口。'
        return
    }
    $taskPattern='-File\s+"?' + [regex]::Escape($taskViewer)
    $taskRunning=@(Get-CimInstance Win32_Process -Filter "Name='pwsh.exe' or Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $taskPattern })
    if ($taskRunning.Count -gt 0) {
        Write-Output '服务日志窗口已在运行，未重复打开。'
        return
    }
    $taskHost=(Get-Process -Id $PID).Path
    Start-Process -FilePath $taskHost -ArgumentList @('-NoLogo','-NoProfile','-NoExit','-ExecutionPolicy','Bypass','-File',('"'+$taskViewer+'"'),'-Root',('"'+$TaskRoot+'"')) -WorkingDirectory $TaskRoot | Out-Null
    Write-Output '已打开服务日志窗口：服务输出、错误和关键事件会实时显示；关掉该窗口不影响服务运行。'
}
$taskRoot=[System.IO.Path]::GetFullPath($PSScriptRoot)
$taskRun=Join-Path $taskRoot '07-server\run'
$taskState=Join-Path $taskRun 'processes.json'
New-Item -ItemType Directory -Force -Path $taskRun | Out-Null
if (Test-Path -LiteralPath $taskState) {
    $taskSaved=Get-Content -LiteralPath $taskState -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskOwned=@()
    foreach ($taskRecord in $taskSaved.processes) {
        $taskExisting=Get-CimInstance Win32_Process -Filter ("ProcessId="+[int]$taskRecord.pid) -ErrorAction SilentlyContinue
        if ($null -ne $taskExisting -and $taskExisting.ExecutablePath -eq $taskSaved.python -and
            $taskExisting.CommandLine.Contains([System.IO.Path]::GetFullPath($taskRecord.script))) {
            $taskOwned+= $taskRecord
        }
    }
    if ($taskOwned.Count -eq 2) {
        try {
            $taskCurrent=Invoke-WebRequest -Uri 'http://127.0.0.1:18080/control/state' -TimeoutSec 5 -UseBasicParsing
            $taskPorts=@([System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() | Where-Object { $_.Address.ToString() -eq '127.0.0.1' -and $_.Port -in @(18080,19001,19041) })
            if ($taskCurrent.StatusCode -eq 200 -and $taskPorts.Count -eq 3) {
                Write-Output '本地服务已经启动。控制台：http://127.0.0.1:18080/control'
                if (-not $NoConsole) { Start-TaskLogWindow -TaskRoot $taskRoot }
                if ($OpenControl) { Start-Process 'http://127.0.0.1:18080/control' }
                return
            }
        } catch { }
    }
    if ($taskOwned.Count -gt 0) { throw '已有本项目服务但未全部就绪。先运行 stop_local.ps1，再重新启动。' }
    # A reboot can reuse old PIDs. Discard only this project's stale record.
    Remove-Item -LiteralPath $taskState
}
foreach ($taskPort in @(18080,19001,19041)) {
    if ([System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() | Where-Object { $_.Port -eq $taskPort }) {
        throw "本地端口 $taskPort 已占用，未启动重复服务。"
    }
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -and -not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) { throw '指定 Python 不存在。' }
$taskProcesses=@()
try {
    $taskStaticScript=Join-Path $taskRoot '02-tools\scripts\serve_bootstrap.py'
    $taskStatic=Start-Process -FilePath $PythonExe -ArgumentList @('-u','-X','utf8','-B', ('"'+$taskStaticScript+'"')) -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRun 'bootstrap.stdout.log') -RedirectStandardError (Join-Path $taskRun 'bootstrap.stderr.log')
    $taskProcesses+=@{pid=$taskStatic.Id;script=$taskStaticScript;role='bootstrap'}
    $taskServerScript=Join-Path $taskRoot '07-server\server_core.py'
    $taskArguments=@('-u','-X','utf8','-B', ('"'+$taskServerScript+'"'))
    if ($RestrictPools) { $taskArguments += '--restrict-pools' }
    if ($RestrictActivities) { $taskArguments += '--restrict-activities' }
    if ($RestrictIllustrations) { $taskArguments += '--restrict-illustrations' }
    foreach ($taskHandler in $Handlers) {
        if ($taskHandler -notmatch '^handlers\.[A-Za-z][A-Za-z0-9_]*$') { throw '无效的本地玩法模块名称。' }
        $taskArguments+=@('--handler',$taskHandler)
    }
    $taskServer=Start-Process -FilePath $PythonExe -ArgumentList $taskArguments -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRun 'server.stdout.log') -RedirectStandardError (Join-Path $taskRun 'server.stderr.log')
    $taskProcesses+=@{pid=$taskServer.Id;script=$taskServerScript;role='business'}
    $taskJson=@{python=$PythonExe;processes=$taskProcesses;started=(Get-Date).ToString('o')} | ConvertTo-Json -Depth 5
    # 不用 Set-Content -Encoding utf8：Windows PowerShell 5.1 会写 BOM，pwsh 7 不会，行为不一致。
    # 显式写入无 BOM 的 UTF-8，保证两个宿主产出的 processes.json 逐字节一致。
    [System.IO.File]::WriteAllText($taskState, $taskJson, (New-Object System.Text.UTF8Encoding($false)))
    $taskReady=$false
    Write-Output '正在加载本地资源，首次启动可能需要较长时间。'
    for ($taskTry=0; $taskTry -lt 600; $taskTry++) {
        if ($taskStatic.HasExited -or $taskServer.HasExited) { throw '服务启动失败：运行 view_logs.ps1 跟随日志，或查看 07-server/run 的 stderr.log。' }
        $taskStatic.Refresh()
        $taskServer.Refresh()
        $taskListening=@([System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() | Where-Object { $_.Address.ToString() -eq '127.0.0.1' -and $_.Port -in @(18080,19001,19041) })
        if ($taskListening.Count -eq 3) { $taskReady=$true; break }
        Start-Sleep -Milliseconds 200
    }
    if (-not $taskReady) { throw '本地服务未在等待时间内开始监听。' }
    foreach ($taskUrl in @('http://127.0.0.1:18080/cross/release/android/curr/update3.3.0.txt', 'http://127.0.0.1:18080/control/state', 'http://127.0.0.1:18080/control/catalog')) {
        $taskResponse=Invoke-WebRequest -Uri $taskUrl -TimeoutSec 15 -UseBasicParsing
        if ($taskResponse.StatusCode -ne 200) { throw ('本地服务未就绪：'+$taskUrl) }
    }
    Write-Output '本地资源与业务服务已启动：18080 / 19001 / 19041。存档：07-server/data/players.sqlite3。'
    Write-Output '控制台：http://127.0.0.1:18080/control'
    if (-not $NoConsole) { Start-TaskLogWindow -TaskRoot $taskRoot }
    if ($OpenControl) { Start-Process 'http://127.0.0.1:18080/control' }
    Write-Output '日志同时写入 07-server/run/*.log 与 07-server/logs/server.jsonl；不想开日志窗口时加 -NoConsole。'
    Write-Output '这是当前已实现功能的开发版本；完整停服后游玩验收仍需实际客户端验证。'
} catch {
    foreach ($taskProcess in $taskProcesses) { & taskkill.exe /PID $taskProcess.pid /F 2>&1 | Out-Null }
    if (Test-Path -LiteralPath $taskState) { Remove-Item -LiteralPath $taskState }
    throw
}
