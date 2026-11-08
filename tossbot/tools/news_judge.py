"""1시간마다 (GitHub Actions): 후보·오늘 살 종목·데스크 종목의 최신 기사를 AI(Haiku)가 읽고 호재/악재 다시 판정.
python tools/news_judge.py kr|us --out out  → out/news.json {market, made, stocks:{code:{impact,why,adj,severe,...}}}
로봇은 매수 직전에 이걸 읽어 '심한 악재'면 안 사고, 점수에 adj 를 더함. 앱 데스크 표시·악재 알림에도 씀."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import news as N  # noqa: E402
from bot import news_judge as NJ  # noqa: E402

RAW = "https://raw.githubusercontent.com/ehdqls5892-netizen/stock-chart"


def _j(url):
    try:
        r = requests.get(url, timeout=20)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def targets(m: str) -> list:
    """판정할 종목: 오늘 흐름 후보(live) → 밤 후보(picks) → 데스크 계획·관심·호재·실적."""
    names = _j(f"{RAW}/swingpicks-{m}/names.json") or {}
    out = {}
    live = _j(f"{RAW}/swinglive-{m}/live.json")
    for x in (live.get("picks") or [])[:25]:            # 날짜가 지났어도 최근 후보라 같이 봄
        out.setdefault(str(x["code"]), x.get("name"))
    for x in (_j(f"{RAW}/swingpicks-{m}/picks.json").get("picks") or [])[:30]:
        out.setdefault(str(x["code"]), x.get("name"))
    for k in ("report", "live", "earn"):
        d = _j(f"{RAW}/main/docs/data/desk-{m}-{k}.json")
        info = d.get("info") or {}
        nm = d.get("names") or {}
        codes = list(d.get("plans") or {})
        if k == "report":
            codes += (d.get("watch") or [])[:20]
        if k == "live":
            codes += list(d.get("impact") or {})
        if k == "earn":
            codes += list(d.get("good") or {}) + list(d.get("upcoming") or {})
            nm = {c: (e or {}).get("name") for c, e in (d.get("events") or {}).items()}
        for c in codes:
            c = str(c)
            out.setdefault(c, (info.get(c) or {}).get("name") or nm.get(c))
    items = [{"code": c, "name": n or names.get(c) or c} for c, n in out.items()]
    return items[:80]


def news_fn(m: str):
    def f(code, name):
        heads = []
        if m == "kr" and code.isdigit():
            heads += N.naver_stock_news(code, 8)
        heads += N.recent_news(name, m.upper(), days=2, n=6)
        return list(dict.fromkeys(heads))[:8]
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["kr", "us"])
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    m = a.market
    prev = (_j(f"{RAW}/swingnews-{m}/news.json") or {}).get("stocks") or {}
    items = targets(m)
    print(f"뉴스 판정 대상 {len(items)}종목 (지난 판정 {len(prev)}개)", file=sys.stderr)
    res = NJ.judge(m.upper(), items, news_fn(m), prev)
    new = sum(1 for v in res.values() if v.get("t") and v not in prev.values())
    bad = [v["name"] for v in res.values() if v["impact"] == "악재"]
    print(f"판정 {len(res)}개 (새로 읽음 {new}) · 악재: {', '.join(bad) or '없음'}", file=sys.stderr)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "news.json").write_text(json.dumps({"market": m, "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                                               "stocks": res}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
