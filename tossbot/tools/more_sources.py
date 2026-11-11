"""키가 있을 때만 붙는 추가 정보 (키가 없으면 조용히 건너뜀). 매일 기록(estimates 브랜치)에 같이 쌓음.
지금은 '기록'만 — 충분히 쌓이면 효과를 검증해서 점수에 쓸지 정함.

  FINNHUB_KEY                  미장: 애널리스트 의견 수(적극매수·매수·중립·매도)와 한 달 전 대비 변화
  FMP_KEY                      미장: 목표가 컨센서스(평균·최고·최저) — 무료는 하루 250번이라 후보·앞쪽 종목부터
  FRED_KEY                     미국 금리(2년·10년·장단기 차)·기준금리·물가·실업률·하이일드 스프레드
  KIS_APP_KEY / KIS_APP_SECRET 국장: 종목별 공매도 비중, 신용잔고율, 외국인·기관·개인 순매수(최근 30일)
키는 GitHub Secrets 에만 넣음 (로그에 키를 찍지 않음).
"""
from __future__ import annotations

import os
import sys
import time

import requests


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def finnhub(rows: list, budget: int = 900) -> int:
    key = os.getenv("FINNHUB_KEY", "").strip()
    if not key:
        return 0
    ses, n, t0 = requests.Session(), 0, time.time()
    for i, r in enumerate(rows):
        if time.time() - t0 > budget:
            log("Finnhub: 시간 예산 초과", i)
            break
        try:
            res = ses.get("https://finnhub.io/api/v1/stock/recommendation", params={"symbol": str(r["code"]).replace(".", "-"), "token": key}, timeout=15)
            if res.status_code == 429:
                time.sleep(20)
                continue
            if res.status_code != 200:
                if i < 2:
                    log("Finnhub 응답", r["code"], res.status_code, res.text[:120])
                continue
            js = res.json() or []
            if js:
                a = js[0]
                r.update(fh_period=a.get("period"), fh_sb=a.get("strongBuy"), fh_b=a.get("buy"), fh_h=a.get("hold"), fh_s=a.get("sell"), fh_ss=a.get("strongSell"))
                if len(js) > 1:
                    b = js[1]
                    r["fh_buy_chg"] = (a.get("strongBuy", 0) + a.get("buy", 0)) - (b.get("strongBuy", 0) + b.get("buy", 0))
                    r["fh_sell_chg"] = (a.get("sell", 0) + a.get("strongSell", 0)) - (b.get("sell", 0) + b.get("strongSell", 0))
                n += 1
        except Exception as e:
            if i < 2:
                log("Finnhub 실패", r["code"], repr(e)[:100])
        time.sleep(1.05)                       # 무료: 1분에 60번
    log(f"Finnhub 애널리스트 의견 {n}종목")
    return n


def fmp(rows: list, first: list | None = None, limit: int = 220) -> int:
    key = os.getenv("FMP_KEY", "").strip()
    if not key:
        return 0
    by = {str(r["code"]): r for r in rows}
    order = [c for c in (first or []) if c in by] + [c for c in by if c not in set(first or [])]
    ses, n = requests.Session(), 0
    for i, code in enumerate(order[:limit]):
        try:
            res = ses.get("https://financialmodelingprep.com/stable/price-target-consensus", params={"symbol": code.replace(".", "-"), "apikey": key}, timeout=15)
            if res.status_code != 200:
                if i < 2:
                    log("FMP 응답", code, res.status_code, res.text[:120])
                if res.status_code in (401, 402, 403, 429):
                    break
                continue
            js = res.json() or []
            a = js[0] if isinstance(js, list) and js else (js if isinstance(js, dict) else {})
            if a.get("targetConsensus") is not None:
                by[code].update(fmp_tgt=_num(a.get("targetConsensus")), fmp_tgt_med=_num(a.get("targetMedian")),
                                fmp_tgt_hi=_num(a.get("targetHigh")), fmp_tgt_lo=_num(a.get("targetLow")))
                n += 1
        except Exception as e:
            if i < 2:
                log("FMP 실패", code, repr(e)[:100])
        time.sleep(0.3)
    log(f"FMP 목표가 {n}종목")
    return n


