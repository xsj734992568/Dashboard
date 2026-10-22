<#
.SYNOPSIS
    一键部署 GitCode 指标看板到 GitHub Pages。

.DESCRIPTION
    把建仓库、推代码、设密钥、开 Pages、触发首次采集这五步合成一条命令。
    全部在本机执行，令牌只经由安全输入进入 GitHub 密钥，不落盘、不进命令行历史。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy.ps1
    powershell -ExecutionPolicy Bypass -File deploy.ps1 -RepoName my-dashboard -Private

.NOTES
    前置条件：
      1. 安装 Git           https://git-scm.com/download/win
      2. 安装 GitHub CLI    https://cli.github.com/
      3. 执行一次 gh auth login 完成 GitHub 登录
      4. 准备一个 GitCode 令牌（gitcode.com/setting/token-classic，只勾读取权限）
#>

[CmdletBinding()]
param(
    [string]$RepoName = "gitcode-dashboard",
    [switch]$Private,
    [switch]$SkipFirstRun
)

# 必须是 Continue 而非 Stop：PowerShell 会把 git/gh 写到 stderr 的正常输出
# （如 "No such remote"、push 的进度信息）当成错误记录，Stop 模式下会直接中断。
# 本脚本在每个关键步骤后自行检查 $LASTEXITCODE，不依赖异常机制。
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Step($n, $t) { Write-Host "`n[$n] $t" -ForegroundColor Cyan }
function Ok($t)       { Write-Host "    √ $t" -ForegroundColor Green }
function Warn($t)     { Write-Host "    ! $t" -ForegroundColor Yellow }
function Die($t)      { Write-Host "`n×  $t" -ForegroundColor Red; exit 1 }

Write-Host "============================================================"
Write-Host " GitCode 指标看板 · 一键部署"
Write-Host "============================================================"

# ---------- 0. 前置检查 ----------
Step 0 "检查环境"
foreach ($cmd in @("git", "gh")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Die "未找到 $cmd。请先安装：git → https://git-scm.com/download/win ；gh → https://cli.github.com/"
    }
    Ok "$cmd 已安装"
}
gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Die "GitHub CLI 尚未登录。请先执行：gh auth login" }
Ok "GitHub 已登录"

$account = (gh api user --jq .login 2>$null | Select-Object -First 1)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($account)) {
    Die "无法读取 GitHub 账号信息，请确认 gh auth login 已完成"
}
$account = $account.Trim()
Ok "账号 $account"

foreach ($f in @("collect.py", "public/index.html", ".github/workflows/update.yml")) {
    if (-not (Test-Path $f)) { Die "缺少文件 $f —— 请在解压后的目录里运行本脚本" }
}
Ok "部署文件齐备"

# ---------- 1. 令牌 ----------
Step 1 "输入 GitCode 令牌"
Write-Host "    在 https://gitcode.com/setting/token-classic 生成，只勾读取类权限。"
Write-Host "    输入时不会显示字符。" -ForegroundColor DarkGray
$sec = Read-Host "    令牌" -AsSecureString
$token = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
if ([string]::IsNullOrWhiteSpace($token)) { Die "未输入令牌" }
Ok "已接收（长度 $($token.Length)）"

# ---------- 2. 仓库 ----------
Step 2 "准备仓库 $account/$RepoName"
$exists = $false
gh repo view "$account/$RepoName" 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) { $exists = $true }

if ($exists) {
    Warn "仓库已存在，将推送到现有仓库"
} else {
    $vis = if ($Private) { "--private" } else { "--public" }
    gh repo create "$RepoName" $vis --description "GitCode 社区指标看板" | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "创建仓库失败" }
    Ok "已创建（$(if($Private){'私有'}else{'公开'})）"
    if ($Private) {
        Warn "私有仓库的 GitHub Pages 需要付费套餐；免费账号请改用 -Public"
    }
}

# ---------- 3. 推送 ----------
Step 3 "推送代码"
if (-not (Test-Path ".git")) {
    git init -q 2>&1 | Out-Null
    git branch -M main 2>&1 | Out-Null
    Ok "已初始化 git"
}
@"
__pycache__/
*.pyc
.venv/
"@ | Set-Content -Encoding utf8 ".gitignore"

