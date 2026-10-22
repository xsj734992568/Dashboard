#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitCode 组织指标采集器 v1
================================================================================

产出
    snapshots/YYYY-MM-DD_HHMM.json   每次运行的完整快照（看板数据源）
    snapshots/latest.json            指向最近一次快照的副本
    timeseries.csv                   逐仓库逐次追加，方便直接出趋势图

    快照与 CSV 都带采集时间戳。平台不提供历史曲线，趋势只能靠自己攒，
    所以越早开始跑越好 —— 漏掉的那天补不回来。

数据来源（经 2026-10-20 实测校准）
    平台指标  https://api.gitcode.com/api/v5  + Personal Access Token
      · GET /orgs/{org}/repos?per_page=100
          一次拿全组织的 stargazers_count / forks_count / watchers_count /
          open_issues_count，85 个仓库约 4 秒。
      · GET /repos/{o}/{r}/issues?state=all&per_page=1
          不翻页，直接读响应头 total_count 得精确总数。state 可换 open/closed。
      · GET /repos/{o}/{r}/pulls?only_count=true
          一次返回 {all, opened, closed, merged, locked} 全量分解；
          该接口忽略 state 参数，调一次即可。加 created_after 可取时间窗增量。
      · GET /repos/{o}/{r}/commits?per_page=1
          读响应头 total_count 得提交总数；加 since 取时间窗增量。
          实测与 git clone 本地统计结果一致，故无需克隆仓库。

    人工指标  manual_metrics.csv
      下载量与访问量在 GitCode 没有任何公开接口（已实测 10 个候选路径全部 404），
      只能人工从网页抄录。脚本会读取该 CSV 并标注其实测日期，
      与自动指标的时点差异会在快照里显式记录，避免把旧数当新数。

已知的平台数据坑（实测发现，脚本已处理）
    · status 字段文档写的是 0/2，实际返回中文字符串（如 "开始"），不可据此判归档。
    · full_name 带空格，形如 "CANN / npu-simulator"，不能当路径用；
      本脚本一律用 namespace.path + path 自行拼接。
    · 响应头里 total_page = ceil(total_count / per_page)，真正可靠的是 total_count。

用法
    python collect.py                          # 采集全部配置的组织
    python collect.py --orgs cann              # 只采一个组织
    python collect.py --filter elec-ops        # 仓库名包含该串才采（做子看板用）
    python collect.py --window 30              # 时间窗天数，默认 30
    python collect.py --workers 8              # 并发数，默认 6
    python collect.py --quick                  # 只采组织级字段，跳过逐仓库调用
    python collect.py --transport curl         # 强制通道

依赖
    仅标准库。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import getpass
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.gitcode.com/api/v5"
ORGS = ["cann", "openJiuwen"]
UA = "gitcode-dashboard-collector/1.0"
TIMEOUT = 60
RATE_PER_MIN = 320          # 官方上限 400/分，留两成余量

_TOKEN = ""
_PROXY: str | None = None
_ACTIVE = "curl"
_lock = threading.Lock()
_stamps: list[float] = []


# ==============================================================================
# 基础设施：脱敏、限流、三通道请求
# ==============================================================================

def _scrub(t) -> str:
    s = str(t or "")
    if _TOKEN:
        s = s.replace(_TOKEN, "<TOKEN>")
    s = re.sub(r"(access_token=)[^&\s\"']+", r"\1<TOKEN>", s)
    s = re.sub(r"://[^/@\s:]+:[^/@\s]+@", "://<USER>:<PASS>@", s)
    return s


def _throttle() -> None:
    """简单滑动窗口限流，确保不超过 RATE_PER_MIN。"""
    with _lock:
        now = time.time()
        _stamps[:] = [t for t in _stamps if now - t < 60]
        if len(_stamps) >= RATE_PER_MIN:
            time.sleep(max(0.0, 60 - (now - _stamps[0])) + 0.05)
            now = time.time()
            _stamps[:] = [t for t in _stamps if now - t < 60]
        _stamps.append(now)


class ApiError(Exception):
    pass