FRED = {"미국 10년 금리(FRED)": "DGS10", "미국 2년 금리": "DGS2", "장단기 금리 차(10년-2년)": "T10Y2Y", "미국 기준금리": "FEDFUNDS",
        "미국 물가(CPI)": "CPIAUCSL", "미국 실업률": "UNRATE", "하이일드 스프레드": "BAMLH0A0HYM2"}


def fred() -> list:
    key = os.getenv("FRED_KEY", "").strip()
    if not key:
        return []
    out = []
    for name, sid in FRED.items():
        try:
            res = requests.get("https://api.stlouisfed.org/fred/series/observations",
                               params={"series_id": sid, "api_key": key, "file_type": "json", "sort_order": "desc", "limit": 30}, timeout=20)
            if res.status_code != 200:
                log("FRED 응답", sid, res.status_code, res.text[:120])
                continue
            ob = [(o["date"], _num(o["value"])) for o in res.json().get("observations", []) if _num(o.get("value")) is not None]
            if len(ob) >= 2:
                last = ob[0][1]
                ch = lambda k: round(last - ob[min(k, len(ob) - 1)][1], 4)       # 금리·비율은 '몇 %p 변했나'로
                out.append({"name": name, "ticker": "FRED:" + sid, "asof": ob[0][0], "last": last, "chg1": ch(1), "chg5": ch(5), "chg20": ch(20)})
        except Exception as e:
            log("FRED 실패", sid, repr(e)[:100])
    log(f"FRED 경제 지표 {len(out)}개")
    return out


def krx_short(rows: list) -> int:
    """국장 공매도(한국거래소, 이미 등록된 KRX 로그인 사용 — 증권사 계좌·키 필요 없음).
    하루치를 시장 전체로 한 번에 받음: 공매도 거래 비중(최근일·5일 평균), 공매도 잔고 비중(이틀 늦게 나옴)."""
    if not os.getenv("KRX_ID") or not os.getenv("KRX_PW"):
        return 0
    import datetime as dt
    try:
        from pykrx import stock
    except Exception as e:
        log("pykrx 없음", repr(e)[:80])
        return 0
    by = {str(r["code"]): r for r in rows}
    vol, bal, days, d = {}, {}, 0, dt.date.today()
    for _ in range(12):                       # 최근 거래일 5일치
        if days >= 5:
            break
        ds = d.strftime("%Y%m%d")
        d -= dt.timedelta(days=1)
        got = False
        for mk in ("KOSPI", "KOSDAQ"):
            try:
                df = stock.get_shorting_volume_by_ticker(ds, market=mk)
                if df is None or df.empty:
                    continue
                if days == 0 and mk == "KOSPI":
                    log("KRX 공매도 거래 확인", ds, list(df.columns)[:8], len(df))
                col = next((c for c in df.columns if "비중" in str(c)), None)
                if col is None:
                    continue
                for code, v in df[col].items():
                    x = _num(v)
                    if x is not None:
                        vol.setdefault(str(code), []).append(x)
                got = True
            except Exception as e:
                log("KRX 공매도 거래 실패", ds, mk, repr(e)[:120])
            time.sleep(1.0)
            if not bal or days < 3:
                try:
                    df = stock.get_shorting_balance_by_ticker(ds, market=mk)
                    if df is not None and not df.empty:
                        col = next((c for c in df.columns if "비중" in str(c)), None)
                        if col is not None:
                            for code, v in df[col].items():
                                x = _num(v)
                                if x is not None and str(code) not in bal:
                                    bal[str(code)] = x
                except Exception as e:
                    log("KRX 공매도 잔고 실패", ds, mk, repr(e)[:120])
                time.sleep(1.0)
        days += got
    n = 0
    for code, r in by.items():
        v = vol.get(code)
        if v:
            r.update(krx_short_pct=round(v[0], 2), krx_short_pct5=round(sum(v) / len(v), 2))
            n += 1
        if code in bal:
            r["krx_short_bal_pct"] = round(bal[code], 2)
    log(f"KRX 공매도 {n}종목 (거래일 {days}일 · 잔고 {len(bal)}종목)")
    return n


