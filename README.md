# GitCode 社区指标看板

一个公开链接，任何人打开都能看到 CANN 与 openJiuwen 两个组织的最新指标。
数据由 CI 定时采集，无人值守。

---

## 它是怎么运转的

```
GitHub Actions 定时触发（每 6 小时）
        │
   collect.py  ──调 GitCode OpenAPI──>  public/snapshots/latest.json
        │                                public/timeseries.csv
        │
   提交回仓库（历史快照与时间序列都留在 git 里）
        │
   发布 public/ 到 Pages  ──>  公开链接
        │
   访问者打开页面，页面每 5 分钟自动回查新数据
```

没有数据库，没有服务器。git 仓库本身就是时间序列存储——平台不提供历史
曲线，趋势只能自己攒，而仓库天然就是带时间戳的追加存储。

---

## 一键部署（推荐）

装好 [Git](https://git-scm.com/download/win) 与 [GitHub CLI](https://cli.github.com/)，
执行一次 `gh auth login` 完成登录，然后在解压目录里运行：

```powershell
powershell -ExecutionPolicy Bypass -File deploy.ps1
```

它会依次完成：建仓库 → 推代码 → 写入令牌密钥 → 启用 Pages → 触发首次采集，
最后打印出看板地址。令牌通过安全输入读取，只写进 GitHub 密钥，不落本地磁盘，
也不会出现在命令行历史里。

可选参数：`-RepoName 自定义仓库名`、`-Private`（私有仓库，注意免费账号的
私有仓库不支持 Pages）、`-SkipFirstRun`（不自动触发首轮采集）。

---

## 手动上线步骤

若不想用 CLI，按下面五步手工操作，效果相同。

### 1. 建仓库

把本目录全部内容推到一个 GitHub 仓库（默认分支 `main`）。

### 2. 配置令牌

在 gitcode.com/setting/token-classic 生成 Personal Access Token，**只勾读取类权限**。

仓库 → Settings → Secrets and variables → Actions → New repository secret：

| 名称 | 值 |
|---|---|
| `GITCODE_TOKEN` | 刚生成的令牌 |

令牌只存在于仓库密钥中，不进代码、不进页面、不进日志。

### 3. 打开 Pages

仓库 → Settings → Pages → Source 选 **GitHub Actions**。

### 4. 首次运行

仓库 → Actions → 「采集并发布看板」→ Run workflow。

跑完后 Actions 页面会给出 Pages 链接，形如
`https://<用户名>.github.io/<仓库名>/`。这就是要分享出去的地址。

### 5. 绑定自有域名（可选）

Settings → Pages → Custom domain 填入域名，再到域名服务商加一条 CNAME
指向 `<用户名>.github.io`。

---

## 日常维护

**自动的部分**：采集、提交、发布，每 6 小时一轮，不用管。

**需要人工的部分**：下载量与访问量。GitCode 没有开放流量类接口（已实测
10 个候选路径全部 404，官方 21 个 API 分类中亦无此类目），这两项只能从
网页抄录，填进 `manual_metrics.csv`：

```csv
repo,downloads_all,downloads_30d,visits_30d,measured_at
cann/elec-ops-prediction,10027,2676,1635,2026-10-20
```

`repo` 用 `组织/仓库` 全名，`measured_at` 填实测日期。改完提交即可，
页面会显示这个日期，并在它与自动采集时点相差超过 7 天时标黄提醒。

---

## 本地预览

改页面样式时不必等 CI：

```bash
python collect.py          # 需要 GITCODE_TOKEN 环境变量
python serve.py            # 自动打开浏览器
```

浏览器禁止 `file://` 页面读取本地 JSON，所以必须经 `serve.py` 走 HTTP，
双击 HTML 是打不开数据的。

---

## 调整采集范围

```bash
python collect.py                                  # 两个组织全部仓库
python collect.py --orgs cann --filter elec-ops    # 只采电力三库
python collect.py --orgs openJiuwen                # 只采 openJiuwen
python collect.py --window 7                       # 时间窗改为近 7 天
```

页面顶部的组织切换按钮是从数据里自动生成的，不需要配置。
若要拆成两个独立站点，各自用不同的 `--orgs` 参数产出到不同目录即可，
页面代码完全复用。

---

## 调整采集频率

改 `.github/workflows/update.yml` 里的 cron（UTC 时间）：

| 频率 | cron |
|---|---|
| 每 6 小时（默认） | `10 18,0,6,12 * * *` |
| 每天一次（北京时间 08:10） | `10 0 * * *` |
| 每小时 | `10 * * * *` |

GitCode 的速率限制是 400 次/分、4000 次/小时，采集器内置 320 次/分的
节流。110 个仓库一轮约 330 个请求，即使每小时跑一次也远未触及上限。

---

## 换成自己的服务器

如果不用 GitHub Pages，而是部署到自有服务器（和 openJiuwen 那个金融看板
一样挂在自有域名下），只需两步：

```bash
# 1. 定时采集
crontab -e
10 */6 * * * cd /srv/dashboard && GITCODE_TOKEN=xxx python3 collect.py --outdir public

# 2. 用任意静态服务指向 public/
#    nginx:  root /srv/dashboard/public;
```

页面与数据文件的相对路径不变，`public/` 目录原样搬过去就行。

---

## 已知限制

**下载量与访问量无法自动采集**。见上文，只能人工录入。这两项在页面上
用虚线边框和 `*` 标记区分，并单独标注实测日期。

**Star 数存在同步增长现象**。实测 2026-10-19 至 10-20，三个电力库的 Star
各增加 7、材料化学库增加 8，增幅高度一致。下载量也有类似特征（三库全历史
下载量极差仅 2.5%，而访问量相差 6 倍，二者不相关）。这些计数器的生成方式
存疑，用于对外汇报前建议核实。

**趋势曲线需要至少两次采集**才会出现。历史数据无法补录，越早开始跑越好。
