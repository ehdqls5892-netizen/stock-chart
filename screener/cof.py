"""COF 종목필터 — 차트 + 수급(Order Flow) 기반 전종목 스크리너.

매 영업일 장 마감 후 실행:
  1) 코스피·코스닥 전종목 당일 시세에서 후보군(거래대금 상위 + 등락률 상위)을 뽑고
  2) 후보마다 60일 시세·20일 투자자별 순매수를 받아 지표를 계산한 뒤
  3) 단타/스윙 조건 점수로 걸러 docs/data/YYYY-MM-DD.json, latest.json 으로 저장한다.

KRX 데이터는 로그인이 필요하다: 환경 변수 KRX_ID, KRX_PW (GitHub Secrets).
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from pykrx import stock

KST = timezone(timedelta(hours=9))
OUT = Path(__file__).resolve().parent.parent / "docs" / "data"

# ── 조건값 (원하면 여기만 고치면 됨) ────────────────────────
UNIVERSE_BY_VALUE = 120      # 거래대금 상위 N
UNIVERSE_BY_CHANGE = 60      # 등락률 상위 N
MIN_VALUE = 50e8             # 최소 거래대금 50억
MAX_DETAIL = 60              # 상세 조회 종목 수 상한 (요청 수 제한)
SLEEP = 0.3                  # KRX 요청 간격(초)

DANTA = dict(turnover_x=3.0, pct=3.0, close_pos=0.7)
SWING = dict(frgn_days=10, disp_max=12.0, disp_min=0.0)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def num(x, d=2):
    try:
        v = float(x)
        return None if math.isnan(v) or math.isinf(v) else round(v, d)
    except Exception:
        return None


def biz_day(today: str | None) -> str:
    if today:
        return today
    now = datetime.now(KST).strftime("%Y%m%d")
    return stock.get_nearest_business_day_in_a_week(now, prev=True)


def daily_board(date: str) -> pd.DataFrame:
    frames = []
    for mk in ("KOSPI", "KOSDAQ"):
        df = stock.get_market_ohlcv_by_ticker(date, market=mk)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["시장"] = "코스피" if mk == "KOSPI" else "코스닥"
        frames.append(df)
        time.sleep(SLEEP)
    if not frames:
        raise SystemExit(f"{date}: 시세가 비어 있습니다 (휴장일이거나 KRX 로그인 실패)")
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="first")]
    df = df[(df["거래량"] > 0) & (df["거래대금"] >= MIN_VALUE)]
    return df


def pick_universe(board: pd.DataFrame) -> list[str]:
    by_val = board.sort_values("거래대금", ascending=False).head(UNIVERSE_BY_VALUE).index
    by_chg = board.sort_values("등락률", ascending=False).head(UNIVERSE_BY_CHANGE).index
    tickers = list(dict.fromkeys(list(by_chg) + list(by_val)))
    # 강한 종목 우선으로 상세 조회 수 제한
    ranked = board.loc[tickers].assign(
        rk=lambda d: d["등락률"].rank(ascending=False) + d["거래대금"].rank(ascending=False)
    ).sort_values("rk")
    return list(ranked.index[:MAX_DETAIL])


def history(ticker: str, date: str) -> pd.DataFrame:
    start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=110)).strftime("%Y%m%d")
    return stock.get_market_ohlcv_by_date(start, date, ticker)


def flows(ticker: str, date: str) -> pd.DataFrame:
    start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=40)).strftime("%Y%m%d")
    df = stock.get_market_trading_value_by_date(start, date, ticker)
    return df.tail(20)


def xray(h: pd.DataFrame, n: int = 60) -> list[dict]:
    """캔들 위치로 거래량을 매수/매도 몫으로 나눈 값 (트레이딩뷰 X-ray 지표와 같은 식)."""
    out = []
    for idx, r in h.tail(n).iterrows():
        rng = r["고가"] - r["저가"]
        frac = (r["종가"] - r["저가"]) / rng if rng > 0 else 0.5
        out.append({
            "d": idx.strftime("%m-%d"),
            "o": int(r["시가"]), "h": int(r["고가"]), "l": int(r["저가"]), "c": int(r["종가"]),
            "v": int(r["거래량"]), "buy": int(r["거래량"] * frac),
        })
    return out


def analyze(ticker: str, row: pd.Series, date: str) -> dict | None:
    h = history(ticker, date)
    time.sleep(SLEEP)
    if h is None or len(h) < 25:
        return None
    c = h["종가"]
    ma5, ma20, ma60 = c.rolling(5).mean(), c.rolling(20).mean(), c.rolling(60).mean()
    value = h["종가"] * h["거래량"]  # 거래대금 근사(일별 시세에 거래대금 열이 없을 때 대비)
    if "거래대금" in h.columns:
        value = h["거래대금"]
    avg_val = value.iloc[-21:-1].mean()
    last = h.iloc[-1]
    rng = last["고가"] - last["저가"]
    close_pos = (last["종가"] - last["저가"]) / rng if rng > 0 else 0.5

    f = flows(ticker, date)
    time.sleep(SLEEP)
    fcol = next((k for k in f.columns if k.startswith("외국인")), None)
    icol = next((k for k in f.columns if k.startswith("기관")), None)
    frgn = f[fcol] if fcol else pd.Series(dtype=float)
    inst = f[icol] if icol else pd.Series(dtype=float)

    m = {
        "ticker": ticker,
        "name": stock.get_market_ticker_name(ticker),
        "market": row["시장"],
        "close": int(row["종가"]),
        "pct": num(row["등락률"]),
        "value_eok": num(row["거래대금"] / 1e8, 0),
        "turnover_x": num(row["거래대금"] / avg_val if avg_val else None, 1),
        "disp20": num((last["종가"] / ma20.iloc[-1] - 1) * 100, 1),
        "close_pos": num(close_pos, 2),
        "aligned": bool(ma5.iloc[-1] > ma20.iloc[-1] > (ma60.iloc[-1] if not math.isnan(ma60.iloc[-1]) else -1)),
        "high60": int(h["고가"].tail(60).max()),
        "near_high60": bool(last["종가"] >= h["고가"].tail(60).max() * 0.95),
        "frgn_today_eok": num(frgn.iloc[-1] / 1e8, 1) if len(frgn) else None,
        "inst_today_eok": num(inst.iloc[-1] / 1e8, 1) if len(inst) else None,
        "frgn_prev_eok": num(frgn.iloc[-2] / 1e8, 1) if len(frgn) > 1 else None,
        "frgn_days20": int((frgn > 0).sum()) if len(frgn) else None,
        "frgn_cum20_eok": num(frgn.sum() / 1e8, 1) if len(frgn) else None,
        "inst_cum20_eok": num(inst.sum() / 1e8, 1) if len(inst) else None,
        "bars": xray(h),
    }

    # ── 단타 조건 ──
    d_tags = []
    if (m["turnover_x"] or 0) >= DANTA["turnover_x"]: d_tags.append("거래대금 급증")
    if (m["pct"] or 0) >= DANTA["pct"]: d_tags.append("등락률")
    if (m["close_pos"] or 0) >= DANTA["close_pos"]: d_tags.append("고가권 마감")
    if (m["frgn_today_eok"] or 0) + (m["inst_today_eok"] or 0) > 0 and (m["frgn_prev_eok"] or 0) > 0:
        d_tags.append("당일+전일 수급")
    # ── 스윙 조건 ──
    s_tags = []
    if (m["frgn_days20"] or 0) >= SWING["frgn_days"]: s_tags.append("꾸준한 외국인 매수")
    if (m["frgn_cum20_eok"] or 0) + (m["inst_cum20_eok"] or 0) > 0: s_tags.append("누적 순매수")
    if m["aligned"]: s_tags.append("정배열")
    if m["disp20"] is not None and SWING["disp_min"] <= m["disp20"] <= SWING["disp_max"]: s_tags.append("과열 아닌 이격")
    if m["near_high60"]: s_tags.append("60일 고가 근접")

    m["danta_tags"], m["swing_tags"] = d_tags, s_tags
    m["danta"] = len(d_tags) >= 3 and "거래대금 급증" in d_tags
    m["swing"] = len(s_tags) >= 4 and "정배열" in s_tags
    return m


def main():
    date = biz_day(sys.argv[1] if len(sys.argv) > 1 else None)
    log("기준일", date)
    board = daily_board(date)
    universe = pick_universe(board)
    log("상세 조회", len(universe), "종목")

    rows = []
    for t in universe:
        try:
            r = analyze(t, board.loc[t], date)
            if r:
                rows.append(r)
        except Exception as e:  # 한 종목 실패가 전체를 멈추지 않게
            log("skip", t, e)

    danta = sorted([r for r in rows if r["danta"]], key=lambda r: -(r["turnover_x"] or 0))[:10]
    swing = sorted([r for r in rows if r["swing"]], key=lambda r: -(r["frgn_cum20_eok"] or 0))[:10]
    keep = {r["ticker"] for r in danta + swing}
    lite = [{k: v for k, v in r.items() if k != "bars"} for r in rows if r["ticker"] not in keep]

    report = {
        "date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        "generatedAt": datetime.now(KST).isoformat(timespec="minutes"),
        "source": "KRX (pykrx)",
        "rules": {"danta": DANTA, "swing": SWING, "min_value_eok": MIN_VALUE / 1e8},
        "danta": danta,
        "swing": swing,
        "universe": lite,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    (OUT / f"{report['date']}.json").write_text(body, encoding="utf-8")
    (OUT / "latest.json").write_text(body, encoding="utf-8")
    idx_path = OUT / "index.json"
    dates = json.loads(idx_path.read_text()) if idx_path.exists() else []
    dates = sorted(set(dates + [report["date"]]), reverse=True)[:120]
    idx_path.write_text(json.dumps(dates), encoding="utf-8")
    log(f"완료: 단타 {len(danta)} · 스윙 {len(swing)} · 전체 {len(rows)}")


if __name__ == "__main__":
    main()
