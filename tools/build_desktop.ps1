[CmdletBinding()]
param(
    [string]$BuildPython = '',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$')]
    [string]$ReleaseId = ('windows-x64-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')),
    [switch]$Offline
)

# Self-contained desktop distribution: private Chromium/Electron, Python, and
# application code. Neither a system browser nor .NET is needed at runtime.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$desktopWorkspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$desktopBuildRoot = Join-Path $desktopWorkspace 'build\windows'
$desktopCache = Join-Path $desktopBuildRoot 'cache\desktop'
$desktopWork = Join-Path $desktopBuildRoot ('desktop-' + $ReleaseId)
$desktopStage = Join-Path $desktopWork 'HarnessAgent-Desktop'
$desktopDist = Join-Path $desktopWorkspace 'dist'
$desktopUtf8 = New-Object Text.UTF8Encoding($false)
$electronVersion = '44.4.5'
$electronZipName = "electron-v$electronVersion-win32-x64.zip"
$electronBaseUrl = "https://github.com/electron/electron/releases/download/v$electronVersion/"
$electronHash = '11c395820a5aaa8ebcc0686b476d0ac98a730274ebfbdc8cf5538a7c2815cb5d'
$rceditVersion = '5.0.2'
$rceditUrl = 'https://registry.npmjs.org/rcedit/-/rcedit-5.0.2.tgz'
$rceditSha512 = '760cacc5a797678b272e93e39fc695b47bd90c2c7e69172f65b696060975a6853a38fbacb4cbce923f5af59a80490ea2e58e6ccc9d772d2be09443b0ae6814c4'

function Assert-DesktopPath([string]$Path, [string]$Parent) {
    $full = [IO.Path]::GetFullPath($Path)
    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\', '/')
    if (-not $full.StartsWith($parentFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the permitted desktop build directory: $full"
    }
    $cursor = $full
    while ($cursor -and $cursor.Length -ge $parentFull.Length) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Desktop build paths must not traverse reparse points: $cursor"
            }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $full
}

function Write-DesktopUtf8([string]$Path, [string]$Content) {
    [IO.File]::WriteAllText($Path, $Content, $desktopUtf8)
}

function Get-DesktopSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-DesktopDownload([string]$Uri, [string]$Path) {
    $null = Assert-DesktopPath $Path $desktopBuildRoot
    if (Test-Path -LiteralPath $Path) { return }
    if ($Offline) { throw "Offline desktop cache missing: $Path" }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $Uri -OutFile $Path -UseBasicParsing
}

if ($env:OS -ne 'Windows_NT') { throw 'The desktop builder requires Windows.' }
foreach ($target in @($desktopBuildRoot, $desktopCache, $desktopWork, $desktopDist)) {
    $null = Assert-DesktopPath $target $desktopWorkspace
}
if (Test-Path -LiteralPath $desktopWork) { throw "Build already exists; select another -ReleaseId: $desktopWork" }
if (-not $BuildPython) { $BuildPython = Join-Path $desktopWorkspace 'venv\Scripts\python.exe' }
$BuildPython = (Resolve-Path -LiteralPath $BuildPython).Path
& $BuildPython -X utf8 (Join-Path $desktopWorkspace 'tools\build_brand_icon.py')
if ($LASTEXITCODE -ne 0) { throw 'Brand icon generation or pixel verification failed.' }
$desktopSourceFiles = @('package.json', 'main.cjs', 'preload.cjs', 'splash.html', 'splash.css', 'splash.js', 'brand.png')
if (Test-Path -LiteralPath (Join-Path $desktopWorkspace 'desktop\smoke-workspace.cjs') -PathType Leaf) {
    $desktopSourceFiles += 'smoke-workspace.cjs'
}
foreach ($relative in $desktopSourceFiles) {
    $source = Join-Path (Join-Path $desktopWorkspace 'desktop') $relative
    $null = Assert-DesktopPath $source $desktopWorkspace
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Desktop source missing: $source" }
}
$iconSource = Join-Path $desktopWorkspace 'packaging\windows\HarnessAgent.ico'
$null = Assert-DesktopPath $iconSource $desktopWorkspace
if (-not (Test-Path -LiteralPath $iconSource -PathType Leaf)) { throw 'Harness Agent icon is missing.' }
$desktopPackage = Get-Content -LiteralPath (Join-Path $desktopWorkspace 'desktop\package.json') -Raw | ConvertFrom-Json
if ($desktopPackage.main -ne 'main.cjs') { throw 'desktop/package.json must point main to main.cjs.' }
if ($desktopPackage.version -notmatch '^\d+\.\d+\.\d+$') { throw 'Desktop package version must have three numeric components.' }
$appVersion = $desktopPackage.version
$null = New-Item -ItemType Directory -Force -Path $desktopCache, $desktopWork, $desktopDist