KIS = "https://openapi.koreainvestment.com:9443"


def kis(rows: list, budget: int = 1500) -> int:
    ak, sk = os.getenv("KIS_APP_KEY", "").strip(), os.getenv("KIS_APP_SECRET", "").strip()
    if not ak or not sk:
        return 0
    import datetime as dt
    ses = requests.Session()
    try:
        tk = ses.post(KIS + "/oauth2/tokenP", json={"grant_type": "client_credentials", "appkey": ak, "appsecret": sk}, timeout=20).json().get("access_token")
    except Exception as e:
        log("KIS 토큰 실패", repr(e)[:100])
        return 0
    if not tk:
        log("KIS 토큰을 못 받았어요 (키를 확인해 주세요)")
        return 0
    today = dt.date.today()
    d1, d0 = today.strftime("%Y%m%d"), (today - dt.timedelta(days=14)).strftime("%Y%m%d")

    def get(path, tr, params, dbg):
        res = ses.get(KIS + path, headers={"authorization": f"Bearer {tk}", "appkey": ak, "appsecret": sk, "tr_id": tr, "custtype": "P",
                                           "content-type": "application/json; charset=utf-8"}, params=params, timeout=15)
        js = res.json() if res.status_code == 200 else {}
        if dbg:
            o = js.get("output2") or js.get("output") or []
            log("KIS 확인", tr, res.status_code, js.get("msg1"), (list(o[0])[:12] if isinstance(o, list) and o else (list(o)[:12] if isinstance(o, dict) else o)))
        return js
    n, t0 = 0, time.time()
    for i, r in enumerate(rows):
        if time.time() - t0 > budget:
            log("KIS: 시간 예산 초과", i)
            break
        code, dbg, got = str(r["code"]), i < 2, False
        try:   # 투자자별 순매수 (최근 30일)
            o = get("/uapi/domestic-stock/v1/quotations/inquire-investor", "FHKST01010900", {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, dbg).get("output") or []
            o = [x for x in o if _num(x.get("frgn_ntby_qty")) is not None][:5]
            if o:
                r.update(kis_frgn5=sum(_num(x.get("frgn_ntby_qty")) or 0 for x in o), kis_orgn5=sum(_num(x.get("orgn_ntby_qty")) or 0 for x in o),
                         kis_prsn5=sum(_num(x.get("prsn_ntby_qty")) or 0 for x in o))
                got = True
        except Exception as e:
            if dbg:
                log("KIS 투자자 실패", code, repr(e)[:100])
        time.sleep(0.07)
        try:   # 공매도 일별 추이
            js = get("/uapi/domestic-stock/v1/quotations/daily-short-sale", "FHPST04830000",
                     {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": d0, "FID_INPUT_DATE_2": d1}, dbg)
            o = js.get("output2") or []
            v = [_num(x.get("ssts_vol_rlim")) for x in o[:5] if _num(x.get("ssts_vol_rlim")) is not None]
            if v:
                r.update(kis_short_pct=v[0], kis_short_pct5=round(sum(v) / len(v), 2))
                got = True
        except Exception as e:
            if dbg:
                log("KIS 공매도 실패", code, repr(e)[:100])
        time.sleep(0.07)
        try:   # 신용잔고 일별 추이
            js = get("/uapi/domestic-stock/v1/quotations/daily-credit-balance", "FHPST04760000",
                     {"FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20476", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": d1}, dbg)
            o = js.get("output") or []
            v = [_num(x.get("whol_loan_rmnd_rate")) for x in (o if isinstance(o, list) else [o])[:6] if _num(x.get("whol_loan_rmnd_rate")) is not None]
            if v:
                r.update(kis_credit_rate=v[0], kis_credit_chg5=round(v[0] - v[-1], 2))
                got = True
        except Exception as e:
            if dbg:
                log("KIS 신용 실패", code, repr(e)[:100])
        time.sleep(0.07)
        n += got
    log(f"KIS 공매도·신용·투자자 {n}종목")
    return n
