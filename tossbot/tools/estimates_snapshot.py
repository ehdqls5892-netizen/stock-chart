"""컨센서스(애널리스트 추정치·목표가) 매일 기록 — 몇 달 쌓이면 '추정치가 오르는 종목'이 더 나은지 시험하려고.

  python tools/estimates_snapshot.py us|kr --out out/us/2026-11-02.csv

미장 (야후): 이번 분기·다음 분기·올해·내년 EPS 예상, 7/30/60/90일 전 예상(eps_trend), 최근 7/30일 상향·하향 수,
            평균 목표가·애널리스트 수·추천 점수, 현재가, PER(예상)
국장 (네이버 금융 종목 메인): 투자의견 점수·목표주가, 기업실적분석 표의 연간 EPS·영업이익 예상(E), 현재가
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def num(x):
    try:
        if x is None:
            return None
        v = float(str(x).replace(",", ""))
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def us_rows(codes: dict) -> list[dict]:
    import yfinance as yf
    rows, t0 = [], time.time()
    for i, (code, name) in enumerate(codes.items()):
        if time.time() - t0 > 3300:
            log("시간 예산 초과", i)
            break
        t = yf.Ticker(code.replace(".", "-"))
        r = {"code": code, "name": name}
        try:
            info = t.get_info() or {}
            for k in ("currentPrice", "forwardEps", "trailingEps", "forwardPE", "targetMeanPrice", "targetHighPrice",
                      "targetLowPrice", "numberOfAnalystOpinions", "recommendationMean"):
                r[k] = num(info.get(k))
        except Exception as e:
            r["err"] = f"info:{type(e).__name__}"
        try:
            tr = t.eps_trend
            if tr is not None and len(tr):
                for per in ("0q", "+1q", "0y", "+1y"):
                    if per in tr.index:
                        for col in ("current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo"):
                            if col in tr.columns:
                                r[f"eps_{per}_{col}"] = num(tr.loc[per, col])
        except Exception as e:
            r["err"] = (r.get("err", "") + f" trend:{type(e).__name__}").strip()
        try:
            rv = t.eps_revisions
            if rv is not None and len(rv):
                for per in ("0q", "0y"):
                    if per in rv.index:
                        for col in ("upLast7days", "upLast30days", "downLast7Days", "downLast30days", "downLast7days"):
                            if col in rv.columns:
                                r[f"rev_{per}_{col}"] = num(rv.loc[per, col])
        except Exception as e:
            r["err"] = (r.get("err", "") + f" rev:{type(e).__name__}").strip()
        rows.append(r)
        if i % 50 == 0:
            log(f"미장 {i}/{len(codes)}")
        time.sleep(0.3)
    return rows


def kr_api(code: str, ses, debug=False) -> dict:
    """네이버 모바일 API (JSON): 컨센서스(목표가·투자의견), 추정 PER·EPS, 최근 외국인·기관 순매수."""
    r = {"code": code}
    res = ses.get(f"https://m.stock.naver.com/api/stock/{code}/integration", timeout=15)
    if debug:
        log("API 확인", code, res.status_code, res.text[:300].replace("\n", " "))
    if res.status_code != 200:
        r["err"] = f"api:{res.status_code}"
        return r
    d = res.json()
    for it in d.get("totalInfos") or []:
        k = it.get("code") or it.get("key")
        if k:
            r[f"ti_{k}"] = num(re.sub(r"[^\d.\-]", "", str(it.get("value") or "")) or None)
    for k, v in (d.get("consensusInfo") or {}).items():
        r[f"cs_{k}"] = num(v) if not isinstance(v, (dict, list)) else None
    dt_ = d.get("dealTrendInfos") or []
    if dt_:
        for k in ("foreignerPureBuyQuant", "organPureBuyQuant"):
            vals = [num(re.sub(r"[^\d.\-]", "", str(x.get(k))) or None) for x in dt_[:5] if x.get(k) is not None]
            vals = [v for v in vals if v is not None]
            if vals:
                r[k.replace("PureBuyQuant", "5d")] = sum(vals)
        hr = [num(re.sub(r"[^\d.\-]", "", str(x.get("foreignerHoldRatio"))) or None) for x in dt_[:5] if x.get("foreignerHoldRatio")]
        hr = [v for v in hr if v is not None]
        if len(hr) >= 2:
            r["foreignHoldChg5d"] = round(hr[0] - hr[-1], 3)
        x = dt_[0]
        r["flow_date"] = x.get("bizdate")
        for k in ("foreignerPureBuyQuant", "organPureBuyQuant", "individualPureBuyQuant", "closePrice", "foreignerHoldRatio"):
            if k in x:
                r[k] = num(re.sub(r"[^\d.\-]", "", str(x[k])) or None)
    return r


def kr_one(code: str, ses) -> dict:
    import pandas as pd
    r = {"code": code}
    res = ses.get(f"https://finance.naver.com/item/main.naver?code={code}", timeout=15)
    html = res.content.decode(res.encoding if res.encoding and "euc" not in res.encoding.lower() else "euc-kr", "replace")
    m = re.search(r'<p class="no_today">.*?<span class="blind">([\d,]+)</span>', html, re.S)
    r["price"] = num(m.group(1)) if m else None
    # 투자의견 · 목표주가 (컨센서스 영역)
    m = re.search(r"투자의견.*?<em>([\d.]+)</em>.*?목표주가.*?<em>([\d,]+)</em>", html, re.S)
    if m:
        r["opinion"], r["target"] = num(m.group(1)), num(m.group(2))
    # 기업실적분석 표: 연간 열 중 (E) 예상치
    try:
        for t in pd.read_html(io.StringIO(html)):
            cols = [" ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in t.columns]
            if not any("최근 연간 실적" in c for c in cols):
                continue
            t.columns = cols
            first = t.columns[0]
            idx = {str(v).strip(): i for i, v in enumerate(t[first])}
            ann = [c for c in cols if "최근 연간 실적" in c]
            est = [c for c in ann if "(E)" in c]
            for key, rowname in (("eps", "EPS(원)"), ("op", "영업이익"), ("per", "PER(배)")):
                if rowname in idx:
                    if est:
                        r[f"{key}_E"] = num(t.loc[idx[rowname], est[0]])
                        r["E_year"] = re.sub(r"[^\d.]", "", est[0].split()[-1])[:7]
                    act = [c for c in ann if "(E)" not in c]
                    if act:
                        r[f"{key}_last"] = num(t.loc[idx[rowname], act[-1]])
            break
    except Exception as e:
        r["err"] = f"table:{type(e).__name__}"
    return r


def kr_rows(n: int) -> list[dict]:
    import requests
    import swing_data as SD
    _, names = SD.kr_universe(n)
    ses = requests.Session()
    ses.headers["User-Agent"] = "Mozilla/5.0 (stock-chart bot)"
    rows, t0 = [], time.time()
    for i, (code, name) in enumerate(names.items()):
        if time.time() - t0 > 3000:
            log("시간 예산 초과", i)
            break
        try:
            r = kr_api(code, ses, debug=i < 2)
            if r.get("err") or len(r) < 4:
                r2 = kr_one(code, ses)
                r = {**r2, **{k: v for k, v in r.items() if k != "err"}} if not r2.get("err") else r
        except Exception as e:
            r = {"code": code, "err": f"{type(e).__name__}:{str(e)[:60]}"}
        r["name"] = name
        rows.append(r)
        if i % 50 == 0:
            log(f"국장 {i}/{len(names)} · 예시 {r}")
        time.sleep(0.25)
    return rows


MACRO = {"VIX(변동성 지수)": "^VIX", "원/달러": "KRW=X", "미국 10년 금리": "^TNX", "달러 지수": "DX-Y.NYB", "유가(WTI)": "CL=F", "금": "GC=F",
         "S&P500": "^GSPC", "나스닥": "^IXIC", "코스피": "^KS11", "코스닥": "^KQ11", "필라델피아 반도체": "^SOX", "하이일드 채권": "HYG",
         "업종 기술": "XLK", "업종 금융": "XLF", "업종 에너지": "XLE", "업종 헬스케어": "XLV", "업종 소비재(선택)": "XLY", "업종 필수소비": "XLP",
         "업종 산업재": "XLI", "업종 유틸리티": "XLU", "업종 소재": "XLB", "업종 부동산": "XLRE", "업종 통신": "XLC"}


def macro_rows() -> list[dict]:
    """시장 전체 정보(키 필요 없음, 야후): 변동성·환율·금리·유가·지수·미국 업종별 흐름 — 마지막 값과 1·5·20일 변화.
    지금은 매일 기록만 쌓음(매매 규칙엔 안 씀) → 충분히 쌓이면 효과를 검증해서 쓸지 정함."""
    import yfinance as yf
    out = []
    df = yf.download(list(MACRO.values()), period="3mo", interval="1d", auto_adjust=True, progress=False, group_by="ticker", threads=True)
    for name, t in MACRO.items():
        try:
            c = df[t]["Close"].dropna()
            if len(c) < 22:
                continue
            last = float(c.iloc[-1])
            out.append({"name": name, "ticker": t, "asof": c.index[-1].strftime("%Y-%m-%d"), "last": round(last, 4),
                        "chg1": round(last / float(c.iloc[-2]) - 1, 4), "chg5": round(last / float(c.iloc[-6]) - 1, 4),
                        "chg20": round(last / float(c.iloc[-21]) - 1, 4)})
        except Exception as e:
            log("시장 정보 실패", name, repr(e)[:80])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["kr", "us"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=700)
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    if a.probe:                                # 연결 확인용: 몇 종목만 넣고 추가 정보(키·로그인)가 들어오는지 봄
        rows = ([{"code": c, "name": c} for c in ("AAPL", "NVDA", "MSFT")] if a.market == "us"
                else [{"code": c, "name": c} for c in ("005930", "000660", "035420")])
    elif a.market == "us":
        import swing_data as SD
        rows = us_rows(SD.us_universe())
    else:
        rows = kr_rows(a.n)
    try:                                       # 키가 있으면 추가 정보도 같이 기록 (없으면 건너뜀)
        import more_sources as MS
        if a.market == "us":
            first = []
            try:
                import requests
                pj = requests.get("https://raw.githubusercontent.com/ehdqls5892-netizen/stock-chart/swingpicks-us/picks.json", timeout=20).json()
                first = [str(x["code"]) for x in pj.get("picks", [])]
            except Exception:
                first = []
            MS.fmp(rows, first)
            MS.finnhub(rows)
        else:
            MS.krx_short(rows)
            MS.kis(rows)
    except Exception as e:
        log("추가 정보 실패", repr(e)[:120])
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date"] + keys)
        w.writeheader()
        for r in rows:
            w.writerow({"date": day, **r})
    try:                                       # 시장 전체 정보도 같이 기록 (store/macro/날짜-시장.csv)
        mr = macro_rows()
        try:
            import more_sources as MS
            mr += MS.fred()
        except Exception as e:
            log("FRED 실패", repr(e)[:100])
        if mr:
            mp = out.parent.parent / "macro" / f"{out.stem}-{a.market}.csv"
            mp.parent.mkdir(parents=True, exist_ok=True)
            with open(mp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(mr[0]))
                w.writeheader()
                w.writerows(mr)
            log(f"시장 정보 {len(mr)}개 → {mp}")
    except Exception as e:
        log("시장 정보 기록 실패", repr(e)[:120])
    if a.probe:
        for r in rows:
            log("확인", {k: v for k, v in r.items()})
    ok = sum(1 for r in rows if not r.get("err"))
    filled = sum(1 for r in rows if (r.get("forwardEps") or r.get("eps_E") or r.get("target")
                                     or any(k.startswith("cs_") and r[k] for k in r)))
    log(f"완료 {len(rows)}개 · 오류 없음 {ok} · 추정치 있음 {filled} → {out}")


if __name__ == "__main__":
    main()