def _req_urllib(url: str):
    h = []
    if _PROXY:
        h.append(urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY}))
    h.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    r = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json", "PRIVATE-TOKEN": _TOKEN})
    try:
        with urllib.request.build_opener(*h).open(r, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, (e.read() if hasattr(e, "read") else b""), \
               {k.lower(): v for k, v in (e.headers or {}).items()}


def _req_curl(url: str):
    exe = shutil.which("curl") or shutil.which("curl.exe")
    if not exe:
        raise ApiError("未找到 curl")
    with tempfile.TemporaryDirectory() as td:
        b, hd = Path(td) / "b", Path(td) / "h"
        argv = [exe, "--config", "-", "-sS", "--max-time", str(TIMEOUT),
                "-o", str(b), "-D", str(hd)]
        if _PROXY:
            argv += ["-x", _PROXY]
        argv.append(url)
        # token 走 stdin，不进进程命令行；路径走 argv，避免配置文件反斜杠转义
        cfg = f'header = "PRIVATE-TOKEN: {_TOKEN}"\nheader = "User-Agent: {UA}"\n' \
              'header = "Accept: application/json"\n'
        p = subprocess.run(argv, input=cfg, capture_output=True, text=True,
                           timeout=TIMEOUT + 20)
        status, hdrs = None, {}
        if hd.exists():
            for line in hd.read_text("utf-8", "replace").splitlines():
                if line.upper().startswith("HTTP/"):
                    parts = line.split()
                    if len(parts) > 1 and parts[1].isdigit():
                        status, hdrs = int(parts[1]), {}
                elif ":" in line:
                    k, v = line.split(":", 1)
                    hdrs[k.strip().lower()] = v.strip()
        if status is None:
            raise ApiError(_scrub(" ".join((p.stderr or "无响应").split()))[:200])
        return status, (b.read_bytes() if b.exists() else b""), hdrs


def _req_powershell(url: str):
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise ApiError("未找到 PowerShell")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "o.json"
        px = f" -Proxy '{_PROXY}'" if _PROXY else ""
        script = (
            "$ErrorActionPreference='Stop';"
            "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
            "try{$r=Invoke-WebRequest -Uri '" + url + "' -Headers @{'PRIVATE-TOKEN'=$env:GCTK;"
            "'Accept'='application/json'} -UseBasicParsing -UserAgent '" + UA + "'"
            f" -TimeoutSec {TIMEOUT}{px};"
            "$o=@{status=$r.StatusCode;body=$r.Content;hdr=($r.Headers|ConvertTo-Json -Compress)}}"
            "catch{$c=$null;if($_.Exception.Response){$c=[int]$_.Exception.Response.StatusCode};"
            "$o=@{status=$c;body='';err=$_.Exception.Message}};"
            f"$o|ConvertTo-Json -Compress|Set-Content -Encoding utf8 '{out}'")
        subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=TIMEOUT + 25,
                       env={**os.environ, "GCTK": _TOKEN})
        if not out.exists():
            raise ApiError("PowerShell 未产出结果")
        # PowerShell 的 utf8 带 BOM，必须用 utf-8-sig 读（v1 探测脚本在此踩过坑）
        o = json.loads(out.read_text("utf-8-sig", errors="replace"))
        if o.get("status") is None:
            raise ApiError(_scrub(o.get("err"))[:200])
        hdrs = {}
        try:
            raw = json.loads(o.get("hdr") or "{}")
            if isinstance(raw, dict):
                hdrs = {k.lower(): (v[0] if isinstance(v, list) and v else v)
                        for k, v in raw.items()}
        except ValueError:
            pass
        return int(o["status"]), str(o.get("body") or "").encode(), hdrs


_TRANSPORTS = {"urllib": _req_urllib, "curl": _req_curl, "powershell": _req_powershell}


