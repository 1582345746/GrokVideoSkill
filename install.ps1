[CmdletBinding()]
param(
    [string]$Destination,
    [string]$Python = "python",
    [switch]$Force,
    [switch]$Configure,
    [switch]$ConfigureFromStdin,
    [switch]$SkipProviderTest,
    [ValidateSet("core", "native-dialogue", "local-voice", "full-dialogue")]
    [string]$ComponentProfile = "core",
    [string]$ComponentSourceRoot,
    [string]$ComponentModelsRoot,
    [switch]$InstallComponents,
    [switch]$IncludeComponentModels,
    [switch]$AcceptComponentDownloads,
    [switch]$StartComponents
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
if ($Configure -and $ConfigureFromStdin) {
    throw "Use either -Configure or -ConfigureFromStdin, not both."
}
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

if ($Configure -or $ConfigureFromStdin) {
    $arguments = @($cli, "configure")
    if ($ConfigureFromStdin) {
        $arguments += "--credentials-stdin"
    }
    if ($SkipProviderTest) {
        $arguments += "--skip-test"
    }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Skill configuration failed."
    }
}

$componentArguments = @($cli, "components-configure", "--profile", $ComponentProfile)
if (-not [string]::IsNullOrWhiteSpace($ComponentSourceRoot)) {
    $componentArguments += @("--source-root", $ComponentSourceRoot)
}
if (-not [string]::IsNullOrWhiteSpace($ComponentModelsRoot)) {
    $componentArguments += @("--models-root", $ComponentModelsRoot)
}
& $Python @componentArguments
if ($LASTEXITCODE -ne 0) {
    throw "Component profile configuration failed."
}

if ($InstallComponents) {
    if ($ComponentProfile -notin @("local-voice", "full-dialogue")) {
        throw "-InstallComponents requires -ComponentProfile local-voice or full-dialogue."
    }
    if (-not $AcceptComponentDownloads) {
        throw "Component installation requires -AcceptComponentDownloads after the user approves downloads."
    }
    & $Python $cli components-install --profile $ComponentProfile --accept-downloads
    if ($LASTEXITCODE -ne 0) {
        throw "Optional component source installation failed."
    }
    $setupArguments = @($cli, "components-setup", "--profile", $ComponentProfile, "--accept-downloads")
    if ($IncludeComponentModels) {
        $setupArguments += "--include-models"
    }
    & $Python @setupArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Optional component runtime setup failed."
    }
}

if ($StartComponents) {
    if (-not $InstallComponents -or -not $IncludeComponentModels) {
        throw "-StartComponents requires -InstallComponents and -IncludeComponentModels."
    }
    & $Python $cli components-start --profile $ComponentProfile
    if ($LASTEXITCODE -ne 0) {
        throw "Optional component services failed to start."
    }
}

Write-Host "Installed Grok Video Studio to $destinationPath"
Write-Host "Component profile: $ComponentProfile"
if (-not $Configure -and -not $ConfigureFromStdin) {
    Write-Host "Next: Codex can run python `"$cli`" configure --credentials-stdin --skip-test and provide the credential JSON through process stdin."
}
