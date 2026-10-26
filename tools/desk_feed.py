"""주식비서 데스크 자료 → 토스 자동매매 로봇용 요약 파일 (docs/data/desk-<m>-<kind>.json).

예약 작업(Claude)이 아티팩트 DB 문서를 JSON 파일로 받은 뒤 이 스크립트로 변환해 GitHub 에 올린다.
로봇은 raw.githubusercontent.com 에서 읽는다 (로그인 불필요).

  python tools/desk_feed.py report   --market kr --report R.json
  python tools/desk_feed.py live     --market kr --intraday I.json [--issues S.json]
  python tools/desk_feed.py earn     --market us --calendar C.json [--today 2026-10-27]

kind
  report: 마감 리포트의 1순위·픽·관심종목 가격 계획(plans) + 테마·차트 종목(watch)
  live:   장중 알림의 '지금 1순위' 가격 계획 + 새로 급등한 종목·실시간 이슈 종목(watch) + 장중 실적 서프라이즈(earn_good)
  earn:   실적 발표 예정(upcoming: code→날짜), 발표 결과 좋음(good)·나쁨(bad)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path(__file__).resolve().parent.parent / "docs" / "data"
TZ = {"kr": ZoneInfo("Asia/Seoul"), "us": ZoneInfo("America/New_York")}


def load(p):
    j = json.loads(Path(p).read_text(encoding="utf-8"))
    return j.get("data", j) if isinstance(j, dict) and "data" in j and isinstance(j["data"], dict) else j


def tick(v, m):
    if m == "us":
        return round(v, 2)
    t = 1 if v < 1000 else 5 if v < 5000 else 10 if v < 10000 else 50 if v < 50000 else 100 if v < 100000 else 500 if v < 500000 else 1000
    return round(v / t) * t


def plan_of(it, m, src, price=None):
    """대시보드 planOf 와 같은 방식으로 진입 구간·1차 목표를 채운다. 계획이 없거나 무효면 None."""
    p = it.get("plan") or {}
    if not p or p.get("skip") or p.get("status"):
        return None
    price = price if price is not None else (it.get("price") if it.get("price") is not None else it.get("close"))
    stop = p.get("stop")
    try:
        price, stop = float(price), float(stop)
    except (TypeError, ValueError):
        return None
    if stop >= price:
        return None
    hi = float(p.get("entryHigh") or price)
    lo = float(p.get("entryLow") or tick(stop + (price - stop) * 0.5, m))
    mid = (lo + hi) / 2
    t1 = float(p.get("t1") or tick(mid + 2 * (mid - stop), m))
    out = {"name": it.get("name", ""), "stop": stop, "entryLow": lo, "entryHigh": hi, "t1": t1, "src": src}
    if p.get("t2"):
        out["t2"] = float(p["t2"])
    sc = it.get("score") or {}
    if isinstance(sc, dict) and sc:
        out["score"] = sum(v for k, v in sc.items() if isinstance(v, (int, float)))
    return out


def report(a):
    r = load(a.report)
    m = a.market
    plans, watch = {}, []
    top = (r.get("top") or {}).get("code")
    items = [(p, "픽") for p in r.get("picks", [])] + [(w, "관심") for w in r.get("watchlist", [])]
    for it, src in items:
        c = it.get("code")
        if not c:
            continue
        pl = plan_of(it, m, "1순위" if c == top else src)
        if pl:
            plans.setdefault(c, pl)
        else:
            watch.append(c)
    for t in r.get("themes", []):
        for x in t.get("related", []):
            if x.get("code"):
                watch.append(x["code"])
    for k in ("breakout", "rebound", "trend"):
        for x in (r.get("chartScan") or {}).get(k, []):
            if x.get("code"):
                watch.append(x["code"])
    return {"market": m, "date": r.get("date"), "plans": plans, "watch": list(dict.fromkeys(c for c in watch if c not in plans))[:60]}


def live(a):
    d = load(a.intraday)
    m = a.market
    ups = d.get("updates") or []
    plans, watch, good = {}, [], {}
    if ups:
        u = ups[-1]
        top = u.get("top") or {}
        if top.get("code"):
            pl = plan_of(top, m, "장중 1순위", top.get("price"))
            if pl:
                plans[top["code"]] = pl
        for x in u.get("items", []):
            if x.get("code") and (x.get("ref") == "new" or (x.get("pct") or 0) >= 5):
                watch.append(x["code"])
        for x in u.get("earnGood") or []:
            if x.get("code"):
                good[x["code"]] = {"date": d.get("date"), "text": x.get("text", "")}
    if a.issues:
        iss = load(a.issues)
        for x in iss.get("issues", []):
            if x.get("market") in (m, "global") and x.get("impact") != "악재":
                for s in x.get("stocks", []):
                    if s.get("code"):
                        watch.append(s["code"])
    return {"market": m, "date": d.get("date"), "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
            "plans": plans, "watch": list(dict.fromkeys(watch))[:40], "earn_good": good}


def earn(a):
    c = load(a.calendar)
    m = a.market
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(TZ[m]).date()
    up, good, bad = {}, {}, {}
    for e in c.get("events", []):
        code, ds = e.get("code"), e.get("date")
        if not code or not ds:
            continue
        try:
            d = dt.date.fromisoformat(ds[:10])
        except ValueError:
            continue
        sur = (e.get("surprise") or "").strip()
        verdict = (e.get("result") or {}).get("verdict")
        text = e.get("surpriseText") or (e.get("result") or {}).get("text") or ""
        if sur == "좋음" or (not sur and verdict == "상승"):
            if (today - d).days <= 3:
                good[code] = {"date": ds, "text": text, "name": e.get("name", "")}
        elif sur == "나쁨" or (not sur and verdict == "하락"):
            if (today - d).days <= 2:
                bad[code] = {"date": ds, "text": text}
        elif d >= today and not sur and not verdict:
            up[code] = ds
    return {"market": m, "date": today.isoformat(), "upcoming": up, "good": good, "bad": bad}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["report", "live", "earn"])
    ap.add_argument("--market", required=True, choices=["kr", "us"])
    ap.add_argument("--report")
    ap.add_argument("--intraday")
    ap.add_argument("--issues")
    ap.add_argument("--calendar")
    ap.add_argument("--today")
    a = ap.parse_args()
    out = {"report": report, "live": live, "earn": earn}[a.kind](a)
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / f"desk-{a.market}-{a.kind}.json"
    f.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n = {k: len(v) for k, v in out.items() if isinstance(v, (dict, list))}
    print(f"{f.name} 저장: {n}")


if __name__ == "__main__":
    main()
