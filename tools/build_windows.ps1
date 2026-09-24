[CmdletBinding()]
param(
    [string]$BuildPython = '',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')]
    [string]$ReleaseId = ('windows-x64-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')),
    [switch]$Offline,
    [switch]$RuntimeOnly
)

# Build prerequisites: Windows, x64 CPython 3.12 with pip, .NET Framework 4.x csc.
# The resulting application needs neither a Python install nor pip. Dependency
# installation happens only in this script, outside the embedded interpreter.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildRoot = Join-Path $workspace 'build\windows'
$distRoot = Join-Path $workspace 'dist'
$cacheRoot = Join-Path $buildRoot 'cache'
$workRoot = Join-Path $buildRoot $ReleaseId
$packageRoot = Join-Path $workRoot 'HarnessAgent'
$pythonVersion = '3.12.10'
$pythonArchive = 'python-3.12.10-embed-amd64.zip'
$pythonUrl = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
$pythonHash = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
$pythonHashSource = $pythonUrl + '.spdx.json'
$utf8 = New-Object Text.UTF8Encoding($false)

function Assert-ChildPath([string]$Path, [string]$Parent) {
    $full = [IO.Path]::GetFullPath($Path)
    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\', '/')
    if (-not $full.StartsWith($parentFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the permitted build directory: $full"
    }
    # A junction anywhere in the existing path could escape the intended root.
    $cursor = $full
    while ($cursor -and $cursor.Length -ge $parentFull.Length) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Build paths must not traverse reparse points: $cursor"
            }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $full
}

function Write-Utf8([string]$Path, [string]$Content) {
    [IO.File]::WriteAllText($Path, $Content, $utf8)
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    $oldTemp = $env:TEMP
    $oldTmp = $env:TMP
    try {
        $env:TEMP = Join-Path $workRoot 'temp'
        $env:TMP = $env:TEMP
        & $Program @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $Program"
        }
    } finally {
        $env:TEMP = $oldTemp
        $env:TMP = $oldTmp
    }
}

function Copy-AppTree([string]$RelativePath) {
    $source = Join-Path $workspace $RelativePath
    $null = Assert-ChildPath $source $workspace
    if (-not (Test-Path -LiteralPath $source -PathType Container)) { throw "Missing app directory: $source" }
    $items = @(Get-ChildItem -LiteralPath $source -Recurse -Force)
    foreach ($item in $items) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "App inputs must not contain symlinks or junctions: $($item.FullName)"
        }
        if ($item.PSIsContainer) { continue }
        $relative = $item.FullName.Substring($workspace.Length + 1)
        if ($relative -match '(^|[\\/])(__pycache__|\.git|\.pytest_cache|node_modules|tests?)([\\/]|$)') { continue }
        if ($item.Name -like '.env*' -or $item.Extension -match '^\.(pyc|pyo|log|sqlite|sqlite3|db)$') { continue }
        $destination = Join-Path (Join-Path $packageRoot 'app') $relative
        $null = New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination)
        Copy-Item -LiteralPath $item.FullName -Destination $destination
    }
}

