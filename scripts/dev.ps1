<#
.SYNOPSIS
    一条命令拉起本地开发环境：服务端 + 控制台。

.EXAMPLE
    pwsh scripts/dev.ps1
    pwsh scripts/dev.ps1 -NoWeb        # 只跑服务端
#>

[CmdletBinding()]
param(
    [switch]$NoWeb,
    [switch]$SkipSync
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Assert-Command {
    param([string]$Name, [string]$Hint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "找不到 $Name。$Hint"
    }
}

function Resolve-Runner {
    # Start-Process 需要真正的可执行文件。Node 在 Windows 上同时提供无扩展名的 shell
    # 脚本与 .cmd 包装，把前者交给 Start-Process 会报“不是有效的 Win32 应用程序”，
    # 因此这里显式优先取 .cmd。
    foreach ($candidate in @('pnpm.cmd', 'npm.cmd')) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    throw '找不到 npm 或 pnpm。请先安装 Node.js。'
}

Assert-Command 'uv' '请先安装 uv: https://docs.astral.sh/uv/'

if (-not $SkipSync) {
    Write-Host '同步 Python 依赖...' -ForegroundColor Cyan
    uv sync --all-packages
}

$webDir = Join-Path $root 'web'
$hasWeb = (Test-Path $webDir) -and (-not $NoWeb)

if ($hasWeb) {
    Assert-Command 'node' '请先安装 Node.js 20 以上版本。'
    if (-not (Test-Path (Join-Path $webDir 'node_modules'))) {
        Write-Host '安装控制台依赖...' -ForegroundColor Cyan
        Push-Location $webDir
        try {
            if (Get-Command pnpm -ErrorAction SilentlyContinue) { pnpm install } else { npm install }
        } finally {
            Pop-Location
        }
    }
}

Write-Host ''
Write-Host '服务端 : http://127.0.0.1:7710' -ForegroundColor Green
if ($hasWeb) {
    Write-Host '控制台 : http://127.0.0.1:5273  (Vite 把 /v1 代理到服务端)' -ForegroundColor Green
} else {
    Write-Host '控制台 : 未启动（页面由服务端托管已构建的静态资源）' -ForegroundColor DarkGray
}
Write-Host '按 Ctrl+C 结束，两个进程的日志都输出在当前终端。' -ForegroundColor DarkGray
Write-Host ''

$uvPath = (Get-Command uv).Source
$serverArgs = @('run', 'python', '-m', 'afr_server')
$server = Start-Process -FilePath $uvPath -ArgumentList $serverArgs -WorkingDirectory $root -NoNewWindow -PassThru

$console = $null
if ($hasWeb) {
    $console = Start-Process -FilePath (Resolve-Runner) -ArgumentList @('run', 'dev') -WorkingDirectory $webDir -NoNewWindow -PassThru
}

try {
    while ($true) {
        Start-Sleep -Seconds 1
        if ($server.HasExited) {
            Write-Warning '服务端已退出。'
            break
        }
        if ($console -and $console.HasExited) {
            Write-Warning '控制台已退出（服务端仍在运行）。'
            break
        }
    }
} finally {
    foreach ($proc in @($console, $server)) {
        if ($proc -and (-not $proc.HasExited)) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

