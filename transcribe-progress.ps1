# 读取 logs/srt-daemon.log，估算正在进行的转录进度
$ErrorActionPreference = 'SilentlyContinue'
$log = Join-Path $PSScriptRoot 'logs\srt-daemon.log'
if (-not (Test-Path $log)) {
    Write-Host "还没有日志：$log"
    exit 1
}
$lines = Get-Content $log -Encoding UTF8

function Format-Span([int]$seconds) {
    $ts = [timespan]::FromSeconds($seconds)
    return ('{0:hh\:mm\:ss}' -f $ts)
}

$total = 0
$started = $null
$durationLine = $lines | Select-String 'Processing audio with duration' | Select-Object -Last 1
if ($durationLine -and $durationLine.Line -match 'duration (\d+):(\d+):(\d+)') {
    $total = [int]$matches[1] * 3600 + [int]$matches[2] * 60 + [int]$matches[3]
    if ($durationLine.Line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
        $started = [datetime]::ParseExact($matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
    }
}

$done = 0
$segments = 0
$segTime = $null
$segLine = $lines | Select-String 'segments, at (\d+):(\d+):(\d+)' | Select-Object -Last 1
if ($segLine) {
    if ($segLine.Line -match '\.\.\. (\d+) segments') { $segments = [int]$matches[1] }
    if ($segLine.Line -match 'at (\d+):(\d+):(\d+)') {
        $done = [int]$matches[1] * 3600 + [int]$matches[2] * 60 + [int]$matches[3]
    }
    if ($segLine.Line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
        $segTime = [datetime]::ParseExact($matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
    }
}

$fileLine = $lines | Select-String 'Transcribing:' | Select-Object -Last 1
$name = if ($fileLine) { $fileLine.Line -replace '^.*Transcribing:\s*', '' } else { '(未知)' }

$percent = if ($total -gt 0) { [math]::Round($done * 100 / $total, 1) } else { 0 }
$eta = '统计中'
$ratio = 0
if ($started -and $segTime -and $done -gt 0 -and $total -gt $done) {
    $wall = ($segTime - $started).TotalSeconds
    if ($wall -gt 0) {
        $ratio = $wall / $done
        $minutes = [math]::Round(($total - $done) * $ratio / 60)
        $eta = "约 $minutes 分钟（约 $([math]::Round($ratio, 1))x 实时）"
    }
}

Write-Host ''
Write-Host "文件    : $name"
Write-Host "总时长  : $(Format-Span $total)"
Write-Host "已识别  : $(Format-Span $done)  ($percent%)  $segments 段"
Write-Host "预计还需: $eta  (之后还要约 1/3 音频时长做说话人聚类)"
if ($ratio -gt 2.2) {
    Write-Host ''
    Write-Host "提示    : 这台机器 large-v3 约 $([math]::Round($ratio, 1))x 实时，"
    Write-Host "          medium 约 1.1x、small 约 0.6x（改 transcribe.model 或在界面里选）"
}
Write-Host ''
