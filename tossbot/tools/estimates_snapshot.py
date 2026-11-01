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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["kr", "us"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=700)
    a = ap.parse_args()
    if a.market == "us":
        import swing_data as SD
        rows = us_rows(SD.us_universe())
    else:
        rows = kr_rows(a.n)
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
    ok = sum(1 for r in rows if not r.get("err"))
    filled = sum(1 for r in rows if (r.get("forwardEps") or r.get("eps_E") or r.get("target")
                                     or any(k.startswith("cs_") and r[k] for k in r)))
    log(f"완료 {len(rows)}개 · 오류 없음 {ok} · 추정치 있음 {filled} → {out}")


if __name__ == "__main__":
    main()