def api(path: str, params: dict | None = None, retries: int = 2) -> tuple[object, dict]:
    """返回 (解析后的 body, 响应头)。失败抛 ApiError。"""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = ""
    for attempt in range(retries + 1):
        _throttle()
        try:
            status, raw, hdrs = _TRANSPORTS[_ACTIVE](url)
        except Exception as e:                                # noqa: BLE001
            last = _scrub(e)
            time.sleep(1.5 * (attempt + 1))
            continue
        if status == 429:
            time.sleep(8 * (attempt + 1))
            last = "429 限流"
            continue
        if status >= 500:
            last = f"HTTP {status}"
            time.sleep(2 * (attempt + 1))
            continue
        if not (200 <= status < 300):
            raise ApiError(f"HTTP {status} @ {_scrub(path)}")
        try:
            return json.loads(raw.decode("utf-8", "replace")), hdrs
        except ValueError:
            raise ApiError(f"响应非 JSON @ {_scrub(path)}")
    raise ApiError(f"{last} @ {_scrub(path)}（已重试 {retries} 次）")


def _total(hdrs: dict) -> int | None:
    """响应头里的精确总数。实测 total_count 可靠，total_page 只是换算值。"""
    for k in ("total_count", "x-total-count", "total"):
        if k in hdrs and str(hdrs[k]).strip().isdigit():
            return int(hdrs[k])
    return None


# ==============================================================================
# 采集
# ==============================================================================

def pick_transport(force: str | None) -> str:
    if force:
        return force
    probe = f"/orgs/{ORGS[0]}/repos"
    for name in ("curl", "urllib", "powershell"):
        global _ACTIVE
        _ACTIVE = name
        try:
            api(probe, {"per_page": 1}, retries=0)
            return name
        except Exception:                                     # noqa: BLE001
            continue
    raise SystemExit("三条通道均不可用，请先用 gitcode_api_probe.py --diagnose 排查。")


def list_org_repos(org: str) -> list[dict]:
    """一次调用拿全组织核心指标（最划算的一步）。"""
    out, page = [], 1
    while page <= 20:
        body, hdrs = api(f"/orgs/{org}/repos",
                         {"page": page, "per_page": 100, "type": "all"})
        if not isinstance(body, list):
            break
        out.extend(body)
        if len(body) < 100:
            break
        page += 1
    return out


def repo_detail(org: str, repo: str, since_iso: str) -> dict:
    """逐仓库补齐累计类与时间窗类指标。每仓库 6 个请求。"""
    d: dict = {"errors": []}

    def count(path: str, params: dict) -> int | None:
        try:
            _, h = api(path, params)
            return _total(h)
        except ApiError as e:
            d["errors"].append(str(e))
            return None

    base = f"/repos/{org}/{repo}"
    d["issues_total"] = count(f"{base}/issues", {"state": "all", "per_page": 1})
    d["issues_open"] = count(f"{base}/issues", {"state": "open", "per_page": 1})
    d["issues_closed"] = count(f"{base}/issues", {"state": "closed", "per_page": 1})
    d["commits_total"] = count(f"{base}/commits", {"per_page": 1})
    d["commits_window"] = count(f"{base}/commits", {"since": since_iso, "per_page": 1})

    # PR：一次返回全状态分解，且忽略 state 参数
    try:
        body, _ = api(f"{base}/pulls", {"only_count": "true"})
        if isinstance(body, dict):
            d.update({f"pr_{k}": body.get(k) for k in
                      ("all", "opened", "closed", "merged", "locked")})
    except ApiError as e:
        d["errors"].append(str(e))
    try:
        body, _ = api(f"{base}/pulls",
                      {"only_count": "true", "created_after": since_iso})
        if isinstance(body, dict):
            d["pr_created_window"] = body.get("all")
    except ApiError as e:
        d["errors"].append(str(e))
    return d