if ($env:OS -ne 'Windows_NT') { throw 'This builder requires Windows.' }
$null = Assert-ChildPath $buildRoot $workspace
$null = Assert-ChildPath $distRoot $workspace
$null = Assert-ChildPath $workRoot $buildRoot
$null = Assert-ChildPath $cacheRoot $buildRoot
if (Test-Path -LiteralPath $workRoot) {
    throw "Build directory already exists; select a new -ReleaseId to preserve it: $workRoot"
}
if (-not $BuildPython) { $BuildPython = Join-Path $workspace 'venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) {
    throw 'Pass -BuildPython with the absolute path of x64 CPython 3.12 with pip.'
}
$BuildPython = (Resolve-Path -LiteralPath $BuildPython).Path
$builderInfoJson = & $BuildPython -I -c "import json,sys,struct; print(json.dumps(dict(version=sys.version,major=sys.version_info.major,minor=sys.version_info.minor,bits=struct.calcsize('P')*8)))"
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect build Python.' }
$builderInfo = $builderInfoJson | ConvertFrom-Json
if ($builderInfo.major -ne 3 -or $builderInfo.minor -ne 12 -or $builderInfo.bits -ne 64) {
    throw 'The dependency build interpreter must be CPython 3.12 x64, matching the embedded ABI.'
}
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not $RuntimeOnly -and -not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework 4.x x64 C# compiler is required.' }
$requiredFiles = @('requirements.lock', 'alembic.ini', 'run.py', 'local_agent.py')
if (-not $RuntimeOnly) { $requiredFiles += @('packaging\windows\Launcher.cs', 'packaging\windows\HarnessAgent.ico', 'packaging\windows\app.manifest') }
foreach ($relative in $requiredFiles) {
    $null = Assert-ChildPath (Join-Path $workspace $relative) $workspace
    if (-not (Test-Path -LiteralPath (Join-Path $workspace $relative) -PathType Leaf)) { throw "Required build input missing: $relative" }
}
$null = New-Item -ItemType Directory -Force -Path $cacheRoot, $workRoot, $distRoot, $packageRoot, (Join-Path $workRoot 'temp')
$runtimeRoot = Join-Path $packageRoot 'runtime'
$sitePackages = Join-Path $runtimeRoot 'Lib\site-packages'
$appRoot = Join-Path $packageRoot 'app'
$null = New-Item -ItemType Directory -Force -Path $runtimeRoot, $appRoot

Write-Host "Building Harness Agent $ReleaseId"
$cachedPython = Join-Path $cacheRoot $pythonArchive
$null = Assert-ChildPath $cachedPython $buildRoot
if (-not (Test-Path -LiteralPath $cachedPython)) {
    if ($Offline) { throw "Offline cache is missing $pythonArchive" }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $pythonUrl -OutFile $cachedPython -UseBasicParsing
}
if ((Get-Sha256 $cachedPython) -ne $pythonHash) {
    throw "Embedded Python SHA256 mismatch. Preserve and inspect the cache file: $cachedPython"
}
Expand-Archive -LiteralPath $cachedPython -DestinationPath $runtimeRoot
Write-Utf8 (Join-Path $runtimeRoot 'python312._pth') "python312.zip`n.`nLib/site-packages`n../app`nimport site`n"

$lockPath = Join-Path $workspace 'requirements.lock'
$lockHash = Get-Sha256 $lockPath
$wheelCache = Join-Path $cacheRoot ('wheels-cp312-win_amd64-' + $lockHash.Substring(0, 16))
$null = Assert-ChildPath $wheelCache $buildRoot
$null = New-Item -ItemType Directory -Force -Path $wheelCache
if (-not $Offline) {
    Invoke-Checked $BuildPython @('-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', '--no-cache-dir', 'download', '--require-hashes', '--only-binary=:all:', '--dest', $wheelCache, '--requirement', $lockPath)
}
$installReport = Join-Path $workRoot 'dependency-install-report.json'
Invoke-Checked $BuildPython @('-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', '--no-cache-dir', 'install', '--no-index', '--find-links', $wheelCache, '--require-hashes', '--only-binary=:all:', '--no-compile', '--no-warn-script-location', '--target', $sitePackages, '--report', $installReport, '--requirement', $lockPath)
# pip generates console-script stubs with the build interpreter's absolute path.
# Runtime code uses imports / python -m; never ship these non-portable stubs.
foreach ($scriptFolder in @('bin', 'Scripts')) {
    $generatedScripts = Join-Path $sitePackages $scriptFolder
    $null = Assert-ChildPath $generatedScripts $workRoot
    if (Test-Path -LiteralPath $generatedScripts) {
        Remove-Item -LiteralPath $generatedScripts -Recurse -Force
    }
}

