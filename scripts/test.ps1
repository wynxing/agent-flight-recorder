<#
.SYNOPSIS
    一条命令跑完整验证：Python 测试 + 控制台构建 + pi 运行器契约测试。

.DESCRIPTION
    自带依赖引导。**所有依赖都在任何检查开跑之前补齐**：缺少 Python 工作区、
    web/node_modules 或 integrations/pi/node_modules 时，脚本先自行安装并说明补了什么，
    再去跑检查。因此全新克隆可以直接跑，不需要任何手工前置命令；顺序也保证了
    pytest 里的跨语言契约测试不会因为「跑它的时候 pi 依赖还没装」而被跳过。

    结论分三态，脚本最后一行与退出码一致：
      - 全部验证通过：所有检查都真的跑过且通过                    -> 退出码 0
      - 通过，但有未验证项：有检查被跳过或被显式排除，结论不覆盖它们 -> 退出码 0（-Strict 下为 1）
      - 失败：有检查真的跑过但没有通过                            -> 退出码 1

    只要存在任何未验证项，脚本就不会输出「全部验证通过」。

    「未验证」不只来自脚本自己的判断，也来自被调用工具的自我申报：pytest 的跳过同时从
    junit 报告（结构化的 skipped 计数与明细）与 -rs 汇总（明细，留在日志里供人核对）两个来源
    确认，任一来源缺失或两者对不上都算未验证；pi 运行器的 skipped / todo 计数同样登记。
    也就是说「某个测试自己跳过了」不可能被写成「全部验证通过」——跑绿不等于都跑过。

    CI 用 -Strict，因此「CI 绿」等价于「Python、控制台与 pi 运行器都真的跑过」。

.PARAMETER PythonOnly
    只跑 Python 测试。控制台与 pi 运行器会被登记为未验证项，而不是第四种结论：
    默认模式下末行是「通过，但有未验证项」，与 -Strict 组合时判为失败
    （严格模式要求所有检查都真的跑过）。

.PARAMETER Strict
    严格模式：任何未验证项（被跳过的、被显式排除的检查）都直接判为失败（非零退出）。CI 使用此模式。

.PARAMETER SkipInstall
    不做依赖引导：依赖缺失或损坏时不再自动安装，而是记为未验证项。
    用于核对三态结论，或想完全手动控制安装时。

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
$script:tempLogs = [System.Collections.Generic.List[string]]::new()
$script:pythonProbeDetail = ''

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
    param([string]$Name, [string]$Reason = '')
    $entry = if ([string]::IsNullOrWhiteSpace($Reason)) { $Name } else { "$Name（$Reason）" }
    $script:unverified.Add($entry)
    Write-Host ''
    Write-Host "未验证：$entry" -ForegroundColor Yellow
}

function New-TempLog {
    param([string]$Tag, [string]$Extension = 'log')
    $path = Join-Path ([System.IO.Path]::GetTempPath()) ("afr-test-" + $Tag + "-" + [guid]::NewGuid().ToString('n') + "." + $Extension)
    $script:tempLogs.Add($path)
    return $path
}

function Test-BinReady {
    # 就绪判定不能只看 node_modules 目录存在：空目录、半损坏目录也会「存在」，
    # 那样会把结论押在包管理器自愈或全局工具是否碰巧可用上。这里要求真正的二进制在。
    param([string]$BinDir, [string[]]$Names)
    if (-not (Test-Path $BinDir)) { return $false }
    foreach ($name in $Names) {
        $found = $false
        foreach ($candidate in @($name, "$name.cmd", "$name.ps1", "$name.exe")) {
            if (Test-Path (Join-Path $BinDir $candidate)) { $found = $true; break }
        }
        if (-not $found) { return $false }
    }
    return $true
}

function Test-PythonReady {
    # 直接探测工作区包能否导入，而不是只看 .venv 是否存在：uv run 只同步根项目，
    # 工作区成员要靠 uv sync --all-packages 安装。探测输出保留下来供排障。
    $probe = & uv run --quiet python -c 'import agent_flight_recorder, afr_server' 2>&1
    $script:pythonProbeDetail = (($probe | Out-String).Trim())
    return ($LASTEXITCODE -eq 0)
}

