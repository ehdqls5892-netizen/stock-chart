"""스윙 전략 연구용 자료 모으기 (GitHub Actions 에서 실행 — 야후·KRX 접속 가능한 곳).

  python tools/swing_data.py kr|us --years 3 --out ../out

모으는 것
  - 일봉 3년 (수정주가, 야후): 국장은 최근 거래대금 상위 유동 종목, 미장은 S&P500+나스닥100
  - 지수: 국장 코스피(^KS11)·코스닥(^KQ11), 미장 SPY·QQQ
  - 국장 수급: 종목별 일별 외국인·기관 순매수 금액 (pykrx, 최근 2년)
  - 미장 실적: 발표일·예상 EPS·실제 EPS·서프라이즈% (야후)
결과: out/swing-<m>.pkl.gz = {"bars", "names", "index", "flow", "earn", "asof"}
※ 지금 상장된 종목만 들어가므로(상장폐지 종목 빠짐) 결과가 실제보다 조금 좋게 나올 수 있음.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import pickle
import sys
import time
from pathlib import Path

import requests


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def frames_to_bars(df):
    out = []
    for ts, r in df.iterrows():
        try:
            o, h, l, c, v = (float(r[k]) for k in ("Open", "High", "Low", "Close", "Volume"))
        except (KeyError, TypeError, ValueError):
            continue
        if c != c or o != o or c <= 0:
            continue
        out.append((ts.strftime("%Y-%m-%d"), o, h, l, c, v))
    return out


def yf_bars(tickers: dict, years: int) -> dict:
    """tickers: 우리 코드 → 야후 심볼"""
    import yfinance as yf
    out = {}
    items = list(tickers.items())
    for i in range(0, len(items), 60):
        part = items[i:i + 60]
        syms = [s for _, s in part]
        for t in range(3):
            try:
                df = yf.download(syms, period=f"{years}y", interval="1d", group_by="ticker", auto_adjust=True,
                                 progress=False, threads=True)
                break
            except Exception as e:
                log("재시도", e)
                time.sleep(5)
        else:
            continue
        for code, s in part:
            try:
                h = (df[s] if len(syms) > 1 else df).dropna(subset=["Close"])
                h = h[h["Volume"] > 0]
                b = frames_to_bars(h)
                if len(b) >= 120:
                    out[code] = b
            except Exception:
                pass
        log(f"일봉 {min(i + 60, len(items))}/{len(items)} · 받음 {len(out)}")
        time.sleep(1)
    return out


def kr_universe(n: int) -> tuple[dict, dict]:
    """data-kr 브랜치 전 종목에서 최근 60일 평균 거래대금 상위 n개."""
    base = "https://raw.githubusercontent.com/ehdqls5892-netizen/stock-chart/data-kr"
    idx = requests.get(f"{base}/index.json", timeout=60).json()
    rows = []
    for it in idx["items"]:
        code, name, mk = it[0], it[1], it[2] if len(it) > 2 else ""
        try:
            r = requests.get(f"{base}/s/{code}.json", timeout=20)
            bars = r.json().get("bars") or [] if r.status_code == 200 else []
        except Exception:
            continue
        tv = [b[4] * b[5] for b in bars[-60:] if len(b) > 5 and b[4] and b[5]]
        if len(tv) >= 40 and code.isdigit():
            rows.append((sum(tv) / len(tv), code, name, mk))
    rows.sort(reverse=True)
    pick = rows[:n]
    log("국장 후보", len(rows), "→", len(pick))
    yfs = {c: c + (".KQ" if mk == "코스닥" else ".KS") for _, c, _, mk in pick}
    names = {c: nm for _, c, nm, _ in pick}
    return yfs, names


def kr_flow(codes: list, years: int) -> dict:
    """종목별 일별 외국인·기관 순매수(원). pykrx 한 종목에 한 번 호출."""
    from pykrx import stock
    end = dt.date.today()
    start = end - dt.timedelta(days=365 * years)
    out, t0 = {}, time.time()
    for i, code in enumerate(codes):
        if time.time() - t0 > 3000:
            log("수급: 시간 예산 초과, 여기까지", i)
            break
        for t in range(2):
            try:
                df = stock.get_market_trading_value_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code)
                fcol = next((c for c in df.columns if "외국인" in c and "기타" not in c), None)
                icol = next((c for c in df.columns if "기관" in c), None)
                if df is not None and len(df) and fcol and icol:
                    out[code] = [(ts.strftime("%Y-%m-%d"), float(r[fcol]), float(r[icol])) for ts, r in df.iterrows()]
                break
            except Exception as e:
                if t:
                    log("수급 실패", code, repr(e)[:120])
                time.sleep(2)
        if i % 50 == 0:
            log(f"수급 {i}/{len(codes)} · 받음 {len(out)}")
        time.sleep(0.2)
    return out


def us_universe() -> dict:
    out = {}
    ua = {"User-Agent": "Mozilla/5.0 (stock-chart bot)"}
    import io
    import pandas as pd
    for url, col_sym, col_name in (("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol", "Security"),
                                   ("https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker", "Company")):
        try:
            html = requests.get(url, headers=ua, timeout=30).text
            for t in pd.read_html(io.StringIO(html)):
                if col_sym in t.columns and col_name in t.columns:
                    for s, n in zip(t[col_sym], t[col_name]):
                        out[str(s).strip()] = str(n).strip()
                    break
        except Exception as e:
            log("목록 실패", url, e)
    return out


def us_earn(codes: list) -> dict:
    import yfinance as yf
    out, t0 = {}, time.time()
    for i, c in enumerate(codes):
        if time.time() - t0 > 2400:
            log("실적: 시간 예산 초과", i)
            break
        try:
            df = yf.Ticker(c.replace(".", "-")).get_earnings_dates(limit=16)
            rows = []
            for ts, r in (df if df is not None else []).iterrows():
                est, rep, sp = r.get("EPS Estimate"), r.get("Reported EPS"), r.get("Surprise(%)")
                rows.append((ts.strftime("%Y-%m-%d %H:%M"), None if est != est else float(est),
                             None if rep != rep else float(rep), None if sp != sp else float(sp)))
            if rows:
                out[c] = rows
        except Exception as e:
            if i < 3:
                log("실적 실패", c, repr(e)[:120])
        if i % 50 == 0:
            log(f"실적 {i}/{len(codes)} · 받음 {len(out)}")
        time.sleep(0.4)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["kr", "us"])
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--n", type=int, default=700)
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    res = {"asof": dt.datetime.utcnow().isoformat(timespec="minutes"), "flow": {}, "earn": {}}
    if a.market == "kr":
        yfs, names = kr_universe(a.n)
        res["bars"] = yf_bars(yfs, a.years)
        res["names"] = names
        res["index"] = yf_bars({"KOSPI": "^KS11", "KOSDAQ": "^KQ11"}, a.years)
        try:
            res["flow"] = kr_flow(list(res["bars"]), min(a.years, 2))
        except Exception as e:
            log("수급 전체 실패", e)
    else:
        uni = us_universe()
        log("미장 목록", len(uni))
        res["bars"] = yf_bars({c: c.replace(".", "-") for c in uni}, a.years)
        res["names"] = uni
        res["index"] = yf_bars({"SPY": "SPY", "QQQ": "QQQ"}, a.years)
        res["earn"] = us_earn(list(res["bars"]))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with gzip.open(out / f"swing-{a.market}.pkl.gz", "wb") as f:
        pickle.dump(res, f)
    summ = {k: len(v) if hasattr(v, "__len__") else v for k, v in res.items()}
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False), encoding="utf-8")
    log("완료", summ)


if __name__ == "__main__":
    main()
