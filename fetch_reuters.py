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

# 美政策源查询组（纯美政策新闻，标题不带 China 字眼也能抓到）：
# 覆盖 Fed 货币政策 / 关税 / 制裁 / 芯片管制 / 军工黑名单 / 出口管制。
# 注意：Google News RSS 不支持括号 OR 组合语法（实测 228 条仅 2% 相关，降级为 site 泛搜索），
# 因此每个主题用单词/词组独立查询（Google 自动词形变化：sanction 会匹配 sanctions）。
# 2026-08-17 补：blacklist（DJI/WuXi 军工黑名单类）+ export control（出口管制类），
# 覆盖“纯美方视角标题”（无 China 字眼）的制裁/名单新闻；military/pentagon 不加（泛词噪音）。
# 由 --us-policy 开关启用（默认关闭），云端 workflow 不传参数时行为不变。
US_POLICY_SOURCES = {
    "US Policy": [
        "https://news.google.com/rss/search"
        "?q=site%3Areuters.com%20%22federal%20reserve%22%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Areuters.com%20tariff%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Areuters.com%20sanction%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Areuters.com%20semiconductor%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Areuters.com%20blacklist%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Areuters.com%20%22export%20control%22%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Abloomberg.com%20%22federal%20reserve%22%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Abloomberg.com%20tariff%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Abloomberg.com%20sanction%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Abloomberg.com%20semiconductor%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Abloomberg.com%20blacklist%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search"
        "?q=site%3Abloomberg.com%20%22export%20control%22%20when%3A1d"
        "&hl=en-US&gl=US&ceid=US:en",
    ],
}

