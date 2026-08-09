# Reuters + Bloomberg 中国相关新闻 → 企业微信推送（GitHub Actions）

定时抓取 **Google News RSS**（Reuters / Bloomberg 两个来源 × 主查询 + 补充主题查询 `beijing` / `taiwan` / `hong kong`，合并去重），把最近 24 小时两大财经媒体关于中国的报道推送到企业微信群。

> 为什么不用官网直抓：实测 reuters.com 部署 CloudFront + **DataDome 反爬**（HTTP 401）、bloomberg.com 反爬（HTTP **403**），脚本直抓均不可行；
> Google News RSS 为标准 XML、无反爬、免费、稳定，实测 200，Reuters 来源 100%、Bloomberg 来源 93%+（Bloomberg.com + Bloomberg LEI）。
> 为什么多查询合并：Google News RSS 单查询最多返回 100 条且按相关性排序，24h 内相关报道超过 100 条时会漏报，
> 补充主题查询可捞回主查询漏掉的条目（实测 8 查询合并 369 条 vs 单查询 100 条，过滤后 42 条）。

## 文件结构

| 文件 | 用途 |
|---|---|
| `fetch_reuters.py` | 多查询抓取 RSS + 合并去重 + 关键词/时间过滤 + 去重 + 企业微信纯文本推送（无超链接） |
| `.github/workflows/reuters-china-push.yml` | GitHub Actions 定时任务定义（每小时整点 UTC） |
| `last_sent.json` | 运行期生成：已推送 URL 记录（自动创建，勿手动编辑，不入 git） |
| `.env.local` | 本地调试配置（含完整 webhook 地址，**不入 git**）；GitHub 部署用 Secret 即可 |

## 部署步骤

1. **建 GitHub 仓库**，把本目录三个文件推上去。
2. **企业微信群添加机器人**：群设置 → 群机器人 → 添加，复制 webhook 地址中 `key=` 后面的字符串。
3. **配置 Secret**：仓库 Settings → Secrets and variables → Actions → New repository secret，名称 `WECOM_WEBHOOK_KEY`，值粘贴上一步的 key。
4. **手动验证**：仓库 Actions 页面 → `reuters-china-push` → Run workflow，看日志确认抓取与推送成功。
5. **确认定时生效**：默认每小时整点（UTC）自动运行；改频率编辑 yaml 里的 `cron` 行。

## 本地调试

```bash
# 方式一（推荐）：创建 .env.local 文件（参考 .env.local.example），内容一行：
#   WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的完整key
# 脚本优先读取该文件，完整 key 不会出现在命令行/日志中

# 方式二：环境变量
set WECOM_WEBHOOK_KEY=你的key        # PowerShell: $env:WECOM_WEBHOOK_KEY="你的key"

# 本机访问海外站点需代理（本机 7890 端口有 Clash 类代理可复用）
pip install requests
$env:HTTPS_PROXY="http://127.0.0.1:7890"
python fetch_reuters.py
```

> 实测参考（2026-08-08，双源版）：走代理抓取 8 个查询（Reuters 4 + Bloomberg 4）→ 合并去重 369 条 → 关键词/时间过滤 42 条 → 按来源分组纯文本推送成功（2 条消息：20+23）。

## 修改指南

| 想改什么 | 改哪里 |
|---|---|
| 推送频率 | yaml 的 `cron` 表达式（UTC 时区） |
| 搜索查询（新增/删减来源或主题） | `fetch_reuters.py` 顶部 `SOURCES` dict（来源名 → 查询列表，URL 编码，`site:reuters.com china when:1d`；dict 顺序即推送分组顺序） |
| 过滤关键词集合 | `fetch_reuters.py` 的 `KEYWORDS` 列表 |
| 时间窗口 | 各查询的 `when:1d`（可选 `when:7d`、`when:1h`） |
| 消息文案/格式 | `push_wecom()` 函数 |
| webhook 地址 | Secret `WECOM_WEBHOOK_KEY`（或本地 `.env.local` / 环境变量 `WECOM_WEBHOOK_URL`） |

## 过滤规则（2026-08-08 升级）

- **关键词（子串匹配）**：标题含 china/chinese/beijing/hong kong/taiwan/xi jinping/us-china/sino-/shanghai/shenzhen 之一
- **中国公司名（词边界匹配）**：标题含公司名单中任一名称（美股中概 + 港股巨头 + A 股巨头 + 非上市巨头 + AI 新势力，共 99 个词条；独立成词才命中，避免 nio/junio、gree/Greece、jd/人名 等撞词误报）
- 两者为**并集**（命中任一即通过），再叠加 24 小时时间校验

## 已知限制（诚实说明）

- **抓取重试（2026-08-09 新增）**：查询失败自动重试 1 次（间隔 5 秒）——GitHub Actions 云端 runner 的 IP 池可能被 Google News 瞬时风控（503），重试可显著降低误报；但若 IP 段长期被标记，错误通知仍会出现（缓解非根治，终极方案是备用源/自托管 runner）。
- **错误处理（2026-08-08 新增）**：单查询失败（重试后）→ 附注附加在当轮消息末尾（`⚠️ 注：n/8 查询失败...，可能漏报`）；无新消息时改为失败提示；全部查询失败（重试后）/ 推送失败（重试 1 次后）/ 未捕获异常 → 推送独立错误消息（⚠️ 前缀）；**key 缺失或无效时无法推送错误消息**（webhook 本身不可用），只能靠 GitHub Actions 红叉兑底。

- **链接为 Google News 跳转链**：条目链接是 `news.google.com/rss/articles/...`，点击后跳转到 reuters 原文，企业微信内可正常打开（推送已不包含链接，仅内部记录用）。
- **单查询 100 条上限**：Google News RSS 每查询最多 100 条且按相关性排序，单查询会漏报；已用多查询合并缓解，但极端情况下（单主题也超 100 条）仍可能漏。
- **标题关键词过滤**：`KEYWORDS` 二次过滤兜底，个别条目标题不含中国关键词会被筛掉；如需更全可放宽关键词或增加查询。
- **cron 不保证准时**：GitHub Actions 定时任务可能有数分钟~数十分钟延迟，偶发跳过。
- **去重依赖 Actions Cache**：缓存可能被清理，清理后会出现少量重复推送，属可接受范围。
- **消息超长自动分组**：企业微信 text 单条上限 2048 字节，条数多时脚本按字节预算自动拆分为多条发送（实测带时间 22 条 → 2 条消息：20+2）。
- **纯文本推送（无超链接、无 URL）**：消息类型为 text，格式为「编号. [MM-DD HH:mm] 标题」，按来源分组（组间 `— Bloomberg —` 分隔行，组内时间倒序最新在前）；仅推送标题纯文本，不包含链接与 URL（用户 2026-08-01 确认，2026-08-08 扩展双源分组）。
- **时间说明**：`[MM-DD HH:mm]` 为**北京时间**，来自 RSS 的 pubDate——即 **Google News 收录时间**，与原文发布时刻误差分钟级（Reuters 官网 DataDome 反爬、Bloomberg 官网 403，均拿不到原文精确时间戳）。
- **合规**：抓取频率每小时 1 次（8 个查询），对 Google News RSS 压力极小；仅用于个人/内部信息聚合。