$electronZip = Join-Path $desktopCache $electronZipName
$electronChecksums = Join-Path $desktopCache "electron-v$electronVersion-SHASUMS256.txt"
Get-DesktopDownload ($electronBaseUrl + 'SHASUMS256.txt') $electronChecksums
$matchingChecksum = @(Get-Content -LiteralPath $electronChecksums | Where-Object { $_ -match ('^[a-fA-F0-9]{64}\s+\*?' + [regex]::Escape($electronZipName) + '$') })
if ($matchingChecksum.Count -ne 1 -or $matchingChecksum[0].Substring(0, 64).ToLowerInvariant() -ne $electronHash) {
    throw 'Official Electron SHASUMS256 does not match the pinned Windows x64 checksum.'
}
Get-DesktopDownload ($electronBaseUrl + $electronZipName) $electronZip
if ((Get-DesktopSha256 $electronZip) -ne $electronHash) { throw 'Electron ZIP SHA256 mismatch; inspect the preserved cache.' }
$rceditArchive = Join-Path $desktopCache "rcedit-$rceditVersion.tgz"
Get-DesktopDownload $rceditUrl $rceditArchive
if ((Get-FileHash -LiteralPath $rceditArchive -Algorithm SHA512).Hash.ToLowerInvariant() -ne $rceditSha512) {
    throw 'rcedit npm archive SHA512 does not match the pinned npm integrity value.'
}

Write-Host "Building Harness Agent desktop $ReleaseId with Electron $electronVersion"
$coreRelease = 'core-' + $ReleaseId
$coreArgs = @{ BuildPython = $BuildPython; ReleaseId = $coreRelease; RuntimeOnly = $true; Offline = $Offline }
& (Join-Path $PSScriptRoot 'build_windows.ps1') @coreArgs
$coreStage = Join-Path $desktopBuildRoot ("$coreRelease\HarnessAgent")
$null = Assert-DesktopPath $coreStage $desktopBuildRoot
Expand-Archive -LiteralPath $electronZip -DestinationPath $desktopStage
foreach ($directory in @('runtime', 'app')) {
    Copy-Item -LiteralPath (Join-Path $coreStage $directory) -Destination (Join-Path $desktopStage $directory) -Recurse
}
$defaultApp = Join-Path $desktopStage 'resources\default_app.asar'
$null = Assert-DesktopPath $defaultApp $desktopWork
if (Test-Path -LiteralPath $defaultApp) { Remove-Item -LiteralPath $defaultApp -Force }
$desktopApp = Join-Path $desktopStage 'resources\app'
$null = New-Item -ItemType Directory -Force -Path $desktopApp
foreach ($relative in $desktopSourceFiles) {
    Copy-Item -LiteralPath (Join-Path (Join-Path $desktopWorkspace 'desktop') $relative) -Destination (Join-Path $desktopApp $relative)
}
Copy-Item -LiteralPath $iconSource -Destination (Join-Path $desktopStage 'resources\HarnessAgent.ico')
$guide = Join-Path $desktopWorkspace 'docs\local-agent-windows.md'
$null = Assert-DesktopPath $guide $desktopWorkspace
if (Test-Path -LiteralPath $guide -PathType Leaf) { Copy-Item -LiteralPath $guide -Destination (Join-Path $desktopStage 'README-Windows.md') }
$electronExe = Join-Path $desktopStage 'electron.exe'
$desktopExe = Join-Path $desktopStage 'HarnessAgent.exe'
$null = Assert-DesktopPath $electronExe $desktopWork
$null = Assert-DesktopPath $desktopExe $desktopWork
Move-Item -LiteralPath $electronExe -Destination $desktopExe
$electronLicense = Join-Path $desktopStage 'LICENSE'
$null = Assert-DesktopPath $electronLicense $desktopWork
if (Test-Path -LiteralPath $electronLicense) {
    $licenseDestination = Join-Path $desktopStage 'LICENSE-electron.txt'
    $null = Assert-DesktopPath $licenseDestination $desktopWork
    Move-Item -LiteralPath $electronLicense -Destination $licenseDestination
}
Write-DesktopUtf8 (Join-Path $desktopStage 'THIRD-PARTY-NOTICES.txt') @"
Harness Agent Desktop includes separately licensed third-party software.

