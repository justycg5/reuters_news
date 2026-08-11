#!/usr/bin/env python3
"""
fetch_reuters.py - 抓取 Google News RSS（Reuters + Bloomberg 中国相关，最近 24h）并推送企业微信群机器人

数据源: Google News RSS（聚合 Reuters / Bloomberg 文章，无反爬、免费、稳定）
  主查询: site:reuters.com china when:1d + site:bloomberg.com china when:1d

为什么不用官网直抓:
  reuters.com 有 CloudFront + DataDome（HTTP 401）；bloomberg.com 反爬（HTTP 403）。
  直抓均不可行；Google News RSS 为标准 XML，实测 200，来源 93%+ 为对应媒体。

运行环境: GitHub Actions (ubuntu-latest)，或本机（需能访问海外站点/挂代理）

用法:
    WECOM_WEBHOOK_KEY=*** python fetch_reuters.py
    本机走代理:  curl -x http://127.0.0.1:7890 ... 或设置 HTTPS_PROXY 环境变量

行为:
    1. 请求 Google News RSS 多来源多查询（Reuters/Bloomberg × china/beijing/taiwan/hong kong），合并去重
    2. 解析标准 RSS XML（标题/链接/来源/发布时间）
    3. 过滤: 标题关键词（子串）或中国公司名（词边界）命中 + 24 小时时间校验
    4. 去重: 读取 last_sent.json，跳过已推送条目
    5. 推送: 纯文本消息 POST 企业微信 webhook；按来源分组（组间分隔行，组内时间倒序），
       字节超限自动拆多条；无新条目时推送“暂无新消息”占位消息
    6. 保存 last_sent.json（最近 200 条，供下次去重）

错误处理（2026-08-08 新增，2026-08-09 加重试）:
    - 单查询失败: 告警跳过，**自动重试 1 次（间隔 5 秒）**；重试后仍失败的摘要作为附注附加在当轮消息末尾（或 idle 消息内）
    - 全部查询失败（重试后仍失败）: 推送独立错误消息（⚠️ 前缀）
    - 推送失败: 重试 1 次，仍失败则推送错误消息
    - 未捕获异常: 推送错误消息（类型+信息，不推完整堆栈）
    - key 缺失/无效: 无法推送（webhook 本身不可用），靠 GitHub Actions 红叉兜底
"""

import json
import argparse
import os
import re
import sys
import time
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests

# 多来源 × 多主题查询（Google News RSS 单查询最多 100 条且按相关性排序，
# 24h 内相关报道超 100 条时会漏，故多查询合并提升覆盖）。
# dict 顺序即推送分组顺序（Reuters 在前，Bloomberg 在后）。
SOURCES = {
    "Reuters": [
        "https://news.google.com/rss/search"
        "?q=site%3Areuters.com%20china%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Areuters.com%20beijing%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Areuters.com%20taiwan%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Areuters.com%20%22hong%20kong%22%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
    ],
    "Bloomberg": [
        "https://news.google.com/rss/search"
        "?q=site%3Abloomberg.com%20china%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Abloomberg.com%20beijing%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Abloomberg.com%20taiwan%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Abloomberg.com%20%22hong%20kong%22%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
    ],
}
KEYWORDS = [
    "china", "chinese", "beijing", "hong kong", "taiwan",
    "xi jinping", "us-china", "sino-", "shanghai", "shenzhen",
]

