#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看板本地静态服务
================================================================================

为什么需要它
    浏览器出于安全限制，用 file:// 打开的页面不能 fetch 本地 JSON，
    所以看板必须经 HTTP 访问。这个脚本就是最小的静态服务。
    它同时也是将来部署到静态托管上的本地等价物 —— 线上换成托管平台，
    页面与数据文件的相对路径完全不变。

目录约定（都在本脚本所在目录）
    dashboard.html              看板页面
    snapshots/latest.json       collect.py 产出的最新快照
    timeseries.csv              collect.py 追加的时间序列（趋势曲线用）

用法
    python serve.py                  启动并自动打开浏览器
    python serve.py --port 9000      指定端口
    python serve.py --no-open        不自动开浏览器
    python serve.py --host 0.0.0.0   允许局域网内其他机器访问

依赖
    仅标准库。
"""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Handler(http.server.SimpleHTTPRequestHandler):
    """禁用缓存，使刷新页面即可看到新快照。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_GET(self):                                    # noqa: N802
        if self.path in ("/", "/index.html"):
            self.path = "/dashboard.html"
        if self.path == "/favicon.ico":
            # 浏览器总会请求它；返回 204 以免在控制台留下无意义的 404
            self.send_response(204)
            self.end_headers()
            return
        return super().do_GET()

    def log_message(self, fmt, *args):                   # noqa: A003
        # 只记录失败请求，正常请求不刷屏
        try:
            code = int(args[1])
        except (IndexError, ValueError, TypeError):
            code = 0
        if code >= 400:
            sys.stderr.write(f"  [{code}] {args[0]}\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def preflight() -> bool:
    """启动前检查数据文件是否就绪，缺什么直接说清楚。"""
    ok = True
    page = ROOT / "dashboard.html"
    snap = ROOT / "snapshots" / "latest.json"
    series = ROOT / "timeseries.csv"

    if not page.exists():
        print(f"  ✗ 缺少 dashboard.html（应与本脚本同目录：{ROOT}）")
        ok = False
    else:
        print("  ✓ dashboard.html")

    if not snap.exists():
        print(f"  ✗ 缺少 snapshots/latest.json")
        print("     请先在同一目录运行：python collect.py")
        ok = False
    else:
        try:
            d = json.loads(snap.read_text(encoding="utf-8"))
            n = len(d.get("repos") or [])
            t = (d.get("collected_at") or "").replace("T", " ")[:16]
            print(f"  ✓ snapshots/latest.json — {n} 个仓库，采集于 {t}")
            if not d.get("manual_metrics_measured_at"):
                print("    提示：未录入下载量/访问量（manual_metrics.csv），"
                      "看板中这两列会显示为空")
        except Exception as e:                            # noqa: BLE001
            print(f"  ✗ snapshots/latest.json 解析失败：{e}")
            ok = False

    if series.exists():
        try:
            rows = max(0, len(series.read_text(encoding="utf-8-sig")
                               .strip().splitlines()) - 1)
            stamps = set()
            for ln in series.read_text(encoding="utf-8-sig").splitlines()[1:]:
                stamps.add(ln.split(",")[0])
            print(f"  ✓ timeseries.csv — {rows} 行，{len(stamps)} 次采集"
                  + ("（趋势曲线需要至少 2 次）" if len(stamps) < 2 else ""))
        except Exception:                                 # noqa: BLE001
            print("  · timeseries.csv 存在但读取异常，趋势曲线可能不显示")
    else:
        print("  · 无 timeseries.csv，趋势曲线暂不显示（跑过两次采集后出现）")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="看板本地静态服务")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--host", default="127.0.0.1",
                    help="默认仅本机可访问；传 0.0.0.0 可供局域网访问")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    print("=" * 58)
    print("看板本地服务")
    print("=" * 58)
    print(f"目录：{ROOT}")
    print("检查数据文件：")
    ready = preflight()

    port = args.port
    for _ in range(20):
        try:
            httpd = Server((args.host, port), Handler)
            break
        except OSError:
            port += 1
    else:
        print(f"\n✗ {args.port} 起连续 20 个端口都被占用，用 --port 指定其他端口。")
        return 1

    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{port}/"
    print(f"\n服务已启动：{url}")
    if args.host == "0.0.0.0":
        print("（已开放局域网访问）")
    if not ready:
        print("注意：上面有缺失项，页面会提示具体原因。")
    print("采集器重新跑过之后，直接刷新页面即可看到新数据。")
    print("按 Ctrl+C 停止。\n")

    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