foreach ($folder in @('backend', 'frontend', 'migrations')) { Copy-AppTree $folder }
if (Test-Path -LiteralPath (Join-Path $workspace 'packaging\defaults')) { Copy-AppTree 'packaging\defaults' }
foreach ($relative in @('alembic.ini', 'run.py', 'local_agent.py', 'requirements.lock', 'requirements.txt', 'README.md', 'RELEASE.md', 'LICENSE', 'LICENSE.md', 'LICENSE.txt')) {
    $source = Join-Path $workspace $relative
    $null = Assert-ChildPath $source $workspace
    if (Test-Path -LiteralPath $source -PathType Leaf) { Copy-Item -LiteralPath $source -Destination (Join-Path $appRoot $relative) }
}
$guide = Join-Path $workspace 'docs\local-agent-windows.md'
$null = Assert-ChildPath $guide $workspace
if (Test-Path -LiteralPath $guide -PathType Leaf) { Copy-Item -LiteralPath $guide -Destination (Join-Path $packageRoot 'README-Windows.md') }

$exe = Join-Path $packageRoot 'HarnessAgent.exe'
if (-not $RuntimeOnly) {
    $compileArgs = @('/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/debug-', '/utf8output', "/out:$exe", '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll', '/reference:System.Core.dll', ('/win32icon:' + (Join-Path $workspace 'packaging\windows\HarnessAgent.ico')), ('/win32manifest:' + (Join-Path $workspace 'packaging\windows\app.manifest')), (Join-Path $workspace 'packaging\windows\Launcher.cs'))
    Invoke-Checked $compiler $compileArgs
}

# Deliberately import dependencies only: building must not initialize live app
# configuration, migrations, credentials, or databases.
$runtimePython = Join-Path $runtimeRoot 'python.exe'
$smokeCode = @'
import importlib, json, pathlib, struct, subprocess, sys
for module in ('fastapi', 'uvicorn', 'sqlalchemy', 'alembic', 'httpx', 'websockets', 'jwt', 'cryptography', 'multipart', 'yaml', 'qrcode', 'pypdf', 'pypdfium2', 'docx', 'openpyxl', 'pptx', 'PIL', 'lxml'):
    importlib.import_module(module)
assert sys.version_info[:3] == (3, 12, 10)
assert struct.calcsize('P') * 8 == 64
assert pathlib.Path(sys.executable).resolve().parent == pathlib.Path(sys.prefix).resolve()
child = subprocess.check_output([sys.executable, '-c', 'import sys; print(sys.version_info[:3])'], text=True).strip()
assert child == '(3, 12, 10)', child
result = subprocess.run([sys.executable, '-m', 'json.tool'], input='{"portable": true}', text=True, capture_output=True, check=True)
assert json.loads(result.stdout)['portable'] is True
print('Embedded runtime dependency imports and -c/-m subprocess checks passed.')
'@
$runtimeSmokePath = Join-Path $workRoot 'runtime-smoke.py'
Write-Utf8 $runtimeSmokePath $smokeCode
Invoke-Checked $runtimePython @('-B', $runtimeSmokePath)

