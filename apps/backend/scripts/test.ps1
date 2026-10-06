param([switch]$Integration, [string]$FFmpeg = 'ffmpeg')
$ErrorActionPreference = 'Stop'
$backend = Split-Path $PSScriptRoot -Parent
$previousIntegration = $env:ONAIR_INTEGRATION_TEST
$previousFFmpeg = $env:ONAIR_FFMPEG
Push-Location $backend
try {
    if ($Integration) {
        $ping = & docker compose exec -T redis redis-cli ping
        if ($LASTEXITCODE -ne 0 -or $ping -notcontains 'PONG') {
            throw 'Start Docker Desktop and run docker compose up -d redis first.'
        }
        $null = Get-Command $FFmpeg -ErrorAction Stop
        $env:ONAIR_INTEGRATION_TEST = '1'
        $env:ONAIR_FFMPEG = $FFmpeg
    } else {
        $env:ONAIR_INTEGRATION_TEST = '0'
    }
    & ./.venv/Scripts/python.exe -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed.' }
} finally {
    $env:ONAIR_INTEGRATION_TEST = $previousIntegration
    $env:ONAIR_FFMPEG = $previousFFmpeg
    Pop-Location
}
