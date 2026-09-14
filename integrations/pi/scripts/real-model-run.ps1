<#
.SYNOPSIS
    用真实模型跑一次「真实失败任务集」的回归验证（手动运行；会花钱；不进 CI、不进 test.ps1）。

.DESCRIPTION
    纪律（完整说明见 docs/pi-failure-suite.md）：

      1. 凭证只从**进程环境变量**读：API_URL / API_KEY。缺任何一个都在任何真实调用之前停下并报错，
         不退化成离线模型、不写进任何文件。
      2. **先声明上限再开跑**：-BudgetModels / -BudgetCostUsd（与平台同一套预算语义）。
      3. check（免费）→ record（花钱）→ estimate（免费）→ 确认预估在限额内 → regress（花钱，硬停）。
      4. 预估超出上限就**不跑**：脚本自己停下，让人决定是缩小任务集还是提高上限。

.EXAMPLE
    pwsh integrations/pi/scripts/real-model-run.ps1 -Samples 3
#>
[CmdletBinding()]
param(
    [string]$Suite = 'validation/failure-suite.json',
    [int]$Samples = 3,
    [string]$Model = 'deepseek-v4.1-flash',
    [string]$Provider = 'msee-gateway',
    [string]$ToolSource = 'snapshot',
    [int]$BudgetModels = 400,
    [double]$BudgetCostUsd = 2.0,
    [string]$Out = 'artifacts/round8',
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$pi = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repo = (Resolve-Path (Join-Path $pi '..\..')).Path
$outDir = Join-Path $pi $Out
$modelsPath = Join-Path $pi 'artifacts/models.json'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $modelsPath) | Out-Null

# ---------------------------------------------------------------- 1. 凭证（值不进任何文件）
if (-not $env:API_URL -or -not $env:API_KEY) {
    throw '缺少凭证：请先在当前进程里设置 API_URL 与 API_KEY（网关地址与密钥）。本脚本不会退化成离线模型，也不会写凭证到磁盘。'
}
$baseUrl = $env:API_URL.TrimEnd('/')
$uri = [Uri]$baseUrl
Write-Host "网关：$($uri.Scheme)://$($uri.Host)$($uri.AbsolutePath)（凭证来自环境变量，未打印）" -ForegroundColor Cyan

# 本地价目表只用于预算与估算，不是账单：默认取同网关同族模型的价目快照，来源写在下面这条说明里。
$priceInput = 0.22   # USD / 1M tokens
$priceOutput = 0.66  # USD / 1M tokens
$models = @{
    providers = @{
        $Provider = @{
            baseUrl = $baseUrl
            api = 'openai-completions'
            apiKey = '$API_KEY'   # 只写变量名：真正的值由 pi 在执行时从进程环境变量解析
            models = @(@{
                id = $Model
                name = $Model
                api = 'openai-completions'
                reasoning = $true
                input = @('text')
                cost = @{ input = $priceInput; output = $priceOutput; cacheRead = 0; cacheWrite = 0 }
                contextWindow = 1000000
                maxTokens = 8192
            })
        }
    }
}
$models | ConvertTo-Json -Depth 8 | Set-Content -Path $modelsPath -Encoding utf8
Write-Host "模型配置：$modelsPath（价目快照 $priceInput/$priceOutput USD per 1M tokens，来源与局限见 docs/pi-failure-suite.md）" -ForegroundColor DarkCyan

# ---------------------------------------------------------------- 2. 免费核对：网关、模型、参数
$catalog = Invoke-RestMethod -Uri "$baseUrl/models" -Headers @{ Authorization = "Bearer $($env:API_KEY)" } -TimeoutSec 30
$ids = @($catalog.data | ForEach-Object { $_.id })
if ($ids -notcontains $Model) { throw "网关的模型目录里没有 $Model（这一步不产生推理费用）。不换模型、不换网关：请先确认网关行为是否变化。" }
$tasks = (Get-Content (Join-Path $pi $Suite) -Raw | ConvertFrom-Json).tasks.Count
Write-Host "模型目录核对通过：$Model 在列（共 $($ids.Count) 个模型）。任务 $tasks 个 × 2 个条件 × $Samples 次采样。" -ForegroundColor Cyan
Write-Host "声明的上限：$BudgetModels 次真实模型调用 / `$$BudgetCostUsd（估算口径）" -ForegroundColor Yellow

$validateArgs = @('run','validate','--','--repo',$repo,'--out',$outDir,'--suite',$Suite,
    '--provider',$Provider,'--model',$Model,'--models-path',$modelsPath,'--tool-source',$ToolSource)
function Invoke-Phase {
    param([string[]]$Extra)
    Push-Location $pi
    try {
        & npm @($validateArgs + $Extra)
        if ($LASTEXITCODE -ne 0) { throw "验证阶段失败（exit $LASTEXITCODE）：$($Extra -join ' ')" }
    } finally { Pop-Location }
}

# ---------------------------------------------------------------- 3. check（免费）
Invoke-Phase @('--phase','check')
if ($CheckOnly) {
    Write-Host '仅自检（-CheckOnly）：已停在 check 阶段，没有产生任何推理费用。' -ForegroundColor Green
    return
}

# ---------------------------------------------------------------- 4. record（花钱，带声明上限）
Invoke-Phase @('--phase','record','--samples',"$Samples",'--budget-models',"$BudgetModels",'--budget-cost-usd',"$BudgetCostUsd")

# ---------------------------------------------------------------- 5. estimate（免费）
Invoke-Phase @('--phase','estimate','--samples',"$Samples")
$estimate = Get-Content (Join-Path $outDir 'estimate.json') -Raw | ConvertFrom-Json
if ($null -eq $estimate.cost_usd) { throw "预估无法定价（成本未知）：$($estimate.detail)  不拿未知当成 0，也不在没有预估的情况下开跑。" }
if ($estimate.cost_usd -gt $BudgetCostUsd) { throw "预估 $($estimate.cost_usd) 超出声明的上限 `$$BudgetCostUsd：缩小任务集或采样数后再跑。" }
if ($estimate.model_calls -gt $BudgetModels) { throw "预估 $($estimate.model_calls) 次调用超出声明的上限 $BudgetModels：缩小任务集或采样数后再跑。" }
Write-Host "预估通过：$($estimate.model_calls) 次调用、约 `$$($estimate.cost_usd)（估算，上限 `$$BudgetCostUsd）" -ForegroundColor Green

# ---------------------------------------------------------------- 6. regress（花钱，硬停）
Invoke-Phase @('--phase','regress','--samples',"$Samples",'--budget-models',"$BudgetModels",'--budget-cost-usd',"$BudgetCostUsd")

$archive = Get-Content (Join-Path $outDir 'archive.json') -Raw | ConvertFrom-Json
Write-Host ''
Write-Host '本轮实际发生（来自归档，不是估算）：' -ForegroundColor Cyan
Write-Host "  真实模型调用：$($archive.batch.usage.model_calls_used) 次；成本（按本地价目估算）：`$$($archive.batch.usage.cost_used_usd)"
Write-Host "  是否触顶：$($archive.batch.usage.exceeded)；触顶维度：$($archive.batch.usage.stopped_by)；未启动格子：$($archive.batch.not_started)"
Write-Host "  归档：$outDir/archive.json；报告：$outDir/report.md"