# 中国公司名单（词边界匹配）：美股中概 + 港股巨头 + A 股巨头 + 非上市重要公司。
# 甄别原则: 避开强歧义词（abc=农行/ABC News、jd=京东/人名 JD Vance、poly、greenland、
# honor=荣耀/普通词"荣誉"等）；词边界 \b 确保独立成词才命中（nio 不撞 junio、gree 不撞 Greece）。
COMPANY_NAMES = [
    # --- 美股中概 ---
    "alibaba", "pinduoduo", "jd.com", "baidu", "netease", "bilibili",
    "kuaishou", "trip.com", "nio", "xpeng", "li auto", "didi", "weibo",
    "iqiyi", "shein", "temu", "tiktok", "bytedance", "zhihu",
    "full truck alliance", "yum china", "autohome", "zto express",
    # --- 港股巨头 ---
    "tencent", "meituan", "xiaomi", "byd", "geely", "smic", "ping an",
    "cmb", "icbc", "ccb", "boc", "lenovo", "great wall motor",
    "nongfu spring", "country garden", "evergrande", "vanke", "sunac",
    "citic", "cnooc", "petrochina", "sinopec", "ant group",
    "anta", "li ning", "yili", "mengniu", "hkex", "haidilao",
    # --- A 股巨头 ---
    "moutai", "catl", "midea", "gree", "haier",
    "sany", "wuliangye", "zijin", "yangtze power", "longi", "sungrow",
    "trina", "ja solar", "gcl", "huaneng", "shenhua", "baowu", "chalco",
    # --- 非上市巨头与重要公司 ---
    "huawei", "oppo", "vivo", "dji", "mihoyo", "hoyoverse", "moonshot",
    "deepseek", "zhipu", "baichuan", "minimax", "xiaohongshu", "rednote",
    "wahaha", "state grid", "sinochem",
    # --- 其他知名/科技/制造 ---
    "hikvision", "zte", "wuxi", "hua hong", "yangtze memory", "cxmt",
    "foxconn", "hon hai", "changan", "saic", "gwm", "chery", "xtep",
]

_COMPANY_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in COMPANY_NAMES) + r")\b",
    re.IGNORECASE,
)


def company_hit(title: str) -> bool:
    """标题是否含中国公司名（词边界匹配，独立成词才命中）。"""
    return bool(_COMPANY_RE.search(title))
STATE_FILE = "last_sent.json"  # 正式链去重状态（原有）
STATE_FILE_ENGINE = "last_sent_engine.json"  # 引擎预览链去重状态（独立，测试群验证用）
MAX_STATE = 200  # 状态文件保留最近条数
DUMP_DIR = "data"  # --dump 原始数据落盘目录（jsonl，按天分文件）

# ---- 预过滤引擎（Phase 3：默认启用；--legacy 一键回退布尔过滤） ----
# 引擎部署在仓库 prefilter/ 子目录（与开发版 news-investment-terminal/prefilter 同步，改后需复制）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_BASE_DIR, "prefilter"))
_PREFILTER_OK = False
_PREFILTER_CFG = None
_PREFILTER_CALIB = None
try:
    import prefilter_engine as pe
    _PREFILTER_CFG = pe.load_configs(os.path.join(_BASE_DIR, "prefilter"))
    _calib_path = os.path.join(_BASE_DIR, "prefilter", "calib.json")
    if os.path.exists(_calib_path):
        with open(_calib_path, encoding="utf-8") as _f:
            _PREFILTER_CALIB = json.load(_f)
    _PREFILTER_OK = True
    print("Prefilter engine loaded (thresholds: tier1=%s tier2=%s)"
          % (_PREFILTER_CALIB["tier1"] if _PREFILTER_CALIB else "dynamic",
             _PREFILTER_CALIB["tier2"] if _PREFILTER_CALIB else "dynamic"))