$gitCommit = $null
$gitDirty = $null
if (Get-Command git -ErrorAction SilentlyContinue) {
    $gitCommit = (& git -C $workspace rev-parse HEAD 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0) {
        $gitDirty = [bool]@(& git -C $workspace status --porcelain=v1 --untracked-files=normal 2>$null).Count
    } else { $gitCommit = $null }
}
$dependencies = @((Get-Content -LiteralPath $installReport -Raw | ConvertFrom-Json).install | ForEach-Object {
    [ordered]@{ name = $_.metadata.name; version = $_.metadata.version; archive = [IO.Path]::GetFileName(([Uri]$_.download_info.url).LocalPath); sha256 = $_.download_info.archive_info.hashes.sha256 }
})
$provenance = [ordered]@{
    schema_version = 1
    product = 'Harness Agent'
    release_id = $ReleaseId
    created_utc = [DateTime]::UtcNow.ToString('o')
    platform = 'windows-x64'
    python = [ordered]@{ version = $pythonVersion; url = $pythonUrl; sha256 = $pythonHash; checksum_source = $pythonHashSource }
    requirements_sha256 = $lockHash
    launcher_source_sha256 = $(if (-not $RuntimeOnly) { Get-Sha256 (Join-Path $workspace 'packaging\windows\Launcher.cs') })
    launcher_icon_sha256 = $(if (-not $RuntimeOnly) { Get-Sha256 (Join-Path $workspace 'packaging\windows\HarnessAgent.ico') })
    launcher_manifest_sha256 = $(if (-not $RuntimeOnly) { Get-Sha256 (Join-Path $workspace 'packaging\windows\app.manifest') })
    build_script_sha256 = Get-Sha256 $PSCommandPath
    build_python = $builderInfo.version
    compiler_version = $(if (-not $RuntimeOnly) { (Get-Item -LiteralPath $compiler).VersionInfo.FileVersion })
    git_commit = $gitCommit
    git_worktree_dirty = $gitDirty
    source_selection = 'Current working tree, including untracked files, restricted to the build whitelist.'
    byte_reproducible = $false
    reproducibility_note = 'Python and dependency wheels are hash-pinned. Build timestamps and the .NET Framework compiler output are not byte deterministic.'
    verification = @('Runtime dependency import smoke', 'Embedded Python -c subprocess', 'Embedded Python -m json.tool subprocess')
    dependencies = $dependencies
}
Write-Utf8 (Join-Path $packageRoot 'build-info.json') ($provenance | ConvertTo-Json -Depth 8)
if ($RuntimeOnly) {
    Write-Host "Python and application staging: $packageRoot"
    return
}
$manifest = @(Get-ChildItem -LiteralPath $packageRoot -Recurse -File | Sort-Object FullName | ForEach-Object {
    [ordered]@{ path = $_.FullName.Substring($packageRoot.Length + 1).Replace('\', '/'); bytes = $_.Length; sha256 = Get-Sha256 $_.FullName }
})
Write-Utf8 (Join-Path $packageRoot 'manifest.json') (([ordered]@{ schema_version = 1; algorithm = 'SHA256'; files = $manifest }) | ConvertTo-Json -Depth 5)
Write-Utf8 (Join-Path $packageRoot 'manifest.json.sha256') ((Get-Sha256 (Join-Path $packageRoot 'manifest.json')) + "  manifest.json`n")

$archivePath = Join-Path $distRoot ("HarnessAgent-$ReleaseId.zip")
$null = Assert-ChildPath $archivePath $distRoot
$null = Assert-ChildPath ($archivePath + '.sha256') $distRoot
if (Test-Path -LiteralPath $archivePath) { throw "Archive already exists: $archivePath" }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($packageRoot, $archivePath, [IO.Compression.CompressionLevel]::Optimal, $true)
$archiveHash = Get-Sha256 $archivePath
Write-Utf8 ($archivePath + '.sha256') ($archiveHash + '  ' + [IO.Path]::GetFileName($archivePath) + "`n")

$publishedRoot = Join-Path $distRoot 'HarnessAgent'
$null = Assert-ChildPath $publishedRoot $distRoot
if (Test-Path -LiteralPath $publishedRoot) {
    $oldRoot = Join-Path $distRoot ('HarnessAgent.previous-' + $ReleaseId)
    $null = Assert-ChildPath $oldRoot $distRoot
    if (Test-Path -LiteralPath $oldRoot) { throw "Previous-release destination already exists: $oldRoot" }
    Move-Item -LiteralPath $publishedRoot -Destination $oldRoot
    Write-Host "Previous unpacked release preserved at $oldRoot"
}
$null = Assert-ChildPath $packageRoot $buildRoot
Move-Item -LiteralPath $packageRoot -Destination $publishedRoot
Write-Host "Application: $publishedRoot"
Write-Host "Archive: $archivePath"
Write-Host "Archive SHA256: $archiveHash"
Write-Host ('Archive size: {0:N2} MiB' -f ((Get-Item -LiteralPath $archivePath).Length / 1MB))