# 矿产源（2026-08-17 引入）：Mining.com 矿业专站 RSS（无鉴权 GET 接口）。
# 背景：Google News RSS 对矿种词全文匹配命中站内 ticker 页/公司页（单词 100 条全是股票页、
# 词组 0 条），抓不到矿产新闻（与 US Policy 同根因）。Mining.com 是矿业专站，
# 返回纯矿产新闻（金价/铜价/锂/铀等），标准 RSS 2.0 可复用 parse_rss。
MINERAL_SOURCES = {
    "Mining.com": [
        "https://www.mining.com/feed/",
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


# 美政策『暗含中国产业影响』规则（2026-08-16 用户需求）：
# 部分美国政策新闻标题无中国词/公司名，但实质影响中国优势产业
# （如美国对光纤/无人机/光伏/钢铁加征关税，中国是这些产业的全球最大供应方）。
# 判定 = 美政策主题词 AND 中国优势产业词，两者都在标题中命中才放行。
# 实测：dump 2400+ 条中此类新闻约 4 条（drone tariffs 等），量小精准；
# 产业词用词边界，避免子串误匹配（如 "ai " 撞 "Dubai"）。
TOPIC_RE = re.compile(
    r"\b(?:tariff\w*|sanction\w*|blacklist\w*|entity list\w*|covered list\w*)\b"
    # 进出口管制（2026-08-17 优化：原 "export control" 短语在真实标题中 0 命中，改为动词+对象变体）
    r"|\b(?:export|import)\w*\s+(?:curb\w*|ban\w*|restrict\w*|control\w*|limit\w*)\b"
    r"|\b(?:curb\w*|restrict\w*|ban\w*|limit\w*|block\w*|halt\w*|bar\w*|tighten\w*)"
    r"\w*\s+(?:on\s+)?(?:export\w*|import\w*|shipment\w*|sale\w*|transfer\w*)\b"
    # 关税 duty 变体（2026-08-17：duty→duties 不规则复数，用 dut(?:y|ies)；
    # 仅收「修饰词+duty」形式，避免 "return to duty" 职责义误伤）
    r"|\b(?:import|anti-?dumping|countervailing|customs)\s+dut(?:y|ies)\b"
    # FCC 互认协议/覆盖清单（2026-08-17）："covered list" 为 FCC 官方术语（覆盖清单），
    # "mutual recognition"/non-MRA 为互认协议措辞，不依赖标题是否点名中国
    r"|\b(?:mutual recognition|non[- ]?mra)\b",
    re.IGNORECASE,
)
INDUSTRY_RE = re.compile(
    r"\b(?:optical\w*|fiber\w*|fibre\w*|telecom\w*|5g|semiconductor\w*|chip\w*|solar\w*|polysilicon\w*|"
    r"steel\w*|aluminum\w*|aluminium\w*|rare[- ]earth|electric vehicle\w*|battery|batteries|lithium\w*|"
    r"drone\w*|uav\w*|wind turbine\w*|shipbuilding\w*|textile\w*|artificial intelligence|data center\w*)\b",
    re.IGNORECASE,
)


def implicit_china_hit(title: str) -> bool:
    """标题无中国词/公司名时，是否暗含中国产业影响（主题词 AND 产业词）。"""
    return bool(TOPIC_RE.search(title) and INDUSTRY_RE.search(title))


# 预览链需求校验词表（2026-08-16 用户定案：三条需求校验只针对预览链）：
# 需求 3（美国影响中国）的政策词。Google News RSS 为全文匹配，引擎会误评高分噪音
# （"Zelenskyy Says Ukraine Strikes…"、"Peru's Economy…" 等标题无关条目，
# 实测 US Policy 源 136 条中 16 条被误评 Tier1/2）。
# 推送前要求标题命中：美政策词（本正则）或中国词或公司名，否则丢弃。
# 含 fed/federal reserve（Fed 货币政策属需求 3 明确列举）；
# 不含产业词（如 chips），避免 "Watch Modi Puts Chips…" 这类误放行。
US_POLICY_PREVIEW_RE = re.compile(
    r"\b(?:tariff\w*|sanction\w*|blacklist\w*|entity list\w*|covered list\w*|fed\b|federal reserve)\b"
    # 进出口管制（2026-08-17 优化：原 "export control" 短语 0 命中，改为动词+对象变体）
    r"|\b(?:export|import)\w*\s+(?:curb\w*|ban\w*|restrict\w*|control\w*|limit\w*)\b"
    r"|\b(?:curb\w*|restrict\w*|ban\w*|limit\w*|block\w*|halt\w*|bar\w*|tighten\w*)"
    r"\w*\s+(?:on\s+)?(?:export\w*|import\w*|shipment\w*|sale\w*|transfer\w*)\b"
    # 关税 duty 变体（2026-08-17：duty→duties 不规则复数，用 dut(?:y|ies)；
    # 仅收「修饰词+duty」形式，避免 "return to duty" 职责义误伤）
    r"|\b(?:import|anti-?dumping|countervailing|customs)\s+dut(?:y|ies)\b"
    # FCC 互认协议/覆盖清单（2026-08-17）："covered list" 为 FCC 官方术语（覆盖清单），
    # "mutual recognition"/non-MRA 为互认协议措辞，不依赖标题是否点名中国
    r"|\b(?:mutual recognition|non[- ]?mra)\b",
    re.IGNORECASE,
)
# 矿产词表（2026-08-17 引入 Mining.com 矿产源）：预览链需求校验用，
# 覆盖“世界重大矿产事件”（贵金属/工业金属/电池金属/稀土/关键矿产/煤炭）。
# 词边界匹配，避开歧义词（lead=铅/领导、steel=钢铁成品均不收）。
MINERAL_RE = re.compile(
    r"\b(?:copper|lithium|nickel|cobalt|zinc|tin|silver|aluminum|aluminium|"
    r"uranium|platinum|palladium|manganese|graphite|bauxite|tungsten|antimony|"
    r"gallium|germanium|molybdenum|indium|gold|coal|bullion|mining|miners?)\b"
    r"|rare[- ]earth|iron ore|critical minerals?",
    re.IGNORECASE,
)
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


def _build_webhook_url(v: str) -> str:
    """把 Secret/环境变量值规范化为完整 webhook URL：兼容三种填法。
    1) 完整 URL: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
    2) 带前缀:   key=xxx
    3) 裸 key:   xxx
    """
    v = (v or "").strip()
    if not v:
        return ""
    if v.startswith("http://") or v.startswith("https://"):
        return v
    if v.startswith("key="):
        v = v[4:].strip()
    if not v:
        return ""
    return "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + v


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
                    return _build_webhook_url(k)
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
    return _build_webhook_url(key)


def webhook_url_test() -> str:
    """测试 webhook（引擎预览验证通道，--engine-preview 用）。
    优先级：环境变量 WECOM_WEBHOOK_KEY_TEST（GitHub Actions Secret）> .env.local.test（本地）。
    找不到返回空串（预览通道跳过，不影响正式链）。"""
    key = os.environ.get("WECOM_WEBHOOK_KEY_TEST", "").strip()
    if key:
        return _build_webhook_url(key)
    try:
        with open(".env.local.test", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("WECOM_WEBHOOK_KEY="):
                    k = line.split("=", 1)[1].strip()
                    if k:
                        return _build_webhook_url(k)
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
        base += f"（引擎过滤: Tier1 {stats['tier1']} 条 / Tier2 {stats['tier2']} 条"
        if stats.get("new_count") is not None:
            base += f"，本次新增 {stats['new_count']} 条"
        base += "）\n"
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


def fetch_all(include_us_policy: bool = False) -> tuple:
    """拉取全部来源×查询并合并去重（URL 去重 + 标题兜底去重），条目打 source 来源标签。
    返回 (items, failures)：failures 为 [(来源名, 查询URL, 异常摘要), ...]。
    失败查询自动重试 1 次（间隔 5 秒），缓解 Google News 对云 IP 段瞬时风控（503）导致的假失败。"""
    sources = {**SOURCES, **US_POLICY_SOURCES, **MINERAL_SOURCES} if include_us_policy else SOURCES
    seen_url, seen_title, merged = set(), set(), []
    failures = []
    for src, queries in sources.items():
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


def load_state(path: str = STATE_FILE):
    """返回 (urls: set, titles: set)。兼容旧格式（纯 URL 字符串列表）。
    双键去重：Google News 对同一报道会生成多个不同跳转 URL（实测 78/4347 标题存在多 URL 变体），
    只按 URL 记会导致跨批重推；标题键兜底拦截同文不同 URL。"""
    urls, titles = set(), set()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for x in json.load(f):
                    if isinstance(x, dict):
                        if x.get("u"):
                            urls.add(x["u"])
                        if x.get("t"):
                            titles.add(x["t"])
                    elif isinstance(x, str) and x:
                        urls.add(x)
        except Exception:
            pass
    return urls, titles


def save_state(urls, titles, path: str = STATE_FILE) -> None:
    """保存去重状态（URL + 标题双键）。旧格式纯 URL 列表自动升级为 dict 条目。"""
    entries = [{"u": u} for u in list(urls)[-MAX_STATE:]] \
        + [{"t": t} for t in list(titles)[-MAX_STATE:]]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)


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
    """预览通道（测试群）：引擎 Tier1/2 门槛 + 需求校验。
    2026-08-16 用户定案：
      正式链 = 中国（大陆/香港/台湾/重要公司）发生的事（简单布尔）；
      预览链 = 引擎评分 Tier1/2 通过，且标题满足需求校验
        （需求2：中国词/公司名 OR 需求3：美政策词 OR 需求4：矿产词）；
        Mining.com 矿产专站条目绕过引擎门槛，直接需求校验。
      预览链不一定包含正式链全部信息（引擎门槛独立判定）。
    独立状态 last_sent_engine.json，失败只 WARN 不影响正式链。
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
    # 引擎门槛：Tier1/2（blacklisted/疑问句已在 engine_filter 剔除）
    filtered, stats, _ = engine_filter(fresh_items, _PREFILTER_CALIB)
    old_urls, old_titles = load_state(STATE_FILE_ENGINE)
    # 候选集 = 引擎 Tier1/2 条目 ∪ Mining.com 矿产专站条目（绕过引擎门槛）。
    # 2026-08-17：矿产新闻（铜/锂等）引擎评分偏低（T3，引擎为“中国相关”校准），
    # 引擎门槛会误伤；Mining.com 是矿产专站，来源本身无 Google News 全文匹配噪音，
    # 直接用需求校验（矿种词）过滤即可（Op-ed/公司八卦无矿种词会被拦）。
    filtered_titles = {it["title"] for it in filtered}
    mining_extra = [it for it in fresh_items
                    if it["source"] == "Mining.com" and it["title"] not in filtered_titles]
    candidates = filtered + mining_extra
    # 需求校验：需求2（中国词/公司名）OR 需求3（美政策词）OR 需求4（矿产词）
    fresh = [it for it in candidates
             if (US_POLICY_PREVIEW_RE.search(it["title"])
                  or MINERAL_RE.search(it["title"])
                  or keyword_hit(it["title"])
                  or company_hit(it["title"]))
             and it["url"] not in old_urls
             and it["title"].lower().strip() not in old_titles]
    print(f"Engine preview: {len(filtered)} filtered (tier1={stats['tier1']} tier2={stats['tier2']}), "
          f"+{len(mining_extra)} mining, {len(fresh)} new for test group")

    if not fresh:
        print("Engine preview: no new items, skip push")
        # 2026-08-16 用户要求：测试群也推 idle 提示，避免静默无法判断链路状态
        if not push_idle(webhook, note="引擎预览"):
            print("WARN: engine-preview idle notice push failed")
        return True

    # 头部统计口径：过滤总量（Tier1/2）+ 本次去重后新增条数
    stats2 = dict(stats)
    stats2["new_count"] = len(fresh)

    # 按条目实际 source 字段分组（2026-08-17 修复：原遍历 SOURCES 会丢弃 US Policy 条目）
    by_src = {}
    for it in fresh:
        by_src.setdefault(it["source"], []).append(it)
    source_groups = []
    for src, sub in by_src.items():
        sub.sort(key=lambda it: parse_dt(it["published"]), reverse=True)
        source_groups.append((src, sub))

    if not push_with_retry(webhook, source_groups, note=note, stats=stats2):
        print("WARN: engine-preview push failed (retried once); formal chain unaffected")
        return False
    save_state(old_urls | {it["url"] for it in fresh},
               old_titles | {it["title"].lower().strip() for it in fresh},
               STATE_FILE_ENGINE)
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
            im = implicit_china_hit(it["title"])
            win = within_24h(it["published"])
            rec = {
                "ts": ts,
                "source": it["source"],
                "title": it["title"],
                "url": it["url"],
                "published": it["published"],
                "keyword_hit": kw,
                "company_hit": co,
                "implicit_hit": im,
                "within_24h": win,
                "passed": (kw or co or im) and win,
            }
            if results is not None:
                r = results.get(it["title"])
                rec["engine_score"] = r["score"] if r else None
                rec["tier"] = r["tier"] if r else None
                rec["assets"] = r["assets"] if r else []
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(f"Dumped {n} items to {path}")


def _run(dump: bool = False, dump_only: bool = False, engine_preview: bool = False,
         us_policy: bool = False) -> int:
    src_map = {**SOURCES, **US_POLICY_SOURCES, **MINERAL_SOURCES} if us_policy else SOURCES
    total_queries = sum(len(v) for v in src_map.values())
    items, failures = fetch_all(include_us_policy=us_policy)
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

    # 主链（正式群）：简单布尔 = 关键词子串 or 公司名词边界 + 24h。
    # 2026-08-16 起需求校验（含美政策/暗含产业影响）收敛到预览链，正式链保持高纯度：
    # 只推标题明确含中国词或中国公司名的新闻。
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

    # 去重（URL + 标题双键：Google News 同文多变体 URL，标题兜底）
    old_urls, old_titles = load_state()
    fresh = [it for it in filtered
             if it["url"] not in old_urls
             and it["title"].lower().strip() not in old_titles]
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

    # 按来源分组（SOURCES 固定顺序 + US Policy），组内时间倒序（最新在前）
    # 2026-08-17 修复：原仅遍历 SOURCES，US Policy 条目（带中国关键词的制裁/关税新闻）被丢弃且被标记已推
    source_groups = []
    for src in list(SOURCES) + list(US_POLICY_SOURCES) + list(MINERAL_SOURCES):
        sub = [it for it in fresh if it["source"] == src]
        if sub:
            sub.sort(key=lambda it: parse_dt(it["published"]), reverse=True)
            source_groups.append((src, sub))

    if not push_with_retry(main_webhook, source_groups, note=note):
        push_error(main_webhook, "Reuters / Bloomberg 推送任务异常",
                   "推送失败（重试 1 次后仍失败）\n建议: 查看 GitHub Actions 日志确认 webhook 状态")
        return 1

    save_state(old_urls | {it["url"] for it in fresh},
               old_titles | {it["title"].lower().strip() for it in fresh})
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
    parser.add_argument("--us-policy", action="store_true",
                        help="启用美政策源查询组（Fed/关税/制裁/芯片/黑名单/出口管制，+12 查询，本地采集攒数据用）")
    args = parser.parse_args()
    try:
        return _run(dump=args.dump, dump_only=args.dump_only, engine_preview=args.engine_preview,
                    us_policy=args.us_policy)
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