# CI 运行在 Linux，CRLF 会导致 workflow 的 run 块与 Python 脚本异常
@"
* text=auto eol=lf
*.bat text eol=crlf
*.ps1 text eol=crlf
"@ | Set-Content -Encoding utf8 ".gitattributes"

git add -A 2>&1 | Out-Null
git diff --staged --quiet
if ($LASTEXITCODE -ne 0) {
    git -c user.name="deploy" -c user.email="deploy@local" commit -q -m "部署指标看板"
    Ok "已提交"
} else {
    Ok "无改动需提交"
}

# 用 git remote（列出全部，无远端时输出为空且不报错）判断，
# 不用 git remote get-url —— 后者在远端不存在时会写 stderr
$remotes = @(git remote 2>$null)
if ($remotes -notcontains "origin") {
    git remote add origin "https://github.com/$account/$RepoName.git" 2>&1 | Out-Null
    Ok "已添加远端 origin"
} else {
    git remote set-url origin "https://github.com/$account/$RepoName.git" 2>&1 | Out-Null
    Ok "已更新远端 origin"
}
# 普通推送，不加 --force：仓库若已有内容，宁可报错也不覆盖
git push -u origin main 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Warn "推送被拒绝，通常是远端已有内容"
    Write-Host "    若确认远端内容可丢弃，手动执行：git push -u origin main --force" -ForegroundColor DarkGray
    Write-Host "    若要保留，先执行：git pull --rebase origin main" -ForegroundColor DarkGray
    Die "推送失败"
}
Ok "已推送到 main"

# ---------- 4. 密钥 ----------
Step 4 "写入仓库密钥 GITCODE_TOKEN"
$token | gh secret set GITCODE_TOKEN --repo "$account/$RepoName" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Die "设置密钥失败" }
Remove-Variable token -ErrorAction SilentlyContinue
Ok "已写入（仅存于 GitHub 密钥，不落本地磁盘）"

# ---------- 5. Pages ----------
Step 5 "启用 GitHub Pages"
# 先尝试创建；已存在则改为更新（gh 的 -f 传表单字段，避免这里拼 JSON）
gh api -X POST "repos/$account/$RepoName/pages" -f "build_type=workflow" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    gh api -X PUT "repos/$account/$RepoName/pages" -f "build_type=workflow" 2>&1 | Out-Null
}
if ($LASTEXITCODE -eq 0) {
    Ok "已设为由 Actions 发布"
} else {
    Warn "自动启用失败（私有仓库免费账号不支持 Pages 是常见原因）"
    Warn "请手动到 Settings → Pages，Source 选 GitHub Actions"
}

# ---------- 6. 首次采集 ----------
if (-not $SkipFirstRun) {
    Step 6 "触发首次采集"
    gh workflow run "update.yml" --repo "$account/$RepoName" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Warn "触发失败。请到仓库 Actions 页手动点 Run workflow"
    } else {
        Ok "已触发，首轮采集约需 3-6 分钟"
        Write-Host "    实时查看：gh run watch --repo $account/$RepoName" -ForegroundColor DarkGray
    }
}

# ---------- 完成 ----------
$pagesUrl = "https://$($account.ToLower()).github.io/$RepoName/"
Write-Host "`n============================================================"
Write-Host " 部署完成" -ForegroundColor Green
Write-Host "============================================================"
Write-Host " 看板地址   $pagesUrl"
Write-Host " 仓库地址   https://github.com/$account/$RepoName"
Write-Host " 运行记录   https://github.com/$account/$RepoName/actions"
Write-Host ""
Write-Host " 首轮采集完成后看板才有数据，请等几分钟再打开。"
Write-Host " 此后每 6 小时自动更新一次，无需人工介入。"
Write-Host ""
Write-Host " 下载量与访问量需人工填入 manual_metrics.csv 后提交 —— " -NoNewline
Write-Host "GitCode 未开放这两项接口。" -ForegroundColor DarkGray
Write-Host "============================================================"