def branch_detail(org: str, repo: str, branch: str, since_iso: str) -> dict:
    """采集某个分支的提交数与以它为目标的 PR 数。

    GitCode 文档未写明 commits 接口的分支参数名，这里依次尝试 sha / ref /
    branch，并把真正生效的那个记进快照，便于以后确认。
    """
    d: dict = {"branch": branch, "errors": []}
    base = f"/repos/{org}/{repo}"

    # 提交数：全量与时间窗
    param_used = None
    for key in ("sha", "ref", "branch"):
        try:
            _, h = api(f"{base}/commits", {key: branch, "per_page": 1})
            tot = _total(h)
            if tot is None:
                continue
            d["commits_total"] = tot
            param_used = key
            break
        except ApiError as e:
            d["errors"].append(f"commits[{key}]: {e}")
    d["branch_param"] = param_used
    if param_used:
        try:
            _, h = api(f"{base}/commits",
                       {param_used: branch, "since": since_iso, "per_page": 1})
            d["commits_window"] = _total(h)
        except ApiError as e:
            d["errors"].append(str(e))

    # PR：以该分支为合入目标（base 参数文档中明确支持）
    try:
        body, _ = api(f"{base}/pulls", {"only_count": "true", "base": branch})
        if isinstance(body, dict):
            d.update({f"pr_{k}": body.get(k) for k in
                      ("all", "opened", "closed", "merged", "locked")})
    except ApiError as e:
        d["errors"].append(str(e))
    try:
        body, _ = api(f"{base}/pulls",
                      {"only_count": "true", "base": branch,
                       "created_after": since_iso})
        if isinstance(body, dict):
            d["pr_created_window"] = body.get("all")
    except ApiError as e:
        d["errors"].append(str(e))
    return d


def load_manual(path: Path) -> tuple[dict, str | None]:
    """读取人工录入的下载量/访问量。CSV 列：repo,downloads_all,downloads_30d,visits_30d,measured_at"""
    if not path.exists():
        return {}, None
    data, measured = {}, None
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = (row.get("repo") or "").strip()
            if not key:
                continue
            data[key] = {k: _int(row.get(k)) for k in
                         ("downloads_all", "downloads_30d", "visits_30d")}
            measured = (row.get("measured_at") or measured or "").strip() or measured
    return data, measured


def _int(v):
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ==============================================================================
# 主流程
# ==============================================================================

