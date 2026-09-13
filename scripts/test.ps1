<#
.SYNOPSIS
    跑完整验证：Python 测试 + 控制台构建。

.EXAMPLE
    pwsh scripts/test.ps1
    pwsh scripts/test.ps1 -PythonOnly
#>

[CmdletBinding()]
param(
    [switch]$PythonOnly
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host '== Python 测试 ==' -ForegroundColor Cyan
uv run pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Python 测试失败。' }

if ($PythonOnly) {
    Write-Host '通过。' -ForegroundColor Green
    return
}

$webDir = Join-Path $root 'web'
if (-not (Test-Path $webDir)) { return }

Write-Host ''
Write-Host '== 控制台构建 ==' -ForegroundColor Cyan
Push-Location $webDir
try {
    $npm = if (Get-Command pnpm -ErrorAction SilentlyContinue) { 'pnpm' } else { 'npm' }
    & $npm run build
    if ($LASTEXITCODE -ne 0) { throw '控制台构建失败。' }
    & $npm run typecheck
    if ($LASTEXITCODE -ne 0) { throw '控制台类型检查失败。' }
} finally {
    Pop-Location
}

$piDir = Join-Path $root 'integrations/pi'
if (Test-Path $piDir) {
    if (-not (Test-Path (Join-Path $piDir 'node_modules'))) {
        Write-Host ''
        Write-Host '跳过 pi 运行器测试：先在 integrations/pi 执行 npm ci --ignore-scripts。' -ForegroundColor Yellow
    } else {
        Write-Host ''
        Write-Host '== pi 运行器 ==' -ForegroundColor Cyan
        Push-Location $piDir
        try {
            npm run typecheck
            if ($LASTEXITCODE -ne 0) { throw 'pi 类型检查失败。' }
            npm test
            if ($LASTEXITCODE -ne 0) { throw 'pi 契约测试失败。' }
        } finally { Pop-Location }
    }
}

Write-Host ''
Write-Host '全部通过。' -ForegroundColor Green

