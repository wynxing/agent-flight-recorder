<#
.SYNOPSIS
    一条命令跑完整验证：Python 测试 + 控制台构建 + pi 运行器契约测试。

.DESCRIPTION
    自带依赖引导：缺少 .venv、web/node_modules 或 integrations/pi/node_modules 时，
    脚本会自行补齐，并在输出里说明补了什么。因此全新克隆可以直接跑，不需要任何手工前置命令。

    结论分三态，脚本最后一行与退出码一致：
      - 全部验证通过：所有检查都真的跑过且通过                 -> 退出码 0
      - 通过，但有未验证项：有检查被跳过，结论不覆盖它们       -> 退出码 0（-Strict 下为 1）
      - 失败：有检查真的跑过但没有通过                         -> 退出码 1

    只要存在任何跳过，脚本就不会输出「全部验证通过」。CI 用 -Strict，
    因此「CI 绿」等价于「Python、控制台与 pi 运行器都真的跑过」。

.PARAMETER PythonOnly
    只跑 Python 测试。此时结论只覆盖 Python，最后一行会明确标注，
    不会声称「全部验证通过」。

.PARAMETER Strict
    严格模式：任何跳过（未验证项）都直接判为失败（非零退出）。CI 使用此模式。

.PARAMETER SkipInstall
    不做依赖引导：缺少依赖时不再自动安装，而是记为未验证项。
    用于在依赖缺失的机器上核对三态结论，或想完全手动控制安装时。

.EXAMPLE
    pwsh scripts/test.ps1
    pwsh scripts/test.ps1 -Strict
    pwsh scripts/test.ps1 -SkipInstall
    pwsh scripts/test.ps1 -PythonOnly
#>

[CmdletBinding()]
param(
    [switch]$PythonOnly,
    [switch]$Strict,
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 结论收集：真实失败与未验证项分开记，最后据此决定三态与退出码。
$script:failures = [System.Collections.Generic.List[string]]::new()
$script:unverified = [System.Collections.Generic.List[string]]::new()

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host "== $Title ==" -ForegroundColor Cyan
}

function Write-Bootstrap {
    param([string]$What)
    Write-Host "补齐依赖：$What" -ForegroundColor DarkCyan
}

function Add-Failure {
    param([string]$Message)
    $script:failures.Add($Message)
    Write-Host "失败：$Message" -ForegroundColor Red
}

function Add-Unverified {
    param([string]$Name, [string]$Reason)
    $script:unverified.Add("$Name（$Reason）")
    Write-Host ''
    Write-Host "跳过 $Name：$Reason" -ForegroundColor Yellow
}

function Test-PythonReady {
    # 直接探测工作区包能否导入，而不是只看 .venv 是否存在：
    # uv run 只同步根项目，工作区成员要靠 uv sync --all-packages 安装。
    & uv run --quiet python -c 'import agent_flight_recorder, afr_server' *> $null
    return ($LASTEXITCODE -eq 0)
}

# ---------------------------------------------------------------- Python 测试
$pythonUsable = $false
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Add-Unverified 'Python 测试' '未找到 uv，无法安装或运行 Python 依赖（https://docs.astral.sh/uv/）'
} elseif (Test-PythonReady) {
    $pythonUsable = $true
} elseif ($SkipInstall) {
    Add-Unverified 'Python 测试' '工作区依赖未安装，且 -SkipInstall 禁止自动补齐'
} else {
    Write-Host 'Python 工作区依赖未就绪，执行 uv sync --all-packages ...' -ForegroundColor Cyan
    uv sync --all-packages
    if ($LASTEXITCODE -ne 0) {
        Add-Failure 'Python 测试：依赖引导（uv sync --all-packages）失败'
    } else {
        Write-Bootstrap 'Python 工作区依赖（uv sync --all-packages）'
        if (Test-PythonReady) {
            $pythonUsable = $true
        } else {
            Add-Failure 'Python 测试：依赖引导后仍无法导入 agent_flight_recorder / afr_server'
        }
    }
}

if ($pythonUsable) {
    Write-Section 'Python 测试'
    uv run pytest
    if ($LASTEXITCODE -ne 0) { Add-Failure 'Python 测试：pytest 未通过' }
}

# ---------------------------------------------------------------- 控制台构建
$webState = 'skip'
$webSkipReason = $null
$webRunner = $null
$webDir = Join-Path $root 'web'

