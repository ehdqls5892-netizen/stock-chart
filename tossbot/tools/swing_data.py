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
                try:
                    h = df[s]
                except KeyError:
                    h = df
                h = h.dropna(subset=["Close"])
                h = h[h["Volume"] > 0]
                b = frames_to_bars(h)
                if len(b) >= 120:
                    out[code] = b
            except Exception:
                pass
        log(f"일봉 {min(i + 60, len(items))}/{len(items)} · 받음 {len(out)}")
        time.sleep(1)
    return out


KR_RAW: dict = {}      # data-kr(우리가 매일 모으는 한국거래소 일봉) — 야후 마지막 봉 검증용


def kr_fix(bars: dict) -> int:
    """야후 국장 일봉은 마지막 며칠이 덜 반영되거나(시가=고가 같은 엉터리 봉) 틀릴 때가 있음 →
    우리 data-kr 일봉(한국거래소 기준)으로 최근 10일을 맞추고, 야후에 없는 최신 날은 붙임. 고친 종목 수를 돌려줌."""
    fixed = 0
    for code, b in bars.items():
        raw = KR_RAW.get(code) or []
        if not raw or not b:
            continue
        kr = {}
        for r in raw[-10:]:
            try:
                d = "20" + str(r[0])[:2] + "-" + str(r[0])[2:4] + "-" + str(r[0])[4:6]
                if r[4] and r[1] and r[2] and r[3]:
                    kr[d] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0))
            except (IndexError, TypeError, ValueError):
                continue
        if not kr:
            continue
        same = [d for d in kr if any(str(x[0])[:10] == d for x in b[-15:])]
        ref = next((x for x in reversed(b) if str(x[0])[:10] in kr), None)
        if ref and abs(kr[str(ref[0])[:10]][3] / ref[4] - 1) > 0.15:
            continue                                    # 액면분할·수정주가 차이가 크면 건드리지 않음
        ch = False
        for i in range(max(0, len(b) - 15), len(b)):
            d = str(b[i][0])[:10]
            if d in kr:
                o, h, l, c, v = kr[d]
                if abs(c / b[i][4] - 1) > 0.001 or abs(o / b[i][1] - 1) > 0.001 or abs(h / b[i][2] - 1) > 0.001:
                    b[i] = (b[i][0], o, h, l, c, v)
                    ch = True
        last = str(b[-1][0])[:10]
        for d in sorted(kr):
            if d > last and same:
                b.append((d,) + kr[d])
                ch = True
        fixed += ch
    return fixed


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
        if bars:
            KR_RAW[code] = bars[-12:]
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


def kr_flow_naver(codes: list, years: int, budget: int = 6000) -> dict:
    """pykrx 가 막혔을 때: 네이버 금융 '외국인·기관 순매매' 일별 표(한 쪽 20일)를 넘겨 가며 받음. 값은 순매매량(주)."""
    import io
    import pandas as pd
    ses = requests.Session()
    ses.headers["User-Agent"] = "Mozilla/5.0 (stock-chart research)"
    pages = int(years * 250 / 20) + 1
    cut = (dt.date.today() - dt.timedelta(days=365 * years)).isoformat()
    out, t0 = {}, time.time()

    def num(v):
        try:
            return float(str(v).replace(",", "").replace("+", ""))
        except ValueError:
            return None
    for i, code in enumerate(codes):
        if time.time() - t0 > budget:
            log("네이버 수급: 시간 예산 초과, 여기까지", i)
            break
        rows = {}
        for pg in range(1, pages + 1):
            try:
                r = ses.get(f"https://finance.naver.com/item/frgn.naver?code={code}&page={pg}", timeout=15)
                r.encoding = "euc-kr"
                got = 0
                for tb in pd.read_html(io.StringIO(r.text)):
                    cols = ["".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in tb.columns]
                    if not any("날짜" in c for c in cols):
                        continue
                    di = next(k for k, c in enumerate(cols) if "날짜" in c)
                    ii = next((k for k, c in enumerate(cols) if "기관" in c), None)
                    fi = next((k for k, c in enumerate(cols) if "외국인" in c and "순매매" in c), None)
                    if ii is None or fi is None:
                        continue
                    for row in tb.itertuples(index=False):
                        d = str(row[di]).strip().replace(".", "-")
                        fo, og = num(row[fi]), num(row[ii])
                        if len(d) == 10 and d[4] == "-" and fo is not None and og is not None:
                            rows[d] = (fo, og)
                            got += 1
                if not got or min(rows) < cut:
                    break
            except Exception as e:
                if pg == 1:
                    log("네이버 수급 실패", code, repr(e)[:100])
                break
            time.sleep(0.12)
        if rows:
            out[code] = [(d, v[0], v[1]) for d, v in sorted(rows.items())]
        if i % 50 == 0:
            log(f"네이버 수급 {i}/{len(codes)} · 받음 {len(out)}")
    return out