function Add-PytestSkips {
    # 跑绿不等于「都跑过」：pytest 会自己跳过没有依赖的测试。这里读两个独立来源，
    # 不让结论押在任一来源的格式上：
    #   1. junit 报告（结构化：testsuite 的 skipped 计数 + 每个 testcase 的 <skipped>）——判定依据；
    #   2. 控制台 -rs 汇总——明细与交叉验证，同时留在日志里供人核对。
    # 任一来源缺失、解析失败或两者对不上，都登记为未验证项：这一层的失败方向只能是更保守。
    param([string]$ConsoleLog, [string]$JunitPath)

    $xmlSkipped = $null
    $xmlDetailed = 0
    if (-not (Test-Path $JunitPath)) {
        Add-Unverified 'pytest 的跳过情况' 'pytest 没有生成 junit 报告，无法确认是否有测试被跳过'
    } else {
        try {
            [xml]$report = Get-Content -LiteralPath $JunitPath -Raw
            $suites = @($report.testsuites.testsuite)
            if ($suites.Count -eq 0) { throw '报告里没有 testsuite 节点' }
            $xmlSkipped = 0
            foreach ($suite in $suites) { $xmlSkipped += [int]$suite.skipped }
            foreach ($suite in $suites) {
                foreach ($case in @($suite.testcase)) {
                    if ($null -eq $case -or $null -eq $case.skipped) { continue }
                    $xmlDetailed++
                    $where = ([string]$case.classname).Trim()
                    if ($where) { $where = $where + '::' + ([string]$case.name).Trim() } else { $where = ([string]$case.name).Trim() }
                    $why = ([string]$case.skipped.message).Trim()
                    if (-not $why) { $why = '未给出跳过原因' }
                    Add-Unverified "pytest 跳过：$where" $why
                }
            }
            if ($xmlSkipped -ne $xmlDetailed) {
                Add-Unverified 'pytest 的跳过情况' "junit 报告记了 $xmlSkipped 处跳过，但只解析出 $xmlDetailed 处明细"
            }
        } catch {
            $xmlSkipped = $null
            Add-Unverified 'pytest 的跳过情况' "pytest 的 junit 报告无法解析：$($_.Exception.Message)"
        }
    }

    if (-not (Test-Path $ConsoleLog)) {
        Add-Unverified 'pytest 的跳过情况' '无法读取 pytest 控制台输出，无法确认是否有测试被跳过'
        return
    }
    $lines = @(Get-Content -LiteralPath $ConsoleLog)
    $declared = -1
    foreach ($line in $lines) {
        if ($line -match '(\d+) skipped') { $declared = [int]$Matches[1]; break }
    }
    # pytest 不打印 "0 skipped"，没有计数行就说明跳过数为 0。
    if ($declared -lt 0) { $declared = 0 }
    if (-not ($lines | Where-Object { $_ -match '\d+ passed|\d+ failed|\d+ error|no tests ran' })) {
        Add-Unverified 'pytest 的跳过情况' '未在控制台输出中找到 pytest 汇总行，无法确认是否有测试被跳过'
    }

    if ($null -eq $xmlSkipped) {
        # junit 不可用时退回控制台明细，仍然不让「跳过」无声通过。
        foreach ($line in $lines) {
            if ($line -match '^SKIPPED \[(\d+)\]\s+(.+?):(\d+):\s*(.*)$') {
                $why = $Matches[4]
                if ([string]::IsNullOrWhiteSpace($why)) { $why = '未给出跳过原因' }
                Add-Unverified "pytest 跳过：$($Matches[2]):$($Matches[3])" $why
            } elseif ($line -match '^SKIPPED \[(\d+)\]\s+(.*)$') {
                Add-Unverified 'pytest 跳过' $Matches[2]
            }
        }
        return
    }

    if ($declared -ne $xmlSkipped) {
        Add-Unverified 'pytest 的跳过情况' "控制台汇总记 $declared 处跳过，junit 报告记 $xmlSkipped 处，两者不一致"
    }
}

function Add-NodeSkips {
    param([string]$LogPath, [string]$Requirement)
    if (-not (Test-Path $LogPath)) {
        Add-Unverified $Requirement '无法读取 pi 运行器输出，无法确认是否有测试被跳过'
        return
    }
    $text = Get-Content -LiteralPath $LogPath -Raw
    # 取最后一处匹配：计数出现在输出末尾的汇总里，靠后的匹配比测试自己打印的文本更可信。
    $skippedMatches = [regex]::Matches($text, 'skipped\s+(\d+)')
    $todoMatches = [regex]::Matches($text, 'todo\s+(\d+)')
    if ($skippedMatches.Count -eq 0 -or $todoMatches.Count -eq 0) {
        Add-Unverified $Requirement 'pi 运行器输出中没有找到 skipped / todo 计数，无法确认是否有测试被跳过'
        return
    }
    $skipped = [int]$skippedMatches[$skippedMatches.Count - 1].Groups[1].Value
    $todo = [int]$todoMatches[$todoMatches.Count - 1].Groups[1].Value
    if ($skipped -gt 0) {
        Add-Unverified $Requirement "node --test 报告 $skipped 个测试被跳过"
    }
    if ($todo -gt 0) {
        Add-Unverified $Requirement "node --test 报告 $todo 个 todo，即声明了但没有实现的检查"
    }
}

