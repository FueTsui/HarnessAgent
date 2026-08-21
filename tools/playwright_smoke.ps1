param(
  [int]$Port = 8765,
  [string]$Python = "python",
  [string]$RootPassword = "Browser-Smoke-Root-2026!",
  [string]$PlaywrightCliPackage = "@playwright/cli@0.1.18"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$artifactRoot = Join-Path $repoRoot "output\playwright\windows-smoke"
$tempBase = Join-Path $repoRoot "tmp"
$dataDir = Join-Path $tempBase ("browser-smoke-" + [guid]::NewGuid().ToString("N"))
$session = "agent-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 10)
$baseUrl = "http://127.0.0.1:$Port"
$server = $null

New-Item -ItemType Directory -Force -Path $artifactRoot, $dataDir | Out-Null

function Invoke-Pw {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
  $result = & npx --yes --package $PlaywrightCliPackage playwright-cli --session $session @Arguments 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0) { throw "playwright-cli failed: $result" }
  return $result
}

function Find-Ref([string]$Snapshot, [string]$Role, [string]$Name) {
  $pattern = '-\s+' + [regex]::Escape($Role) + '\s+"' + [regex]::Escape($Name) + '"[^\r\n]*\[ref=([A-Za-z0-9]+)\]'
  $match = [regex]::Match($Snapshot, $pattern)
  if (-not $match.Success) { throw "Element ref not found: $Role / $Name`n$Snapshot" }
  return $match.Groups[1].Value
}

try {
  $env:APP_HOST = "127.0.0.1"
  $env:APP_PORT = [string]$Port
  $env:APP_DATA_DIR = $dataDir
  $env:JWT_SECRET = "browser-smoke-jwt-secret-with-at-least-32-bytes"
  $env:SECRET_MASTER_KEY = "browser-smoke-envelope-key-with-at-least-32-bytes"
  $env:ROOT_PASSWORD = $RootPassword
  $env:JOB_WORKER_ENABLED = "false"
  $env:CRON_SCHEDULER_ENABLED = "false"
  $env:ALLOW_INSECURE_DEFAULTS = "true"

  $server = Start-Process -FilePath $Python -ArgumentList "run.py" -WorkingDirectory $repoRoot -PassThru -WindowStyle Hidden
  $healthy = $false
  for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
      $healthResponse = Invoke-WebRequest -Uri "$baseUrl/healthz" -TimeoutSec 2 -SkipHttpErrorCheck
      $health = $healthResponse.Content | ConvertFrom-Json
      if ($health.database.ok -and $health.migration.ok -and $health.migration.current -eq $health.migration.expected) { $healthy = $true; break }
    } catch {}
    if ($server.HasExited) { throw "Service exited before becoming healthy" }
    Start-Sleep -Milliseconds 500
  }
  if (-not $healthy) { throw "Service did not become healthy at $baseUrl" }

  & npx --yes --package $PlaywrightCliPackage playwright-cli install-browser chromium | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Chromium installation failed" }
  Push-Location $artifactRoot
  try {
    Invoke-Pw @("open", "$baseUrl/login") | Out-Null
    $loginSnapshot = Invoke-Pw @("snapshot")
    $loginSnapshot | Set-Content -Encoding UTF8 "01-login-snapshot.txt"
    $usernameRef = Find-Ref $loginSnapshot "textbox" "请输入用户名"
    $passwordRef = Find-Ref $loginSnapshot "textbox" "请输入密码"
    $loginRef = Find-Ref $loginSnapshot "button" "登录工作台"
    Invoke-Pw @("fill", $usernameRef, "root") | Out-Null
    Invoke-Pw @("fill", $passwordRef, $RootPassword) | Out-Null
    Invoke-Pw @("click", $loginRef) | Out-Null
    Start-Sleep -Milliseconds 800
    $chatSnapshot = Invoke-Pw @("snapshot")
    $chatSnapshot | Set-Content -Encoding UTF8 "02-chat-snapshot.txt"
    if ($chatSnapshot -notmatch "今天想完成什么|新对话") { throw "Chat workspace did not render after login" }

    Invoke-Pw @("eval", "location.href='/admin'") | Out-Null
    Start-Sleep -Milliseconds 800
    $adminSnapshot = Invoke-Pw @("snapshot")
    $adminSnapshot | Set-Content -Encoding UTF8 "03-admin-snapshot.txt"
    $operationsRef = Find-Ref $adminSnapshot "button" "运行中心"
    Invoke-Pw @("click", $operationsRef) | Out-Null
    Start-Sleep -Milliseconds 800
    $operationsSnapshot = Invoke-Pw @("snapshot")
    $operationsSnapshot | Set-Content -Encoding UTF8 "04-operations-snapshot.txt"
    if ($operationsSnapshot -notmatch "失败与死信任务" -or $operationsSnapshot -notmatch "活跃 Worker") {
      throw "Operations center did not render expected runtime sections"
    }
    Invoke-Pw @("screenshot") | Out-Null
    $console = Invoke-Pw @("console", "error")
    $console | Set-Content -Encoding UTF8 "05-console-errors.txt"
    if ($console -match "TypeError|ReferenceError|SyntaxError") { throw "Browser console contains JavaScript errors: $console" }
  } finally {
    try { Invoke-Pw @("close") | Out-Null } catch {}
    Pop-Location
  }
  Write-Host "Playwright smoke passed. Evidence: $artifactRoot"
} finally {
  if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force }
  $resolvedTemp = (Resolve-Path $tempBase).Path.TrimEnd('\') + '\'
  $resolvedData = (Resolve-Path $dataDir -ErrorAction SilentlyContinue)?.Path
  if ($resolvedData -and $resolvedData.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
    Remove-Item -LiteralPath $resolvedData -Recurse -Force
  }
}