def kr_flow_api(codes: list, years: int, budget: int = 5400) -> dict:
    """네이버 모바일 JSON API(로봇이 매일 5일 수급을 받는 곳과 같은 서버)의 일별 투자자 동향을 과거로 넘겨 가며 받음.
    값은 외국인·기관 순매수량(주). 처음 몇 종목이 다 실패하면 바로 그만둠(막힌 것)."""
    import re
    ses = requests.Session()
    ses.headers["User-Agent"] = "Mozilla/5.0 (stock-chart bot)"
    cut = (dt.date.today() - dt.timedelta(days=365 * years)).strftime("%Y%m%d")
    out, t0, fails = {}, time.time(), 0

    def num(v):
        try:
            return float(re.sub(r"[^\d.\-]", "", str(v)))
        except ValueError:
            return None
    for i, code in enumerate(codes):
        if time.time() - t0 > budget:
            log("수급 API: 시간 예산 초과, 여기까지", i)
            break
        rows, biz = {}, dt.date.today().strftime("%Y%m%d")
        for _ in range(40):
            try:
                r = ses.get(f"https://m.stock.naver.com/api/stock/{code}/trend", params={"pageSize": 60, "bizdate": biz}, timeout=15)
                if r.status_code != 200:
                    if not out and i < 3:
                        log("수급 API 응답", code, r.status_code, r.text[:200].replace("\n", " "))
                    break
                js = r.json()
                items = js if isinstance(js, list) else next((v for v in js.values() if isinstance(v, list)), [])
                new = 0
                for it in items:
                    d = str(it.get("bizdate") or "")
                    fo, og = num(it.get("foreignerPureBuyQuant")), num(it.get("organPureBuyQuant"))
                    if len(d) == 8 and fo is not None and og is not None and d not in rows:
                        rows[d] = (fo, og)
                        new += 1
                if not out and i < 3 and not new:
                    log("수급 API 모양 확인", code, str(js)[:300])
                if not new or min(rows) <= cut:
                    break
                biz = (dt.datetime.strptime(min(rows), "%Y%m%d") - dt.timedelta(days=1)).strftime("%Y%m%d")
            except Exception as e:
                if not out and i < 3:
                    log("수급 API 실패", code, repr(e)[:150])
                break
            time.sleep(0.1)
        if len(rows) >= 20:
            out[code] = [(f"{d[:4]}-{d[4:6]}-{d[6:]}", v[0], v[1]) for d, v in sorted(rows.items())]
        else:
            fails += 1
            if fails >= 10 and not out:
                log("수급 API: 처음 10종목 모두 실패 → 중단")
                break
        if i % 50 == 0:
            log(f"수급 API {i}/{len(codes)} · 받음 {len(out)} · 예: {len(rows)}일")
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
    ap.add_argument("--flow-only", action="store_true")
    a = ap.parse_args()
    res = {"asof": dt.datetime.utcnow().isoformat(timespec="minutes"), "flow": {}, "earn": {}}
    if a.flow_only and a.market == "kr":                 # 일봉은 그대로 두고 수급 기록만 다시 받음 (빠름)
        import gzip as _gz
        import pickle as _pk
        raw = requests.get("https://raw.githubusercontent.com/ehdqls5892-netizen/stock-chart/swingdata-kr/swing-kr.pkl.gz", timeout=120).content
        res = _pk.loads(_gz.decompress(raw))
        res["flow"] = kr_flow_api(list(res["bars"]), min(a.years, 3))
        if len(res["flow"]) < len(res["bars"]) * 0.2:
            log("수급 API 로 거의 못 받음 → 한국거래소(pykrx, 로그인)")
            res["flow"] = kr_flow(list(res["bars"]), min(a.years, 2)) or res["flow"]
    elif a.market == "kr":
        yfs, names = kr_universe(a.n)
        res["bars"] = yf_bars(yfs, a.years)
        res["names"] = names
        res["index"] = yf_bars({"KOSPI": "^KS11", "KOSDAQ": "^KQ11"}, a.years)
        try:
            res["flow"] = kr_flow_api(list(res["bars"]), min(a.years, 3))
            if len(res["flow"]) < len(res["bars"]) * 0.2:
                log("수급 API 로 거의 못 받음 → pykrx")
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