Electron ${electronVersion}: LICENSE-electron.txt
Chromium and its dependencies: LICENSES.chromium.html
CPython 3.12.10 and its bundled libraries: runtime/LICENSE.txt
Python packages: runtime/Lib/site-packages/*.dist-info (LICENSE / licenses / METADATA)

Original license files are preserved. Package versions and archive checksums are
recorded in build-info.json; individual distributed file hashes are in manifest.json.
"@

# Extract only the known executable from the integrity-checked npm tarball.
$rceditTool = Join-Path $desktopWork 'rcedit-x64.exe'
& $BuildPython -I -c "import pathlib,sys,tarfile; t=tarfile.open(sys.argv[1]); f=t.extractfile('package/bin/rcedit-x64.exe'); assert f is not None; pathlib.Path(sys.argv[2]).write_bytes(f.read())" $rceditArchive $rceditTool
if ($LASTEXITCODE -ne 0) { throw 'Unable to extract rcedit from its verified npm package.' }
& $rceditTool $desktopExe '--set-icon' $iconSource '--set-version-string' 'ProductName' 'Harness Agent' '--set-version-string' 'FileDescription' 'Harness Agent Desktop' '--set-version-string' 'CompanyName' 'Harness Agent' '--set-version-string' 'InternalName' 'HarnessAgent' '--set-version-string' 'OriginalFilename' 'HarnessAgent.exe' '--set-version-string' 'LegalCopyright' 'See bundled third-party licenses' '--set-file-version' $appVersion '--set-product-version' $appVersion
if ($LASTEXITCODE -ne 0) { throw 'Unable to apply the Harness Agent Windows executable branding.' }
$peInfo = (Get-Item -LiteralPath $desktopExe).VersionInfo
if ($peInfo.ProductName -ne 'Harness Agent' -or $peInfo.FileDescription -ne 'Harness Agent Desktop' -or $peInfo.OriginalFilename -ne 'HarnessAgent.exe') {
    throw 'Executable product metadata verification failed.'
}
$previousRunAsNode = $env:ELECTRON_RUN_AS_NODE
try {
    $env:ELECTRON_RUN_AS_NODE = '1'
    $electronVersionOutput = Join-Path $desktopWork 'electron-version.json'
    $electronVersionError = Join-Path $desktopWork 'electron-version.stderr.txt'
    $electronProbe = Start-Process -FilePath $desktopExe -ArgumentList @('-e', 'console.log(JSON.stringify({electron:process.versions.electron,chromium:process.versions.chrome,node:process.versions.node,arch:process.arch,platform:process.platform}))') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $electronVersionOutput -RedirectStandardError $electronVersionError
    if ($electronProbe.ExitCode -ne 0) { throw 'The bundled Electron executable runtime check failed.' }
    $electronVersionJson = Get-Content -LiteralPath $electronVersionOutput -Raw
} finally {
    $env:ELECTRON_RUN_AS_NODE = $previousRunAsNode
}
$electronRuntime = $electronVersionJson | ConvertFrom-Json
if ($electronRuntime.electron -ne $electronVersion -or $electronRuntime.arch -ne 'x64' -or $electronRuntime.platform -ne 'win32') {
    throw 'The bundled Electron runtime has an unexpected version or architecture.'
}

$coreInfo = Get-Content -LiteralPath (Join-Path $coreStage 'build-info.json') -Raw | ConvertFrom-Json
$sourceHashes = @($desktopSourceFiles | ForEach-Object {
    [ordered]@{ path = ('desktop/' + $_); sha256 = Get-DesktopSha256 (Join-Path $desktopApp $_) }
})
$desktopProvenance = [ordered]@{
    schema_version = 1
    product = 'Harness Agent Desktop'
    product_version = $appVersion
    release_id = $ReleaseId
    created_utc = [DateTime]::UtcNow.ToString('o')
    platform = 'windows-x64'
    ui = 'Bundled Electron and Chromium; no external browser or .NET UI dependency.'
    electron = [ordered]@{ version = $electronVersion; chromium = $electronRuntime.chromium; node = $electronRuntime.node; url = ($electronBaseUrl + $electronZipName); sha256 = $electronHash; checksum_source = ($electronBaseUrl + 'SHASUMS256.txt'); selected_from = 'https://registry.npmjs.org/electron/latest'; selected_on = '2026-09-23' }
    rcedit = [ordered]@{ version = $rceditVersion; url = $rceditUrl; sha512 = $rceditSha512; binary_sha256 = Get-DesktopSha256 $rceditTool }
    python = $coreInfo.python
    requirements_sha256 = $coreInfo.requirements_sha256
    dependencies = $coreInfo.dependencies
    git_commit = $coreInfo.git_commit
    git_worktree_dirty = $coreInfo.git_worktree_dirty
    source_selection = $coreInfo.source_selection
    desktop_source_files = $sourceHashes
    build_script_sha256 = Get-DesktopSha256 $PSCommandPath
    core_build_script_sha256 = Get-DesktopSha256 (Join-Path $PSScriptRoot 'build_windows.ps1')
    icon_sha256 = Get-DesktopSha256 $iconSource
    icon_source = 'packaging/defaults/branding/web-app-manifest-512x512.png'
    icon_source_sha256 = Get-DesktopSha256 (Join-Path $desktopWorkspace 'packaging\defaults\branding\web-app-manifest-512x512.png')
    byte_reproducible = $false
    reproducibility_note = 'Electron, Python, dependencies and resource editor are hash-pinned. Build timestamps and edited PE resources are not byte deterministic.'
    verification = @($coreInfo.verification) + @('Electron official archive SHA256', 'rcedit npm SHA512 integrity', 'Windows executable product metadata', 'Bundled Electron executable version and architecture')
}
Write-DesktopUtf8 (Join-Path $desktopStage 'build-info.json') ($desktopProvenance | ConvertTo-Json -Depth 10)
$desktopManifest = @(Get-ChildItem -LiteralPath $desktopStage -Recurse -File | Sort-Object FullName | ForEach-Object {
    [ordered]@{ path = $_.FullName.Substring($desktopStage.Length + 1).Replace('\', '/'); bytes = $_.Length; sha256 = Get-DesktopSha256 $_.FullName }
})
Write-DesktopUtf8 (Join-Path $desktopStage 'manifest.json') (([ordered]@{ schema_version = 1; algorithm = 'SHA256'; files = $desktopManifest }) | ConvertTo-Json -Depth 5)
Write-DesktopUtf8 (Join-Path $desktopStage 'manifest.json.sha256') ((Get-DesktopSha256 (Join-Path $desktopStage 'manifest.json')) + "  manifest.json`n")

$desktopArchive = Join-Path $desktopDist ("HarnessAgent-Desktop-$ReleaseId.zip")
$null = Assert-DesktopPath $desktopArchive $desktopDist
$null = Assert-DesktopPath ($desktopArchive + '.sha256') $desktopDist
if (Test-Path -LiteralPath $desktopArchive) { throw "Archive already exists: $desktopArchive" }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($desktopStage, $desktopArchive, [IO.Compression.CompressionLevel]::Optimal, $true)
$desktopArchiveHash = Get-DesktopSha256 $desktopArchive
Write-DesktopUtf8 ($desktopArchive + '.sha256') ($desktopArchiveHash + '  ' + [IO.Path]::GetFileName($desktopArchive) + "`n")
$desktopPublished = Join-Path $desktopDist 'HarnessAgent-Desktop'
$null = Assert-DesktopPath $desktopPublished $desktopDist
if (Test-Path -LiteralPath $desktopPublished) {
    $desktopPrevious = Join-Path $desktopDist ('HarnessAgent-Desktop.previous-' + $ReleaseId)
    $null = Assert-DesktopPath $desktopPrevious $desktopDist
    if (Test-Path -LiteralPath $desktopPrevious) { throw "Previous release destination exists: $desktopPrevious" }
    Move-Item -LiteralPath $desktopPublished -Destination $desktopPrevious
    Write-Host "Previous desktop release preserved at $desktopPrevious"
}
$null = Assert-DesktopPath $desktopStage $desktopWork
Move-Item -LiteralPath $desktopStage -Destination $desktopPublished
Write-Host "Desktop application: $desktopPublished"
Write-Host "Desktop archive: $desktopArchive"
Write-Host "Desktop archive SHA256: $desktopArchiveHash"
Write-Host ('Desktop archive size: {0:N2} MiB' -f ((Get-Item -LiteralPath $desktopArchive).Length / 1MB))
