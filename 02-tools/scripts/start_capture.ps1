param([int]$Port = 8080)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$mitmdump = Join-Path $projectRoot '02-tools\capture-venv\Scripts\mitmdump.exe'
$confDir = Join-Path $projectRoot '02-tools\mitmproxy\conf'
$flowPath = Join-Path $projectRoot '04-capture\flows\session1.flow'
$addon = Join-Path $PSScriptRoot 'capture_http.py'
& $mitmdump --listen-host 127.0.0.1 -p $Port --set "confdir=$confDir" --set flow_detail=0 --set console_eventlog_verbosity=warn --set 'save_stream_filter=~d megagamelog.com' -s $addon -w $flowPath
exit $LASTEXITCODE