def main() -> int:
    global _TOKEN, _PROXY, _ACTIVE
    here = Path(__file__).resolve().parent

    ap = argparse.ArgumentParser(description="GitCode 组织指标采集器")
    ap.add_argument("--orgs", nargs="+", default=ORGS)
    ap.add_argument("--filter", default=None, help="仓库名包含该子串才采集")
    ap.add_argument("--window", type=int, default=30, help="时间窗天数，默认 30")
    ap.add_argument("--workers", type=int, default=6, help="并发数，默认 6")
    ap.add_argument("--quick", action="store_true", help="只采组织级字段")
    ap.add_argument("--transport", choices=list(_TRANSPORTS), default=None)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--manual", default=None, help="人工指标 CSV，默认 manual_metrics.csv")
    args = ap.parse_args()

    _TOKEN = os.environ.get("GITCODE_TOKEN", "").strip()
    if not _TOKEN:
        try:
            _TOKEN = getpass.getpass("粘贴 GitCode token（不回显）：").strip()
        except (EOFError, KeyboardInterrupt):
            return 2
    if not _TOKEN:
        print("缺少 token。"); return 2

    if args.no_proxy:
        _PROXY = None
    elif args.proxy:
        _PROXY = args.proxy
    else:
        try:
            px = urllib.request.getproxies()
            _PROXY = px.get("https") or px.get("http")
        except Exception:                                     # noqa: BLE001
            _PROXY = None

    outdir = Path(args.outdir).resolve() if args.outdir else here
    snapdir = outdir / "snapshots"
    snapdir.mkdir(parents=True, exist_ok=True)
    manual_path = Path(args.manual) if args.manual else outdir / "manual_metrics.csv"

    t_start = time.time()
    now = datetime.now().astimezone()
    since = (now - timedelta(days=args.window)).astimezone(timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")

    print("=" * 62)
    print(f"GitCode 指标采集  {now.isoformat(timespec='seconds')}")
    print("=" * 62)
    if _PROXY:
        print(f"代理：{_scrub(_PROXY)}")
    _ACTIVE = pick_transport(args.transport)
    print(f"通道：{_ACTIVE}｜时间窗：近 {args.window} 天｜并发：{args.workers}")

    manual, manual_date = load_manual(manual_path)
    print(f"人工指标：{len(manual)} 条"
          + (f"（实测日 {manual_date}）" if manual_date else "（未提供）"))

    rows: list[dict] = []
    for org in args.orgs:
        print(f"\n[{org}] 拉取仓库列表 …", end=" ", flush=True)
        try:
            repos = list_org_repos(org)
        except ApiError as e:
            print(f"失败：{e}")
            continue
        if args.filter:
            repos = [r for r in repos if args.filter in (r.get("path") or "")]
        print(f"{len(repos)} 个")

        for r in repos:
            ns = (r.get("namespace") or {}).get("path") or org
            path = r.get("path") or ""
            rows.append({
                "org": ns, "repo": path, "key": f"{ns}/{path}",
                # full_name 实测带空格（"CANN / x"），不可用作标识，这里自行拼接
                "display_name": (r.get("full_name") or f"{ns}/{path}").replace(" ", ""),
                "description": (r.get("description") or "").strip(),
                "language": r.get("language"),
                "default_branch": r.get("default_branch"),
                "stars": r.get("stargazers_count"),
                "forks": r.get("forks_count"),
                "watchers": r.get("watchers_count"),
                "issues_open_hint": r.get("open_issues_count"),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
                "pushed_at": r.get("pushed_at"),
                "status_raw": r.get("status"),   # 文档说 0/2，实际是中文串，原样留存
            })

    if not rows:
        print("\n没有采集到任何仓库，终止。")
        return 1

    if not args.quick:
        total = len(rows)
        print(f"\n逐仓库补齐累计与时间窗指标（{total} 个 × 6 请求）…")
        done = 0
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(repo_detail, x["org"], x["repo"], since): x
                    for x in rows}
            for fu in cf.as_completed(futs):
                x = futs[fu]
                done += 1
                try:
                    x.update(fu.result())
                except Exception as e:                        # noqa: BLE001
                    x["errors"] = [_scrub(e)]
                if done % 10 == 0 or done == total:
                    el = time.time() - t_start
                    print(f"   {done}/{total}  已用 {el:.0f}s"
                          f"  预计剩余 {el / done * (total - done):.0f}s")

    # 分支级目标：追加为独立行，仓库级指标继承自母仓库并标注
    if BRANCH_TARGETS and not args.quick:
        print(f"\n分支级采集（{len(BRANCH_TARGETS)} 个目标）…")
        for org, repo, branch in BRANCH_TARGETS:
            parent = next((x for x in rows
                           if x["org"] == org and x["repo"] == repo), None)
            print(f"      · {org}/{repo}@{branch} …", end=" ", flush=True)
            try:
                bd = branch_detail(org, repo, branch, since)
            except Exception as e:                    # noqa: BLE001
                print(f"失败：{_scrub(e)}")
                continue
            row = {
                "org": org, "repo": f"{repo}@{branch}",
                "key": f"{org}/{repo}@{branch}",
                "display_name": f"{org}/{repo}@{branch}",
                "is_branch": True, "parent_repo": repo, "branch": branch,
                "description": (parent or {}).get("description", ""),
                "language": (parent or {}).get("language"),
                "default_branch": branch,
                # 仓库级指标分支不独有，继承并标注
                "stars": (parent or {}).get("stars"),
                "forks": (parent or {}).get("forks"),
                "watchers": (parent or {}).get("watchers"),
                "issues_total": (parent or {}).get("issues_total"),
                "issues_open": (parent or {}).get("issues_open"),
                "repo_level_inherited": True,
                "created_at": (parent or {}).get("created_at"),
                "updated_at": (parent or {}).get("updated_at"),
                "pushed_at": (parent or {}).get("pushed_at"),
            }
            row.update({k: v for k, v in bd.items() if k != "errors"})
            row["errors"] = bd.get("errors", [])
            rows.append(row)
            print(f"提交 {bd.get('commits_total')}｜PR {bd.get('pr_all')}"
                  f"（分支参数 {bd.get('branch_param') or '未生效'}）")

    # 合并人工指标
    for x in rows:
        m = manual.get(x["key"]) or manual.get(x["repo"]) or {}
        x["downloads_all"] = m.get("downloads_all")
        x["downloads_30d"] = m.get("downloads_30d")
        x["visits_30d"] = m.get("visits_30d")

    def ssum(field: str) -> int:
        return sum(v for v in (x.get(field) for x in rows) if isinstance(v, int))

    snapshot = {
        "schema": 1,
        "collected_at": now.isoformat(timespec="seconds"),
        "window_days": args.window,
        "window_since_utc": since,
        "orgs": args.orgs,
        "branch_targets": [f"{o}/{r}@{b}" for o, r, b in BRANCH_TARGETS],
        "branch_note": ("分支行的 Star/Fork/Issue 继承自所属仓库（分支无独立计数），"
                        "仅提交数与 PR 数为该分支实测"),
        "repo_filter": args.filter,
        "quick_mode": args.quick,
        "manual_metrics_measured_at": manual_date,
        "manual_metrics_note": ("下载量/访问量无公开 API（已实测 10 个候选路径全部 404），"
                                "由人工录入；其时点可能与自动指标不同，见上面两个日期"),
        "transport": _ACTIVE,
        "elapsed_sec": round(time.time() - t_start, 1),
        "repo_count": len(rows),
        "totals": {k: ssum(k) for k in (
            "stars", "forks", "watchers", "issues_total", "issues_open",
            "issues_closed", "pr_all", "pr_merged", "pr_created_window",
            "commits_total", "commits_window",
            "downloads_all", "downloads_30d", "visits_30d")},
        "repos": rows,
    }

    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    jpath = snapdir / f"{stamp}.json"
    jpath.write_text(_scrub(json.dumps(snapshot, ensure_ascii=False, indent=2)),
                     encoding="utf-8")
    (snapdir / "latest.json").write_text(
        _scrub(json.dumps(snapshot, ensure_ascii=False, indent=2)), encoding="utf-8")

    # 时间序列 CSV：逐次追加，便于直接出趋势图
    cols = ["collected_at", "org", "repo", "stars", "forks", "watchers",
            "issues_total", "issues_open", "issues_closed",
            "pr_all", "pr_opened", "pr_closed", "pr_merged", "pr_created_window",
            "commits_total", "commits_window",
            "downloads_all", "downloads_30d", "visits_30d"]
    cpath = outdir / "timeseries.csv"
    new = not cpath.exists()
    with cpath.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        for x in rows:
            w.writerow({**x, "collected_at": snapshot["collected_at"]})

    # 与上一份快照对比，输出变化量
    prev = sorted(p for p in snapdir.glob("*.json") if p.name != "latest.json")
    delta_line = ""
    if len(prev) >= 2:
        try:
            old = json.loads(prev[-2].read_text(encoding="utf-8"))
            ds = snapshot["totals"]["stars"] - old["totals"].get("stars", 0)
            delta_line = (f"  较上次（{old['collected_at'][:16]}）Star "
                          f"{ds:+d}")
        except Exception:                                     # noqa: BLE001
            pass

    t = snapshot["totals"]
    err = sum(1 for x in rows if x.get("errors"))
    print("\n" + "=" * 62)
    print(f"完成：{len(rows)} 个仓库，用时 {snapshot['elapsed_sec']}s"
          + (f"，{err} 个有接口错误" if err else ""))
    print(f"  Star {t['stars']:,}｜Fork {t['forks']:,}｜"
          f"Issue 累计 {t['issues_total']:,}（未关闭 {t['issues_open']:,}）")
    print(f"  PR 累计 {t['pr_all']:,}（已合入 {t['pr_merged']:,}，"
          f"近{args.window}天新增 {t['pr_created_window']:,}）")
    print(f"  提交累计 {t['commits_total']:,}（近{args.window}天 {t['commits_window']:,}）")
    if delta_line:
        print(delta_line)
    print(f"\n  快照：{jpath}")
    print(f"  时序：{cpath}")
    if not manual:
        print(f"\n  提示：未找到 {manual_path.name}，下载量/访问量为空。")
        print("        建一个 CSV，列：repo,downloads_all,downloads_30d,visits_30d,measured_at")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
