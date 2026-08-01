#!/usr/bin/env python3
"""
fetch_reuters.py - 抓取 Google News RSS（Reuters 中国相关，最近 24h）并推送到企业微信群机器人

数据源: Google News RSS（聚合 Reuters 文章，无反爬、免费、稳定）
  https://news.google.com/rss/search?q=site:reuters.com+china+when:1d&hl=en-US&gl=US&ceid=US:en

为什么不用 reuters.com 直抓:
  reuters.com 部署了 CloudFront + DataDome 反爬，脚本请求返回 HTTP 401，
  直抓不可行；Google News RSS 为标准 XML，实测 200、100% Reuters 来源。

运行环境: GitHub Actions (ubuntu-latest)，或本机（需能访问海外站点/挂代理）

用法:
    WECOM_WEBHOOK_KEY=*** python fetch_reuters.py
    本机走代理:  curl -x http://127.0.0.1:7890 ... 或设置 HTTPS_PROXY 环境变量

行为:
    1. 请求 Google News RSS 主查询 + 补充主题查询（beijing/taiwan/hong kong），合并去重
    2. 解析标准 RSS XML（标题/链接/来源/发布时间）
    3. 过滤: 标题关键词命中 + 24 小时时间校验
    4. 去重: 读取 last_sent.json，跳过已推送条目
    5. 推送: 组装 markdown 消息 POST 企业微信 webhook；有新条目时分组推送，
       无新条目时推送“暂无新消息”占位消息
    6. 保存 last_sent.json（最近 200 条，供下次去重）
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

# 主查询 + 补充主题查询（Google News RSS 单查询最多 100 条且按相关性排序，
# 24h 内 Reuters 中国报道超 100 条时会漏，故用多查询合并提升覆盖）。
QUERIES = [
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
]
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
    #   WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
    # 或 WECOM_WEBHOOK_KEY=xxx
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
    """清理 Google News 标题自带的 ' - Reuters' 来源后缀。"""
    return re.sub(r"\s*-\s*Reuters\s*$", "", title).strip()


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


def keyword_hit(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in KEYWORDS)


def push_wecom(webhook: str, items: list, max_bytes: int = 1900) -> bool:
    """按字节预算分组推送纯文本（企业微信 text 单条上限 2048 字节，留安全余量）。
    格式: 编号. 标题（纯文本，无超链接、无 URL）。
    返回 True 当且仅当所有分组推送成功。"""
    header = "Reuters 中国相关（24h）\n"
    groups, cur, cur_len = [], [], 0
    for i, it in enumerate(items, 1):
        line = f"{i}. {it['title']}\n"
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


def push_idle(webhook: str) -> bool:
    """无新条目时发送占位消息（纯文本）。"""
    payload = {"msgtype": "text", "text": {"content": "Reuters 中国相关（24h）\n暂无新消息"}}
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


def fetch_all() -> list:
    """拉取全部查询并合并去重（URL 去重 + 标题兜底去重）。单个查询失败仅告警跳过。"""
    seen_url, seen_title, merged = set(), set(), []
    for q in QUERIES:
        print(f"Fetching {q}")
        try:
            resp = requests.get(q, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"WARN: fetch failed: {e}")
            continue
        try:
            items = parse_rss(resp.text)
        except ET.ParseError as e:
            print(f"WARN: RSS parse failed: {e}")
            continue
        print(f"  -> {len(items)} items")
        for it in items:
            k_url = it["url"]
            k_title = it["title"].lower().strip()
            if k_url in seen_url or k_title in seen_title:
                continue
            seen_url.add(k_url)
            seen_title.add(k_title)
            merged.append(it)
    return merged


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


def main() -> int:
    items = fetch_all()
    if not items:
        print("FATAL: all queries failed")
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
        return 0 if push_idle(webhook_url()) else 1

    if not push_wecom(webhook_url(), fresh):
        return 1

    save_state(old | {it["url"] for it in fresh})
    print(f"Pushed {len(fresh)} items to WeCom, state saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
