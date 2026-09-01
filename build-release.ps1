[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "dist"),
    [string]$SigningCertificateThumbprint,
    [string]$TimestampServer = "http://timestamp.digicert.com",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$skillSource = Join-Path $repoRoot "grok-video-studio"
$cli = Join-Path $skillSource "scripts\grok_video_studio.py"
if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) {
    throw "Skill CLI is missing: $cli"
}

$versionResult = & python $cli version | ConvertFrom-Json
if (-not $versionResult.ok -or [string]::IsNullOrWhiteSpace($versionResult.version)) {
    throw "Unable to read the Skill version."
}
$version = [string]$versionResult.version
$bundleName = "GrokVideoSkill-v$version"
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$zipPath = Join-Path $outputRoot "$bundleName.zip"
$checksumPath = Join-Path $outputRoot "$bundleName.sha256"
$manifestPath = Join-Path $outputRoot "$bundleName.version.json"
foreach ($path in @($zipPath, $checksumPath, $manifestPath)) {
    if ((Test-Path -LiteralPath $path) -and -not $Force) {
        throw "Release output already exists: $path. Use -Force to replace this exact artifact set."
    }
}

$stagingRoot = Join-Path ([IO.Path]::GetTempPath()) ("gvs-release-" + [guid]::NewGuid().ToString("N"))
$bundleRoot = Join-Path $stagingRoot $bundleName
$signed = $false
$signatureStatus = "NotSigned"
try {
    New-Item -ItemType Directory -Path $bundleRoot -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $repoRoot "install.ps1") -Destination $bundleRoot
    Copy-Item -LiteralPath (Join-Path $repoRoot "README.md") -Destination $bundleRoot
    Copy-Item -LiteralPath (Join-Path $repoRoot "docs") -Destination $bundleRoot -Recurse
    Copy-Item -LiteralPath $skillSource -Destination $bundleRoot -Recurse

    $stagedInstaller = Join-Path $bundleRoot "install.ps1"
    if (-not [string]::IsNullOrWhiteSpace($SigningCertificateThumbprint)) {
        $normalizedThumbprint = $SigningCertificateThumbprint.Replace(" ", "").ToUpperInvariant()
        $certificatePath = "Cert:\CurrentUser\My\$normalizedThumbprint"
        $certificate = Get-Item -LiteralPath $certificatePath -ErrorAction Stop
        if (-not $certificate.HasPrivateKey) {
            throw "The signing certificate has no private key: $normalizedThumbprint"
        }
        $signature = Set-AuthenticodeSignature -FilePath $stagedInstaller -Certificate $certificate -HashAlgorithm SHA256 -TimestampServer $TimestampServer
        $signatureStatus = [string]$signature.Status
        if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
            throw "Authenticode signing failed: $($signature.StatusMessage)"
        }
        $signed = $true
    }

    $commit = (& git -C $repoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read the Git commit for the release manifest."
    }
    $dirty = -not [string]::IsNullOrWhiteSpace((& git -C $repoRoot status --porcelain))
    $manifest = [ordered]@{
        schema_version = 1
        product = "Grok Video Studio"
        version = $version
        git_commit = $commit
        source_dirty = $dirty
        created_at_utc = [DateTime]::UtcNow.ToString("o")
        installer = "install.ps1"
        installer_signed = $signed
        signature_status = $signatureStatus
        profiles = @("basic", "upstream-dialogue", "precise-subtitles", "precise-voice", "lip-sync")
    }
    $stagedManifest = Join-Path $bundleRoot "version.json"
    $manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $stagedManifest -Encoding UTF8

    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    if (Test-Path -LiteralPath $manifestPath) { Remove-Item -LiteralPath $manifestPath -Force }
    Compress-Archive -LiteralPath $bundleRoot -DestinationPath $zipPath -CompressionLevel Optimal
    Copy-Item -LiteralPath $stagedManifest -Destination $manifestPath
    $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([IO.Path]::GetFileName($zipPath))" | Set-Content -LiteralPath $checksumPath -Encoding ASCII
    [pscustomobject]@{
        ok = $true
        version = $version
        zip = $zipPath
        checksum = $checksumPath
        manifest = $manifestPath
        sha256 = $hash
        installer_signed = $signed
        signature_status = $signatureStatus
    } | ConvertTo-Json -Depth 3
}
finally {
    if (Test-Path -LiteralPath $stagingRoot -PathType Container) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}