if ($PythonOnly) {
    $webState = 'excluded'
} elseif (-not (Test-Path $webDir)) {
    $webSkipReason = '仓库中没有 web/ 目录'
} elseif (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    $webSkipReason = '未找到 node（https://nodejs.org/）'
} else {
    $webRunner = if (Get-Command pnpm -ErrorAction SilentlyContinue) { 'pnpm' } elseif (Get-Command npm -ErrorAction SilentlyContinue) { 'npm' } else { $null }
    if (-not $webRunner) {
        $webSkipReason = '未找到 pnpm 或 npm'
    } elseif (Test-Path (Join-Path $webDir 'node_modules')) {
        $webState = 'ready'
    } elseif ($SkipInstall) {
        $webSkipReason = 'web/node_modules 缺失，且 -SkipInstall 禁止自动补齐'
    } else {
        Write-Host "控制台依赖缺失，执行 $webRunner install ..." -ForegroundColor Cyan
        Push-Location $webDir
        try {
            if ($webRunner -eq 'pnpm' -and (Test-Path (Join-Path $webDir 'pnpm-lock.yaml'))) {
                & pnpm install --frozen-lockfile
            } else {
                & $webRunner install
            }
            $installCode = $LASTEXITCODE
        } finally {
            Pop-Location
        }
        if ($installCode -ne 0) {
            Add-Failure '控制台：依赖安装失败'
            $webState = 'failed'
        } else {
            Write-Bootstrap "控制台依赖（$webRunner install，web/node_modules）"
            $webState = 'ready'
        }
    }
}

if ($webState -eq 'skip') {
    Add-Unverified '控制台构建与类型检查' $webSkipReason
} elseif ($webState -eq 'ready') {
    Write-Section '控制台构建'
    Push-Location $webDir
    try {
        & $webRunner run build
        if ($LASTEXITCODE -ne 0) {
            Add-Failure '控制台：构建失败'
        } else {
            & $webRunner run typecheck
            if ($LASTEXITCODE -ne 0) { Add-Failure '控制台：类型检查失败' }
        }
    } finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------- pi 运行器
$piState = 'skip'
$piSkipReason = $null
$piDir = Join-Path $root 'integrations/pi'

if ($PythonOnly) {
    $piState = 'excluded'
} elseif (-not (Test-Path $piDir)) {
    $piSkipReason = '仓库中没有 integrations/pi 目录'
} elseif (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    $piSkipReason = '未找到 npm（https://nodejs.org/）'
} elseif (Test-Path (Join-Path $piDir 'node_modules')) {
    $piState = 'ready'
} elseif ($SkipInstall) {
    $piSkipReason = 'integrations/pi/node_modules 缺失，且 -SkipInstall 禁止自动补齐'
} else {
    Write-Host 'pi 运行器依赖缺失，执行 npm ci --ignore-scripts ...' -ForegroundColor Cyan
    Push-Location $piDir
    try {
        & npm ci --ignore-scripts --no-audit --no-fund
        $installCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($installCode -ne 0) {
        Add-Failure 'pi 运行器：依赖安装失败'
        $piState = 'failed'
    } else {
        Write-Bootstrap 'pi 运行器依赖（npm ci --ignore-scripts，integrations/pi/node_modules）'
        $piState = 'ready'
    }
}

if ($piState -eq 'skip') {
    Add-Unverified 'pi 运行器（typecheck + 契约测试）' $piSkipReason
} elseif ($piState -eq 'ready') {
    Write-Section 'pi 运行器'
    Push-Location $piDir
    try {
        & npm run typecheck
        if ($LASTEXITCODE -ne 0) {
            Add-Failure 'pi 运行器：类型检查失败'
        } else {
            & npm test
            if ($LASTEXITCODE -ne 0) { Add-Failure 'pi 运行器：契约测试失败' }
        }
    } finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------- 结论
Write-Host ''
if ($script:failures.Count -gt 0) {
    Write-Host '失败项：' -ForegroundColor Red
    foreach ($item in $script:failures) { Write-Host "  - $item" -ForegroundColor Red }
    if ($script:unverified.Count -gt 0) {
        Write-Host '另有未验证项：' -ForegroundColor Yellow
        foreach ($item in $script:unverified) { Write-Host "  - $item" -ForegroundColor Yellow }
    }
    Write-Host '失败。' -ForegroundColor Red
    exit 1
}

if ($script:unverified.Count -gt 0) {
    Write-Host '未验证项：' -ForegroundColor Yellow
    foreach ($item in $script:unverified) { Write-Host "  - $item" -ForegroundColor Yellow }
    if ($Strict) {
        Write-Host '严格模式：存在未验证项，判为失败。' -ForegroundColor Red
        exit 1
    }
    Write-Host '通过，但有未验证项。' -ForegroundColor Yellow
    exit 0
}

if ($PythonOnly) {
    Write-Host '仅 Python 测试通过（-PythonOnly：控制台与 pi 运行器未验证）。' -ForegroundColor Green
    exit 0
}

Write-Host '全部验证通过。' -ForegroundColor Green
exit 0