# ================================================================ 第一阶段：依赖引导
# 顺序是本脚本的要害：所有引导必须在这里完成，之后才允许任何检查开跑。
# pytest 里的跨语言契约测试会在 integrations/pi 依赖缺失时自行 skip，若先跑 pytest，
# 全新克隆上它会被静默跳过，而脚本却会打印「全部验证通过」。

# ---------------------------------------------------------------- Python 工作区
$pythonState = 'unavailable'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Add-Unverified 'Python 测试' '未找到 uv，无法安装或运行 Python 依赖（https://docs.astral.sh/uv/）'
} elseif (Test-PythonReady) {
    $pythonState = 'ready'
} elseif ($SkipInstall) {
    Add-Unverified 'Python 测试' '工作区依赖未安装或不可导入，且 -SkipInstall 禁止自动补齐'
    if ($script:pythonProbeDetail) {
        Write-Host "导入探测输出：$($script:pythonProbeDetail)" -ForegroundColor DarkGray
    }
} else {
    Write-Host 'Python 工作区依赖未就绪，执行 uv sync --all-packages ...' -ForegroundColor Cyan
    if ($script:pythonProbeDetail) {
        Write-Host "导入探测输出：$($script:pythonProbeDetail)" -ForegroundColor DarkGray
    }
    uv sync --all-packages
    if ($LASTEXITCODE -ne 0) {
        Add-Failure 'Python 测试：依赖引导（uv sync --all-packages）失败'
    } else {
        Write-Bootstrap 'Python 工作区依赖（uv sync --all-packages）'
        if (Test-PythonReady) {
            $pythonState = 'ready'
        } else {
            Add-Failure "Python 测试：依赖引导后仍无法导入 agent_flight_recorder / afr_server。导入探测输出：$($script:pythonProbeDetail)"
        }
    }
}

# ---------------------------------------------------------------- 控制台依赖
$webState = 'unavailable'
$webRunner = $null
$webDir = Join-Path $root 'web'
$webBin = Join-Path $webDir 'node_modules/.bin'

if ($PythonOnly) {
    $webState = 'excluded'
    Add-Unverified '控制台构建与类型检查' '由 -PythonOnly 显式排除，本次未运行'
} elseif (-not (Test-Path $webDir)) {
    Add-Unverified '控制台构建与类型检查' '仓库中没有 web/ 目录'
} elseif (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Add-Unverified '控制台构建与类型检查' '未找到 node（https://nodejs.org/）'
} else {
    $webRunner = if (Get-Command pnpm -ErrorAction SilentlyContinue) { 'pnpm' } elseif (Get-Command npm -ErrorAction SilentlyContinue) { 'npm' } else { $null }
    if (-not $webRunner) {
        Add-Unverified '控制台构建与类型检查' '未找到 pnpm 或 npm'
    } elseif (Test-BinReady -BinDir $webBin -Names @('vite', 'vue-tsc')) {
        $webState = 'ready'
    } elseif ($SkipInstall) {
        Add-Unverified '控制台构建与类型检查' 'web/node_modules 缺失或缺少 vite / vue-tsc，且 -SkipInstall 禁止自动补齐'
    } else {
        Write-Host "控制台依赖缺失或不完整，执行 $webRunner install ..." -ForegroundColor Cyan
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
        } elseif (Test-BinReady -BinDir $webBin -Names @('vite', 'vue-tsc')) {
            Write-Bootstrap "控制台依赖（$webRunner install，web/node_modules）"
            $webState = 'ready'
        } else {
            Add-Failure '控制台：依赖引导后 web/node_modules/.bin 里仍缺少 vite 或 vue-tsc（该目录疑似损坏；安装不会补齐已丢失的 shim，请删除 web/node_modules 后重跑）'
        }
    }
}

# ---------------------------------------------------------------- pi 运行器依赖
$piState = 'unavailable'
$piDir = Join-Path $root 'integrations/pi'
$piBin = Join-Path $piDir 'node_modules/.bin'
$piTsxPackage = Join-Path $piDir 'node_modules/tsx'

# 这里与 server/tests/test_pi_protocol.py 的守卫条件保持一致：
# 该测试要求 node_modules/tsx 存在且 node 可用。
$piReady = { (Test-Path $piTsxPackage) -and (Test-BinReady -BinDir $piBin -Names @('tsx', 'tsc')) }

