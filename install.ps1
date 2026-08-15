[CmdletBinding()]
param(
    [string]$Destination,
    [string]$Python = "python",
    [switch]$Force,
    [switch]$Configure,
    [switch]$SkipProviderTest
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $repoRoot "grok-video-studio"
if (-not (Test-Path -LiteralPath (Join-Path $source "SKILL.md") -PathType Leaf)) {
    throw "Skill source is incomplete: $source"
}

if ([string]::IsNullOrWhiteSpace($Destination)) {
    $codexRoot = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
        Join-Path $env:USERPROFILE ".codex"
    } else {
        $env:CODEX_HOME
    }
    $Destination = Join-Path (Join-Path $codexRoot "skills") "grok-video-studio"
}

$sourcePath = [IO.Path]::GetFullPath($source)
$destinationPath = [IO.Path]::GetFullPath($Destination)
if ($sourcePath -eq $destinationPath) {
    throw "Source and destination must be different directories."
}

$destinationParent = Split-Path -Parent $destinationPath
New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
if (Test-Path -LiteralPath $destinationPath) {
    if (-not $Force) {
        throw "Skill already exists at $destinationPath. Re-run with -Force to update it."
    }
    $resolvedParent = [IO.Path]::GetFullPath($destinationParent)
    if (-not $destinationPath.StartsWith($resolvedParent + [IO.Path]::DirectorySeparatorChar) -or
        [IO.Path]::GetFileName($destinationPath) -ne "grok-video-studio") {
        throw "Refusing to replace an unexpected destination: $destinationPath"
    }
    Remove-Item -LiteralPath $destinationPath -Recurse -Force
}

Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Recurse -Force
$cli = Join-Path $destinationPath "scripts\grok_video_studio.py"
& $Python $cli version
if ($LASTEXITCODE -ne 0) {
    throw "Installed Skill did not pass the version check."
}

if ($Configure) {
    $arguments = @($cli, "configure")
    if ($SkipProviderTest) {
        $arguments += "--skip-test"
    }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Skill configuration failed."
    }
}

Write-Host "Installed Grok Video Studio to $destinationPath"
if (-not $Configure) {
    Write-Host "Next: run python `"$cli`" configure in an interactive terminal, then run doctor."
}
