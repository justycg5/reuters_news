#!/usr/bin/env python3
"""
backfill.py - 抓取历史窗口新闻（when:7d / when:30d）用于历史回放验证与初步校准

⚠️ 数据边界（诚实标注）:
  - Google News RSS 不支持按任意日期区间查询，只能用预设窗口 when:7d / when:30d
  - 每查询最多返回相关性最高的 100 条 → 历史样本有偏（高相关新闻占比远高于全量）
  - 用途: ① golden set 历史回放验证（补低频事件样本）② 初步分位数参考
  - 正式分位数校准仍以实时 dump（when:1d 并集）为准，backfill 数据不进推送链路

用法:
    python backfill.py                    # 默认 7d + 30d
    python backfill.py --windows 7d 30d   # 指定窗口
落盘: data/backfill-YYYY-MM-DD-<window>.jsonl（每次运行覆盖该文件，重跑即刷新）
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_reuters as fr


def make_queries(window: str) -> dict:
    """把 SOURCES 的 when:1d 替换为指定窗口。"""
    out = {}
    for src, queries in fr.SOURCES.items():
        out[src] = [q.replace("when%3A1d", f"when%3A{window}") for q in queries]
    return out


def fetch_window(window: str, sleep_s: float = 1.0):
    """抓取单个窗口全部查询（失败重试 1 次，查询间 sleep 防反爬）。"""
    queries = make_queries(window)
    seen_url, seen_title, merged, failures = set(), set(), [], []
    for src, qs in queries.items():
        for q in qs:
            fr._fetch_query(src, q, seen_url, seen_title, merged, failures)
            time.sleep(sleep_s)
    if failures:
        print(f"Retrying {len(failures)} failed query(ies) once after 5s...")
        time.sleep(5)
        retried = []
        for src, q, _ in failures:
            fr._fetch_query(src, q, seen_url, seen_title, merged, retried)
            time.sleep(sleep_s)
        failures = retried
    return merged, failures


def dump_window(items: list, window: str) -> tuple:
    """落盘 data/backfill-YYYY-MM-DD-<window>.jsonl（覆盖写，重跑刷新）。"""
    os.makedirs(fr.DUMP_DIR, exist_ok=True)
    day = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    path = os.path.join(fr.DUMP_DIR, f"backfill-{day}-{window}.jsonl")
    ts = datetime.now(timezone.utc).isoformat()
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            rec = {
                "ts": ts,
                "source": it["source"],
                "title": it["title"],
                "url": it["url"],
                "published": it["published"],
                "window": window,
                "keyword_hit": fr.keyword_hit(it["title"]),
                "company_hit": fr.company_hit(it["title"]),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return path, n


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取历史窗口新闻用于回放验证")
    ap.add_argument("--windows", nargs="+", default=["7d", "30d"])
    args = ap.parse_args()
    total = 0
    for w in args.windows:
        print(f"=== window {w} ===")
        items, failures = fetch_window(w)
        if not items:
            print(f"WARN: {w} all queries failed: {[m for _, _, m in failures[:3]]}")
            continue
        path, n = dump_window(items, w)
        total += n
        print(f"[{w}] {n} items -> {path} (failures={len(failures)})")
    print(f"Total backfilled: {total}")


if __name__ == "__main__":
    sys.exit(main())