if ($PythonOnly) {
    $piState = 'excluded'
    Add-Unverified 'pi 运行器（typecheck + 契约测试）' '由 -PythonOnly 显式排除，本次未运行'
} elseif (-not (Test-Path $piDir)) {
    Add-Unverified 'pi 运行器（typecheck + 契约测试）' '仓库中没有 integrations/pi 目录'
} elseif (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Add-Unverified 'pi 运行器（typecheck + 契约测试）' '未找到 npm（https://nodejs.org/）'
} elseif (& $piReady) {
    $piState = 'ready'
} elseif ($SkipInstall) {
    Add-Unverified 'pi 运行器（typecheck + 契约测试）' 'integrations/pi/node_modules 缺失或缺少 tsx / tsc，且 -SkipInstall 禁止自动补齐'
} else {
    Write-Host 'pi 运行器依赖缺失或不完整，执行 npm ci --ignore-scripts ...' -ForegroundColor Cyan
    Push-Location $piDir
    try {
        & npm ci --ignore-scripts --no-audit --no-fund
        $installCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($installCode -ne 0) {
        Add-Failure 'pi 运行器：依赖安装失败'
    } elseif (& $piReady) {
        Write-Bootstrap 'pi 运行器依赖（npm ci --ignore-scripts，integrations/pi/node_modules）'
        $piState = 'ready'
    } else {
        Add-Failure 'pi 运行器：依赖引导后 integrations/pi/node_modules 里仍缺少 tsx 或 tsc（请删除该目录后重跑）'
    }
}

# ================================================================ 第二阶段：检查

# ---------------------------------------------------------------- Python 测试
if ($pythonState -eq 'ready') {
    Write-Section 'Python 测试'
    $pytestLog = New-TempLog 'pytest'
    $pytestJunit = New-TempLog 'pytest-junit' 'xml'
    # -rs 把每一处 skip 与原因打进控制台日志供人核对；--junitxml 再给一份结构化的
    # 跳过计数与明细。两个来源由 Add-PytestSkips 交叉验证，任一缺失或对不上都登记为未验证项。
    & uv run pytest -rs "--junitxml=$pytestJunit" | Tee-Object -FilePath $pytestLog
    if ($LASTEXITCODE -ne 0) { Add-Failure 'Python 测试：pytest 未通过' }
    Add-PytestSkips -ConsoleLog $pytestLog -JunitPath $pytestJunit
}

# ---------------------------------------------------------------- 控制台构建
if ($webState -eq 'ready') {
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
if ($piState -eq 'ready') {
    Write-Section 'pi 运行器'
    $piLog = New-TempLog 'pi'
    Push-Location $piDir
    try {
        & npm run typecheck | Tee-Object -FilePath $piLog
        $piTypecheckOk = ($LASTEXITCODE -eq 0)
        if (-not $piTypecheckOk) { Add-Failure 'pi 运行器：类型检查失败' }
        if ($piTypecheckOk) {
            & npm test | Tee-Object -FilePath $piLog -Append
            if ($LASTEXITCODE -ne 0) { Add-Failure 'pi 运行器：契约测试失败' }
            Add-NodeSkips -LogPath $piLog -Requirement 'pi 运行器的跳过情况'
        }
    } finally {
        Pop-Location
    }
}

# ================================================================ 第三阶段：结论
Write-Host ''
$exitCode = 0
if ($script:failures.Count -gt 0) {
    Write-Host '失败项：' -ForegroundColor Red
    foreach ($item in $script:failures) { Write-Host "  - $item" -ForegroundColor Red }
    if ($script:unverified.Count -gt 0) {
        Write-Host '另有未验证项：' -ForegroundColor Yellow
        foreach ($item in $script:unverified) { Write-Host "  - $item" -ForegroundColor Yellow }
    }
    Write-Host '失败。' -ForegroundColor Red
    $exitCode = 1
} elseif ($script:unverified.Count -gt 0) {
    Write-Host '未验证项：' -ForegroundColor Yellow
    foreach ($item in $script:unverified) { Write-Host "  - $item" -ForegroundColor Yellow }
    if ($Strict) {
        Write-Host '严格模式：存在未验证项，判为失败。' -ForegroundColor Red
        $exitCode = 1
    } else {
        Write-Host '通过，但有未验证项。' -ForegroundColor Yellow
    }
} else {
    Write-Host '全部验证通过。' -ForegroundColor Green
}

foreach ($log in $script:tempLogs) {
    if (Test-Path -LiteralPath $log) { Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue }
}
exit $exitCode
