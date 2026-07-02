#!/usr/bin/env pwsh
# deploy.ps1 — push Hugo code changes to the NAS, rebuild the shared image,
# restart every instance's bot, and show the latest bot log.
#
# Usage (from the repo root):
#   .\deploy.ps1
#
# Connection details come from deploy.local.ps1 (gitignored). Copy
# deploy.local.ps1.example to deploy.local.ps1 and fill it in first.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$configPath = Join-Path $here "deploy.local.ps1"
if (-not (Test-Path $configPath)) {
    Write-Host "Missing deploy.local.ps1." -ForegroundColor Red
    Write-Host "Copy deploy.local.ps1.example to deploy.local.ps1 and fill in your NAS values."
    exit 1
}
. $configPath

$target = "$NasUser@$NasHost"

# 1. Push image source (Python + Dockerfile + requirements) to the primary folder.
#    Other instances share the image, so source only needs to reach the primary.
$sourceNames = @("Dockerfile", "requirements.txt") +
    (Get-ChildItem (Join-Path $here "*.py") | ForEach-Object Name)
$sourcePaths = $sourceNames | ForEach-Object { Join-Path $here $_ }

Write-Host "==> Pushing $($sourceNames.Count) files to ${target}:$PrimaryPath" -ForegroundColor Cyan
$scpArgs = @("-O", "-P", $NasPort) + $sourcePaths + @("${target}:$PrimaryPath/")
scp @scpArgs
if ($LASTEXITCODE -ne 0) { Write-Host "scp failed" -ForegroundColor Red; exit 1 }

# 2. Rebuild the shared image, restart the bot in every instance, tail the log.
$restart = ($Instances | ForEach-Object { "cd $_ && sudo $DockerPath compose up -d bot" }) -join " ; "
$remote  = "cd $PrimaryPath && sudo $DockerPath compose build && $restart ; " +
           "echo '--- recent bot log ---' ; tail -n 15 $PrimaryPath/state/hugo.log"

Write-Host "==> Rebuilding image and restarting $($Instances.Count) instance(s)" -ForegroundColor Cyan
Write-Host "    (you may be prompted for your SSH and sudo passwords)" -ForegroundColor DarkGray
ssh -t -p $NasPort $target $remote
if ($LASTEXITCODE -ne 0) { Write-Host "remote build/restart failed" -ForegroundColor Red; exit 1 }

Write-Host "==> Done. Check the log above for 'Bolt app is running!' with a fresh timestamp." -ForegroundColor Green
