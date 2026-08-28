param(
    [string]$Version = "0.2.1-rc1",
    [string]$IsccPath = "",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path $PSScriptRoot).Path
$expectedVersion = python -c "from app.release import APP_VERSION; print(APP_VERSION)"
if ($Version -ne $expectedVersion) { throw "Version '$Version' must equal source APP_VERSION '$expectedVersion'." }
if ((git status --porcelain).Length -ne 0) { throw "Refusing build: commit or revert tracked source changes first." }
$buildSha = (git rev-parse HEAD).Trim().ToLowerInvariant()
if ($buildSha -notmatch '^[0-9a-f]{40}$') { throw "Could not determine exact Git HEAD." }

$provenancePath = Join-Path ([System.IO.Path]::GetTempPath()) "build_provenance.json"
@{
    version = $Version
    build_sha = $buildSha
    build_timestamp_utc = [DateTime]::UtcNow.ToString("o")
    python_version = (python --version 2>&1).ToString().Trim()
    pyinstaller_version = (python -m PyInstaller --version).ToString().Trim()
} | ConvertTo-Json | Set-Content -LiteralPath $provenancePath -Encoding utf8

$distRoot = Join-Path $root "dist"
$workPath = Join-Path ([System.IO.Path]::GetTempPath()) "hoso-manager-build-$buildSha"
$bundleName = "HosoManager-v$Version"
$env:HOSO_BUILD_PROVENANCE = $provenancePath
$env:HOSO_BUNDLE_NAME = $bundleName
try {
    python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workPath (Join-Path $root "HosoManager.spec")
} finally {
    Remove-Item -LiteralPath Env:HOSO_BUILD_PROVENANCE -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath Env:HOSO_BUNDLE_NAME -ErrorAction SilentlyContinue
}
$bundlePath = Join-Path $distRoot $bundleName
Copy-Item -LiteralPath (Join-Path $root "docs/hoso-digitization-manager-handoff/example-config.json") -Destination (Join-Path $bundlePath "config.example.json") -Force

if (-not $SkipInstaller) {
    if (-not $IsccPath) {
        $defaultIscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
        if (Test-Path -LiteralPath $defaultIscc) { $IsccPath = $defaultIscc }
    }
    if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath)) { throw "Inno Setup ISCC.exe is required, or use -SkipInstaller for an unsigned bundle-only build." }
    & $IsccPath "/DReleaseVersion=$Version" "/DSourceDir=$bundlePath" (Join-Path $root "installer\HosoManager.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE." }
}

$installer = Join-Path $distRoot "installer\HosoManager-Setup-v$Version.exe"
$result = [ordered]@{
    version = $Version
    build_sha = $buildSha
    bundle = $bundlePath
    installer = if (Test-Path -LiteralPath $installer) { $installer } else { $null }
    installer_sha256 = if (Test-Path -LiteralPath $installer) { (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant() } else { $null }
}
$result | ConvertTo-Json
