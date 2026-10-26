"""전 종목 차트 — 검색용. 한국 상장 전 종목(코스피·코스닥) / 미국 S&P500+나스닥100 의 차트 분석을 종목별 파일로 만든다.

출력 (data-kr / data-us 브랜치에 강제 푸시, 기록은 남기지 않음 → 저장소가 커지지 않음)
  out/index.json          {asOf, market, items:[[code,name,market,aliases?],...]}
  out/s/<code>.json       chart.analyze() 결과 + name, market (bars 120개 포함)
  out/_cache.csv.gz       (한국만) 날짜별 전 종목 시세 캐시 — 다음 날엔 하루치만 새로 받음

실행: python screener/allstocks.py kr|us   (환경변수 CACHE_URL: 지난 캐시 주소, 없으면 처음부터)
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chart import analyze  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
DAYS = 260          # 약 1년치 거래일 (120일선·지지/저항 계산용)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ─────────────────────────── 한국: pykrx 날짜별 전 종목 ───────────────────────────

def kr_frames():
    from pykrx import stock
    cache = pd.DataFrame()
    url = os.getenv("CACHE_URL")
    if url:
        try:
            import requests
            r = requests.get(url, timeout=60)
            if r.status_code == 200:
                cache = pd.read_csv(io.BytesIO(r.content), compression="gzip", dtype={"code": str, "d": str})
                log("캐시", len(cache), "행")
        except Exception as e:
            log("캐시 못 받음:", e)
    have = set(cache["d"].unique()) if len(cache) else set()
    holidays = set(cache.loc[cache["code"] == "_", "d"]) if len(cache) else set()
    today = datetime.now(timezone(timedelta(hours=9))).date()
    want, d = [], today
    while len(want) < DAYS + 30 and d > today - timedelta(days=420):
        if d.weekday() < 5:
            want.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    rows, names = [], {}
    new = [x for x in want if x not in have or x == want[0]]   # 오늘은 항상 다시 (장중 실행 대비)
    log("새로 받을 날짜", len(new))
    for ds in new:
        got = 0
        for mkname, mk in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
            for t in range(3):
                try:
                    df = stock.get_market_ohlcv_by_ticker(ds, market=mkname)
                    break
                except Exception as e:
                    log("재시도", ds, mkname, e)
                    time.sleep(2 + t * 3)
            else:
                df = pd.DataFrame()
            if df is None or df.empty or float(df["거래량"].sum()) == 0:
                continue
            for code, r in df.iterrows():
                rows.append((code, ds, mk, r["시가"], r["고가"], r["저가"], r["종가"], r["거래량"]))
            got += len(df)
            time.sleep(0.3)
        if got == 0:
            rows.append(("_", ds, "", 0, 0, 0, 0, 0))   # 휴장일 표시
    newdf = pd.DataFrame(rows, columns=["code", "d", "mk", "o", "h", "l", "c", "v"])
    all_ = pd.concat([cache[~cache["d"].isin(newdf["d"].unique())] if len(cache) else cache, newdf], ignore_index=True)
    all_ = all_[all_["d"].isin(set(want))]
    OUT.mkdir(parents=True, exist_ok=True)
    all_.to_csv(OUT / "_cache.csv.gz", index=False, compression="gzip")
    # 이름: 마지막 거래일 기준 상장 목록
    last = sorted(set(all_.loc[all_["code"] != "_", "d"]))[-1]
    for mkname in ("KOSPI", "KOSDAQ"):
        for code in stock.get_market_ticker_list(last, market=mkname):
            try:
                names[code] = stock.get_market_ticker_name(code)
            except Exception:
                pass
    live = all_[(all_["code"] != "_")]
    cur = set(live.loc[live["d"] == last, "code"])
    frames, meta = {}, {}
    for code, g in live[live["code"].isin(cur)].groupby("code"):
        g = g.sort_values("d")
        g = g[g["v"] > 0]
        if len(g) < 40:
            continue
        df = pd.DataFrame({"Open": g["o"].values, "High": g["h"].values, "Low": g["l"].values,
                           "Close": g["c"].values, "Volume": g["v"].values},
                          index=pd.to_datetime(g["d"].values, format="%Y%m%d")).astype(float)
        frames[code] = df
        meta[code] = {"name": names.get(code, code), "market": g["mk"].iloc[-1]}
    return frames, meta


# ─────────────────────────── 미국: S&P500 + 나스닥100 (yfinance) ───────────────────────────

def us_list():
    import requests
    out = {}
    ua = {"User-Agent": "Mozilla/5.0 (stock-chart bot)"}
    for url, col_sym, col_name in (
        ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol", "Security"),
        ("https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker", "Company"),
    ):
        try:
            html = requests.get(url, headers=ua, timeout=30).text
            for t in pd.read_html(io.StringIO(html)):
                if col_sym in t.columns and col_name in t.columns:
                    for s, n in zip(t[col_sym], t[col_name]):
                        out[str(s).strip()] = {"name": str(n).strip(), "market": ""}
                    break
        except Exception as e:
            log("목록 실패", url, e)
    for f in ("universe_us.json",):
        p = ROOT / "config" / f
        if p.exists():
            for x in json.loads(p.read_text(encoding="utf-8")):
                out.setdefault(x["code"], {"name": x["name"], "market": x.get("market", "")})
    w = ROOT / "config" / "watch.json"
    if w.exists():
        for x in json.loads(w.read_text(encoding="utf-8")).get("us", []):
            out.setdefault(x["code"], {"name": x.get("name", x["code"]), "market": x.get("market", "")})
    return out


def us_frames():
    import yfinance as yf
    uni = us_list()
    log("미국 목록", len(uni))
    frames = {}
    codes = list(uni)
    for i in range(0, len(codes), 80):
        part = codes[i:i + 80]
        syms = [c.replace(".", "-") for c in part]
        try:
            df = yf.download(syms, period="1y", interval="1d", group_by="ticker",
                             auto_adjust=False, progress=False, threads=True)
        except Exception as e:
            log("받기 실패", e)
            continue
        for c, s in zip(part, syms):
            try:
                h = (df[s] if len(syms) > 1 else df).dropna(subset=["Close"])
                h = h[h["Volume"] > 0]
                if len(h) >= 40:
                    frames[c] = h[["Open", "High", "Low", "Close", "Volume"]].astype(float)
            except Exception:
                pass
        time.sleep(1)
    return frames, uni


def main():
    mk = (sys.argv[1] if len(sys.argv) > 1 else "kr").lower()
    frames, meta = kr_frames() if mk == "kr" else us_frames()
    alias = {}
    ap = ROOT / "config" / "alias_us.json"
    if mk == "us" and ap.exists():
        alias = json.loads(ap.read_text(encoding="utf-8"))
    (OUT / "s").mkdir(parents=True, exist_ok=True)
    items, as_of = [], ""
    for code, df in frames.items():
        try:
            a = analyze(df)
        except Exception as e:
            log("skip", code, e)
            continue
        m = meta.get(code, {})
        a.update(code=code, name=m.get("name", code), market=m.get("market", ""))
        (OUT / "s" / f"{code}.json").write_text(json.dumps(a, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        it = [code, a["name"], a["market"]]
        if alias.get(code):
            it.append(alias[code])
        items.append(it)
        as_of = max(as_of, a["bars"][-1][0])
    if not items:
        raise SystemExit("분석된 종목이 없어요 (휴장이거나 시세 수신 실패)")
    idx = {"market": mk, "asOf": f"20{as_of[:2]}-{as_of[2:4]}-{as_of[4:]}", "count": len(items),
           "generatedAt": datetime.now(timezone.utc).isoformat(timespec="minutes"), "items": sorted(items)}
    (OUT / "index.json").write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"{mk}: {len(items)}종목 저장 · asOf {idx['asOf']}")


if __name__ == "__main__":
    main()
