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
    3. 过滤: 标题关键词命中 + 24 小时时间校验
    4. 去重: 读取 last_sent.json，跳过已推送条目
    5. 推送: 纯文本消息 POST 企业微信 webhook；按来源分组（组间分隔行，组内时间倒序），
       字节超限自动拆多条；无新条目时推送“暂无新消息”占位消息
    6. 保存 last_sent.json（最近 200 条，供下次去重）

错误处理（2026-08-08 新增）:
    - 单查询失败: 告警跳过，失败摘要作为附注附加在当轮消息末尾（或 idle 消息内）
    - 全部查询失败: 推送独立错误消息（⚠️ 前缀）
    - 推送失败: 重试 1 次，仍失败则推送错误消息
    - 未捕获异常: 推送错误消息（类型+信息，不推完整堆栈）
    - key 缺失/无效: 无法推送（webhook 本身不可用），靠 GitHub Actions 红叉兜底
"""

import json
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
STATE_FILE = "last_sent.json"
MAX_STATE = 200  # 状态文件保留最近条数

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def webhook_url() -> str:
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
        print("FATAL: WECOM_WEBHOOK_KEY (or WECOM_WEBHOOK_URL, or .env.local) not set", file=sys.stderr)
        sys.exit(2)
    return "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key


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


def make_header(source_groups: list) -> str:
    """按实际出现的来源生成消息头部（单来源时只写该来源名，避免头部与内容不符）。"""
    names = [n for n, _ in source_groups if n]
    if not names:
        return "Reuters / Bloomberg 中国相关（24h）\n"
    return " / ".join(names) + " 中国相关（24h）\n"


def push_wecom(webhook: str, source_groups: list, max_bytes: int = 1900, note: str = None) -> bool:
    """按字节预算分组推送纯文本（企业微信 text 单条上限 2048 字节，留安全余量）。
    source_groups: [(来源名, items)]，调用方已按来源排序、组内时间倒序。
    格式: 编号. [MM-DD HH:mm 北京时间] 标题（纯文本，无超链接、无 URL；
    时间为 Google News 收录时间，与原文发布时刻误差分钟级）；
    来源分组间插入 '— 来源名 —' 分隔行，编号全局连续。
    note: 部分查询失败的附注（有内容时附加在消息末尾）。
    返回 True 当且仅当所有分组推送成功。"""
    header = make_header(source_groups)
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


def push_with_retry(webhook: str, source_groups: list, note: str = None) -> bool:
    """推送失败重试 1 次（间隔 3 秒），缓解偶发网络抖动。"""
    if push_wecom(webhook, source_groups, note=note):
        return True
    print("push failed, retrying once after 3s...")
    time.sleep(3)
    return push_wecom(webhook, source_groups, note=note)


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


def fetch_all() -> tuple:
    """拉取全部来源×查询并合并去重（URL 去重 + 标题兜底去重），条目打 source 来源标签。
    返回 (items, failures)：failures 为 [(来源名, 查询URL, 异常摘要), ...]，单个查询失败仅告警跳过。"""
    seen_url, seen_title, merged = set(), set(), []
    failures = []
    for src, queries in SOURCES.items():
        for q in queries:
            print(f"Fetching [{src}] {q}")
            try:
                resp = requests.get(q, headers=HEADERS, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as e:
                msg = f"{type(e).__name__}: {str(e)[:120]}"
                print(f"WARN: fetch failed: {msg}")
                failures.append((src, q, msg))
                continue
            try:
                items = parse_rss(resp.text)
            except ET.ParseError as e:
                msg = f"ParseError: {e}"
                print(f"WARN: RSS parse failed: {msg}")
                failures.append((src, q, msg))
                continue
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
    return merged, failures


def load_state() -> list:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_state(urls) -> None:
    urls = list(urls)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(urls[-MAX_STATE:], f, ensure_ascii=False)


def _run() -> int:
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

    # 关键词 + 时间过滤
    filtered = [it for it in items if keyword_hit(it["title"]) and within_24h(it["published"])]
    print(f"After filter: {len(filtered)} items")

    # 去重
    old = set(load_state())
    fresh = [it for it in filtered if it["url"] not in old]
    print(f"New items: {len(fresh)}")

    if not fresh:
        print("No new items, send idle notice")
        return 0 if push_idle(webhook_url(), note=note) else 1

    # 按来源分组（保持 SOURCES 固定顺序），组内时间倒序（最新在前）
    source_groups = []
    for src in SOURCES:
        sub = [it for it in fresh if it["source"] == src]
        if sub:
            sub.sort(key=lambda it: parse_dt(it["published"]), reverse=True)
            source_groups.append((src, sub))

    if not push_with_retry(webhook_url(), source_groups, note=note):
        push_error(webhook_url(), "Reuters / Bloomberg 推送任务异常",
                   "推送失败（重试 1 次后仍失败）\n建议: 查看 GitHub Actions 日志确认 webhook 状态")
        return 1

    save_state(old | {it["url"] for it in fresh})
    print(f"Pushed {len(fresh)} items to WeCom, state saved")
    return 0


def main() -> int:
    try:
        return _run()
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