except Exception as e:
    print(f"WARN: prefilter engine load failed ({e}); fall back to legacy boolean filter")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def webhook_url(strict: bool = True) -> str:
    """正式 webhook。strict=False 时（引擎预览模式）找不到 key 返回空串而非退出，
    便于本地只验证预览通道。"""
    # 优先级 1: 本地调试文件 .env.local（不入 git，手动创建），格式:
    #   WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=***
    # 或 WECOM_WEBHOOK_KEY=***
    try:
        with open(".env.local", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("WECOM_WEBHOOK_URL="):
                    return line.split("=", 1)[1].strip()
                if line.startswith("WECOM_WEBHOOK_KEY="):
                    k = line.split("=", 1)[1].strip()
                    return "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + k
    except OSError:
        pass
    # 优先级 2/3: 环境变量（GitHub Actions 用 WECOM_WEBHOOK_KEY）
    key = os.environ.get("WECOM_WEBHOOK_KEY", "").strip()
    full = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
    if full:
        return full
    if not key:
        if strict:
            print("FATAL: WECOM_WEBHOOK_KEY (or WECOM_WEBHOOK_URL, or .env.local) not set", file=sys.stderr)
            sys.exit(2)
        return ""
    return "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key


def webhook_url_test() -> str:
    """测试 webhook（引擎预览验证通道，--engine-preview 用）。
    优先级：环境变量 WECOM_WEBHOOK_KEY_TEST（GitHub Actions Secret）> .env.local.test（本地）。
    找不到返回空串（预览通道跳过，不影响正式链）。"""
    key = os.environ.get("WECOM_WEBHOOK_KEY_TEST", "").strip()
    if key:
        return "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key
    try:
        with open(".env.local.test", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("WECOM_WEBHOOK_KEY="):
                    k = line.split("=", 1)[1].strip()
                    if k:
                        return "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + k
    except OSError:
        pass
    return ""


def strip_source(title: str) -> str:
    """清理 Google News 标题自带的来源后缀（Reuters / Bloomberg.com / Bloomberg LEI）。"""
    return re.sub(r"\s*-\s*(?:Reuters(?: poll)?|Bloomberg(?:\.com| LEI)?)\s*$", "", title).strip()


def parse_rss(text: str) -> list:
    root = ET.fromstring(text)
    items = []
    for it in root.iter("item"):
        title = strip_source(it.findtext("title", "").strip())
        link = it.findtext("link", "").strip()
        pub = it.findtext("pubDate", "").strip()
        src = it.find("source")
        source = src.text.strip() if src is not None and src.text else ""
        if not title or not link:
            continue
        items.append({"title": title, "url": link, "published": pub, "source": source})
    return items


def within_24h(pub: str) -> bool:
    """解析 pubDate 校验是否在 24h 内；解析失败放行（RSS 的 when:1d 已限定窗口）。"""
    if not pub:
        return True
    try:
        dt = parsedate_to_datetime(pub)
    except Exception:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - dt
    return age.total_seconds() <= 24 * 3600 + 300  # 5 分钟容差


def parse_dt(pub: str):
    """解析 pubDate 为带时区的 datetime；失败返回最小时间（排序时垫底）。"""
    try:
        dt = parsedate_to_datetime(pub)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_time(pub: str) -> str:
    """GMT -> 北京时间 MM-DD HH:mm（24h 窗口跨天，带日期）；解析失败返回 '??'。"""
    if not pub:
        return "??"
    dt = parse_dt(pub)
    if dt == datetime.min.replace(tzinfo=timezone.utc):
        return "??"
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")


def keyword_hit(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in KEYWORDS)


def make_header(source_groups: list, stats: dict = None) -> str:
    """按实际出现的来源生成消息头部（单来源时只写该来源名，避免头部与内容不符）；
    stats 非空时附加引擎过滤统计行（Tier1/Tier2 条数）。"""
    names = [n for n, _ in source_groups if n]
    base = (" / ".join(names) if names else "Reuters / Bloomberg") + " 中国相关（24h）\n"
    if stats:
        base += f"（引擎过滤: Tier1 {stats['tier1']} 条 / Tier2 {stats['tier2']} 条）\n"
    return base


def push_wecom(webhook: str, source_groups: list, max_bytes: int = 1900, note: str = None, stats: dict = None) -> bool:
    """按字节预算分组推送纯文本（企业微信 text 单条上限 2048 字节，留安全余量）。
    source_groups: [(来源名, items)]，调用方已按来源排序、组内时间倒序。
    格式: 编号. [MM-DD HH:mm 北京时间] 标题（纯文本，无超链接、无 URL；
    时间为 Google News 收录时间，与原文发布时刻误差分钟级）；
    来源分组间插入 '— 来源名 —' 分隔行，编号全局连续。
    note: 部分查询失败的附注（有内容时附加在消息末尾）。
    返回 True 当且仅当所有分组推送成功。"""
    header = make_header(source_groups, stats)
    body = []
    idx = 0
    first = True
    for src_name, src_items in source_groups:
        if not src_items:
            continue
        if not first:
            body.append(f"— {src_name} —\n")
        first = False
        for it in src_items:
            idx += 1
            body.append(f"{idx}. [{fmt_time(it['published'])}] {it['title']}\n")
    if note:
        body.append(f"\n⚠️ 注：{note}，可能漏报\n")

    groups, cur, cur_len = [], [], 0
    for line in body:
        size = len(line.encode("utf-8"))
        if cur and cur_len + size > max_bytes:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += size
    if cur:
        groups.append(cur)

    ok = True
    for i, g in enumerate(groups, 1):
        content = (header + "".join(g)).rstrip("\n")  # 去掉尾部换行，避免翻译工具报"异常换行"
        payload = {"msgtype": "text", "text": {"content": content}}
        print(f"Pushing group {i}/{len(groups)} ({len(g)} items, {len(content.encode('utf-8'))} bytes)")
        try:
            r = requests.post(webhook, json=payload, timeout=15)
            data = r.json()
        except Exception as e:
            print(f"ERROR: webhook request failed: {e}")
            ok = False
            continue
        if data.get("errcode") != 0:
            print(f"ERROR: webhook errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
            ok = False
    return ok


def push_idle(webhook: str, note: str = None) -> bool:
    """无新条目时发送占位消息（纯文本）；有部分失败时改为失败提示。"""
    if note:
        content = f"Reuters / Bloomberg 中国相关（24h）\n⚠️ {note}，暂无新消息或存在漏报"
    else:
        content = "Reuters / Bloomberg 中国相关（24h）\n暂无新消息"
    payload = {"msgtype": "text", "text": {"content": content}}
    try:
        r = requests.post(webhook, json=payload, timeout=15)
        data = r.json()
    except Exception as e:
        print(f"ERROR: idle push failed: {e}")
        return False
    if data.get("errcode") != 0:
        print(f"ERROR: webhook errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
        return False
    return True


def push_error(webhook: str, err_type: str, detail: str) -> bool:
    """推送独立错误消息（⚠️ 前缀纯文本），用于全部失败/推送失败/未捕获异常。"""
    content = f"⚠️ {err_type}\n\n{detail}"
    payload = {"msgtype": "text", "text": {"content": content}}
    print(f"Pushing error notice ({len(content.encode('utf-8'))} bytes)")
    try:
        r = requests.post(webhook, json=payload, timeout=15)
        data = r.json()
    except Exception as e:
        print(f"ERROR: error-notice push failed: {e}")
        return False
    if data.get("errcode") != 0:
        print(f"ERROR: webhook errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
        return False
    return True


def push_with_retry(webhook: str, source_groups: list, note: str = None, stats: dict = None) -> bool:
    """推送失败重试 1 次（间隔 3 秒），缓解偶发网络抖动。"""
    if push_wecom(webhook, source_groups, note=note, stats=stats):
        return True
    print("push failed, retrying once after 3s...")
    time.sleep(3)
    return push_wecom(webhook, source_groups, note=note, stats=stats)


def fail_summary(failures: list, total: int) -> str:
    """生成失败查询摘要（含来源与主题，最多列 4 个）。"""
    fcnt = len(failures)
    parts = []
    for src, q, _ in failures[:4]:
        m = re.search(r"[?&]q=([^&]+)", q)
        topic = urllib.parse.unquote(m.group(1))[:36] if m else "?"
        parts.append(f"{src} {topic}")
    tail = f" 等{fcnt}个" if fcnt > 4 else ""
    return f"{fcnt}/{total} 查询失败：{'、'.join(parts)}{tail}"


def _fetch_query(src: str, q: str, seen_url: set, seen_title: set, merged: list, failures: list) -> None:
    """拉取单个查询并合并去重；失败记录到 failures（调用方决定是否重试）。"""
    print(f"Fetching [{src}] {q}")
    try:
        resp = requests.get(q, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        msg = f"{type(e).__name__}: {str(e)[:120]}"
        print(f"WARN: fetch failed: {msg}")
        failures.append((src, q, msg))
        return
    try:
        items = parse_rss(resp.text)
    except ET.ParseError as e:
        msg = f"ParseError: {e}"
        print(f"WARN: RSS parse failed: {msg}")
        failures.append((src, q, msg))
        return
    print(f"  -> {len(items)} items")
    for it in items:
        k_url = it["url"]
        k_title = it["title"].lower().strip()
        if k_url in seen_url or k_title in seen_title:
            continue
        seen_url.add(k_url)
        seen_title.add(k_title)
        it["source"] = src
        merged.append(it)


def fetch_all() -> tuple:
    """拉取全部来源×查询并合并去重（URL 去重 + 标题兜底去重），条目打 source 来源标签。
    返回 (items, failures)：failures 为 [(来源名, 查询URL, 异常摘要), ...]。
    失败查询自动重试 1 次（间隔 5 秒），缓解 Google News 对云 IP 段瞬时风控（503）导致的假失败。"""
    seen_url, seen_title, merged = set(), set(), []
    failures = []
    for src, queries in SOURCES.items():
        for q in queries:
            _fetch_query(src, q, seen_url, seen_title, merged, failures)
    if failures:
        print(f"Retrying {len(failures)} failed query(ies) once after 5s...")
        time.sleep(5)
        retried = []
        for src, q, _ in failures:
            _fetch_query(src, q, seen_url, seen_title, merged, retried)
        failures = retried
    return merged, failures


def load_state(path: str = STATE_FILE) -> list:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_state(urls, path: str = STATE_FILE) -> None:
    urls = list(urls)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(urls[-MAX_STATE:], f, ensure_ascii=False)


def engine_filter(items: list, thresholds: dict = None) -> tuple:
    """预过滤引擎评分过滤：返回 (filtered, stats, results_by_title)。
    filtered 为 Tier1+2 且非黑名单的条目（已附加 tier/assets/engine_score 标签）；
    results_by_title 为全量评分结果（供 dump 落盘）；thresholds 为固化阈值（None 时当批分位数）。"""
    results = pe.score_all([{"title": it["title"]} for it in items],
                           _PREFILTER_CFG, thresholds=thresholds)
    push_res = pe.filter_for_push(results, (1, 2))
    push_titles = {r["title"] for r in push_res}
    filtered = []
    for it, r in zip(items, results):
        if it["title"] in push_titles:
            it["tier"] = r["tier"]
            it["assets"] = r["assets"]
            it["engine_score"] = r["score"]
            filtered.append(it)
    stats = {
        "tier1": sum(1 for r in push_res if r["tier"] == 1),
        "tier2": sum(1 for r in push_res if r["tier"] == 2),
        "blacklisted": sum(1 for r in results if r["blacklisted"]),
    }
    return filtered, stats, {r["title"]: r for r in results}


def preview_push(items: list, note: str = None) -> bool:
    """引擎预览验证通道：引擎过滤结果推送到测试 webhook（独立状态 last_sent_engine.json）。
    与正式链完全隔离（独立去重/独立状态），失败只 WARN 不影响正式链退出码。
    无测试 key / 引擎未加载 / 无新条时静默跳过（不推 idle，避免刷屏测试群）。"""
    webhook = webhook_url_test()
    if not webhook:
        print("WARN: engine-preview skipped: no test webhook key "
              "(set WECOM_WEBHOOK_KEY_TEST or .env.local.test)")
        return True
    if not _PREFILTER_OK:
        print("WARN: engine-preview skipped: prefilter engine load failed")
        return True

    fresh_items = [it for it in items if within_24h(it["published"])]
    filtered, stats, _ = engine_filter(fresh_items, _PREFILTER_CALIB)
    old = set(load_state(STATE_FILE_ENGINE))
    fresh = [it for it in filtered if it["url"] not in old]
    print(f"Engine preview: {len(filtered)} filtered, {len(fresh)} new for test group "
          f"(tier1={stats['tier1']}, tier2={stats['tier2']})")
    if not fresh:
        print("Engine preview: no new items, skip push")
        return True

    source_groups = []
    for src in SOURCES:
        sub = [it for it in fresh if it["source"] == src]
        if sub:
            sub.sort(key=lambda it: parse_dt(it["published"]), reverse=True)
            source_groups.append((src, sub))

    if not push_with_retry(webhook, source_groups, note=note, stats=stats):
        print("WARN: engine-preview push failed (retried once); formal chain unaffected")
        return False
    save_state(old | {it["url"] for it in fresh}, STATE_FILE_ENGINE)
    print(f"Engine preview: pushed {len(fresh)} items to test webhook, state saved")
    return True


def dump_items(items: list, results: dict = None) -> None:
    """把当批原始条目（含未过滤的）追加落盘到 data/dump-YYYY-MM-DD.jsonl。
    每条含过滤标记（keyword_hit / company_hit / within_24h / passed），
    供 Phase 2 阈值校准与评估使用。results（引擎评分 title->result）非空时附加引擎字段。
    按北京时间日期分文件，重复运行追加。"""
    os.makedirs(DUMP_DIR, exist_ok=True)
    day = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    path = os.path.join(DUMP_DIR, f"dump-{day}.jsonl")
    ts = datetime.now(timezone.utc).isoformat()
    n = 0
    with open(path, "a", encoding="utf-8") as f:
        for it in items:
            kw = keyword_hit(it["title"])
            co = company_hit(it["title"])
            win = within_24h(it["published"])
            rec = {
                "ts": ts,
                "source": it["source"],
                "title": it["title"],
                "url": it["url"],
                "published": it["published"],
                "keyword_hit": kw,
                "company_hit": co,
                "within_24h": win,
                "passed": (kw or co) and win,
            }
            if results is not None:
                r = results.get(it["title"])
                rec["engine_score"] = r["score"] if r else None
                rec["tier"] = r["tier"] if r else None
                rec["assets"] = r["assets"] if r else []
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(f"Dumped {n} items to {path}")


def _run(dump: bool = False, dump_only: bool = False, engine_preview: bool = False) -> int:
    total_queries = sum(len(v) for v in SOURCES.values())
    items, failures = fetch_all()
    note = fail_summary(failures, total_queries) if failures else None
    if failures:
        print(f"WARN: {note}")

    if not items:
        print("FATAL: all queries failed")
        detail = ("全部查询失败（数据源或网络故障）:\n"
                  + "\n".join(f"- [{s}] {m}" for s, _, m in failures[:8])
                  + "\n建议: 查看 GitHub Actions 日志")
        push_error(webhook_url(), "Reuters / Bloomberg 推送任务异常", detail)
        return 1
    print(f"Merged {len(items)} raw items (deduped)")

    # 主链（原有链条不动）：布尔过滤 = 关键词子串 or 公司名词边界 + 24h
    filtered = [it for it in items
                if (keyword_hit(it["title"]) or company_hit(it["title"]))
                and within_24h(it["published"])]
    print(f"After filter: {len(filtered)} items")

    if dump or dump_only:
        # dump 落盘：引擎可用时附带评分字段（供校准/评估，不改变主链行为）
        engine_results = None
        if _PREFILTER_OK:
            fresh_all = [it for it in items if within_24h(it["published"])]
            engine_results = {r["title"]: r for r in
                              pe.score_all([{"title": it["title"]} for it in fresh_all],
                                           _PREFILTER_CFG, thresholds=_PREFILTER_CALIB)}
        dump_items(items, results=engine_results)
    if dump_only:
        print(f"Dump-only mode: {len(items)} raw, {len(filtered)} would pass filter; no push")
        return 0

    # 去重
    old = set(load_state())
    fresh = [it for it in filtered if it["url"] not in old]
    print(f"New items: {len(fresh)}")

    # 正式 webhook：preview 模式下允许缺失（本地只验预览通道时跳过正式链）
    main_webhook = webhook_url(strict=not engine_preview)
    if not main_webhook:
        print("WARN: no formal webhook key; skip formal chain push (engine-preview mode)")
        if engine_preview:
            preview_push(items, note=note)
        return 0

    if not fresh:
        print("No new items, send idle notice")
        ok = push_idle(main_webhook, note=note)
        if engine_preview:
            preview_push(items, note=note)
        return 0 if ok else 1

    # 按来源分组（保持 SOURCES 固定顺序），组内时间倒序（最新在前）
    source_groups = []
    for src in SOURCES:
        sub = [it for it in fresh if it["source"] == src]
        if sub:
            sub.sort(key=lambda it: parse_dt(it["published"]), reverse=True)
            source_groups.append((src, sub))

    if not push_with_retry(main_webhook, source_groups, note=note):
        push_error(main_webhook, "Reuters / Bloomberg 推送任务异常",
                   "推送失败（重试 1 次后仍失败）\n建议: 查看 GitHub Actions 日志确认 webhook 状态")
        return 1

    save_state(old | {it["url"] for it in fresh})
    print(f"Pushed {len(fresh)} items to WeCom, state saved")

    # 引擎预览验证通道（可选）：引擎过滤结果推测试 webhook，与正式链完全隔离
    if engine_preview:
        preview_push(items, note=note)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reuters/Bloomberg 中国相关新闻抓取推送")
    parser.add_argument("--dump", action="store_true",
                        help="正常流程外，额外把原始条目落盘到 data/（jsonl，按天分文件）")
    parser.add_argument("--dump-only", action="store_true",
                        help="只抓取 + 落盘，不推送（本机采集数据用，供阈值校准）")
    parser.add_argument("--engine-preview", action="store_true",
                        help="并行验证：正式链维持布尔过滤推正式群，另将引擎过滤结果推测试群（独立去重）")
    args = parser.parse_args()
    try:
        return _run(dump=args.dump, dump_only=args.dump_only, engine_preview=args.engine_preview)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"UNCAUGHT ERROR:\n{tb}")
        detail = f"未预期异常: {type(e).__name__}: {e}\n建议: 查看 GitHub Actions 日志"
        try:
            push_error(webhook_url(), "Reuters / Bloomberg 推送任务异常", detail)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
