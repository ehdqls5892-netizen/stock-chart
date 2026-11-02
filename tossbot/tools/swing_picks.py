"""스윙 신호 종목 만들기 (장 끝난 뒤 GitHub Actions 에서).  python tools/swing_picks.py kr|us --out out
→ out/picks.json  {market, date(신호 난 날), regime(시장 국면 좋음?), index, picks:[{code,name,setup,close,stop,atr,score,reason}]}
백테스트(tools/swingbt.py)와 같은 함수로 신호를 판단한다."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import swing_data as SD  # noqa: E402
import swingbt as B  # noqa: E402

SETUPS = {"kr": {"ep": {"gap": 0.04, "volx": 3}}, "us": {"hi52": {"volx": 1.5}, "dip": {"rsi": 10, "drop": 0.10}}}
NAMES = {"ep": "큰 재료 갭(갭 상승+거래량 폭증+강한 마감)", "hi52": "52주 신고가 돌파+거래량",
         "dip": "하락장 저점 매수(200일선 위 종목·3일 RSI 10↓·20일 고점 -10%↓)"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["kr", "us"])
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    m = a.market
    if m == "kr":
        yfs, names = SD.kr_universe(700)
        bars = SD.yf_bars(yfs, 2)
        idx = SD.yf_bars({"KOSPI": "^KS11"}, 2).get("KOSPI")
    else:
        names = SD.us_universe()
        bars = SD.yf_bars({c: c.replace(".", "-") for c in names}, 2)
        idx = SD.yf_bars({"SPY": "SPY"}, 2).get("SPY")
    out = build(m, bars, names, idx)
    if m == "us":
        try:
            enrich_us(out["picks"], bars)
        except Exception as e:
            print("실적·애널리스트 정보 실패:", e, file=sys.stderr)
    try:
        out["etf"] = etf_metrics(m)
    except Exception as e:
        print("ETF 상태 계산 실패:", e, file=sys.stderr)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "picks.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "picks"}, ensure_ascii=False), len(out["picks"]), "picks")


def react_day(ts: str) -> str:
    """발표 시각(뉴욕) → 주가 반응일: 정오 전 발표면 그날, 뒤면 다음 평일."""
    d = dt.date.fromisoformat(ts[:10])
    if int(ts[11:13] or 0) >= 12:
        d += dt.timedelta(days=1)
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
    return d.isoformat()


def enrich_us(picks, bars):
    """후보마다 다음 실적일·과거 실적 갭 크기·애널리스트 전망치 변화·목표가·업종을 붙임 (ev)."""
    import yfinance as yf
    today = dt.date.today().isoformat()
    for p in picks:
        ev = {}
        t = yf.Ticker(p["code"].replace(".", "-"))
        try:
            df = t.get_earnings_dates(limit=12)
            rows = sorted((ts.strftime("%Y-%m-%d %H:%M"), r.get("Reported EPS")) for ts, r in (df if df is not None else []).iterrows())
            fut = [ts for ts, rep in rows if ts[:10] >= today and rep != rep]
            if fut:
                ev["earn"], ev["react"] = fut[0], react_day(fut[0])
            b = bars.get(p["code"]) or []
            pos = {x[0]: i for i, x in enumerate(b)}
            gaps = []
            for ts, rep in rows:
                if ts[:10] >= today:
                    continue
                rd = react_day(ts)
                i = pos.get(rd)
                if i:
                    gaps.append(abs(b[i][1] / b[i - 1][4] - 1))
            if gaps:
                ev["gap_avg"] = round(sum(gaps[-8:]) / len(gaps[-8:]), 4)
                ev["gap_max"] = round(max(gaps[-8:]), 4)
        except Exception as e:
            ev["err"] = f"earn:{type(e).__name__}"
        try:
            info = t.get_info() or {}
            ev["sector"], ev["industry"] = info.get("sector"), info.get("industry")
            px, tg = info.get("currentPrice") or p["close"], info.get("targetMeanPrice")
            if tg and px:
                ev["tgt_up"] = round(tg / px - 1, 4)
            if info.get("recommendationMean"):
                ev["rec"] = round(float(info["recommendationMean"]), 2)
            ev["n_analysts"] = info.get("numberOfAnalystOpinions")
        except Exception as e:
            ev["err"] = (ev.get("err", "") + f" info:{type(e).__name__}").strip()
        try:
            tr = t.eps_trend
            if tr is not None and "0q" in tr.index:
                ev["eps0q"] = float(tr.loc["0q", "current"])
                ev["eps0q_30d"] = float(tr.loc["0q", "30daysAgo"])
            rv = t.eps_revisions
            if rv is not None and "0q" in rv.index:
                ev["up30"] = float(rv.loc["0q", "upLast30days"]) if "upLast30days" in rv.columns else None
                for k in ("downLast30days", "downLast30Days"):
                    if k in rv.columns:
                        ev["down30"] = float(rv.loc["0q", k])
        except Exception as e:
            ev["err"] = (ev.get("err", "") + f" eps:{type(e).__name__}").strip()
        ev = {k: v for k, v in ev.items() if v is not None and v == v}
        p["ev"] = ev
        print(p["code"], ev, file=sys.stderr)


def etf_metrics(m):
    """추천용 ETF 상태: 현재가·1년 수익률·52주 고점 대비·200일선 위/아래·배당수익률(최근 1년)."""
    import yfinance as yf
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
    items = ((cfg.get("etf_long") or {}).get("list") or {}).get(m.upper()) or []
    tick = {code: (code + ".KS" if m == "kr" else code) for code, _, _ in items}
    bars = SD.yf_bars(tick, 2)
    out = []
    for code, name, w in items:
        b = bars.get(code)
        if not b or len(b) < 210:
            out.append({"code": code, "name": name, "w": w})
            continue
        c = [x[4] for x in b]
        ma200 = sum(c[-200:]) / 200
        hi = max(x[2] for x in b[-252:])
        d = {"code": code, "name": name, "w": w, "close": round(c[-1], 4), "date": b[-1][0],
             "ma200": round(ma200, 4), "above200": c[-1] > ma200, "from_hi": round(c[-1] / hi - 1, 4),
             "ret1y": round(c[-1] / c[-253] - 1, 4) if len(c) > 253 else None}
        try:
            dv = yf.Ticker(tick[code]).dividends
            if dv is not None and len(dv):
                last = dv[dv.index >= dv.index[-1] - __import__("pandas").Timedelta(days=365)]
                d["div_yield"] = round(float(last.sum()) / c[-1], 4)
        except Exception:
            pass
        out.append(d)
    return out


def build(m, bars, names, idx):
    S = {c: B.indicators(B.prep(b)) for c, b in bars.items() if len(b) >= 120}
    last = max(s["d"][-1] for s in S.values())
    picks = []
    for name, P in SETUPS[m].items():
        for d, code, sc, stop in B.signals(name, S, {**P, "include_last": True}, {"bars": bars}, m):
            if d != last:
                continue
            s = S[code]
            i = s["pos"][d]
            picks.append({"code": code, "name": names.get(code, code), "setup": name, "close": round(float(s["c"][i]), 4),
                          "stop": round(float(stop), 4), "atr": round(float(s["atr"][i]), 4), "score": round(float(sc), 3),
                          "reason": f"{NAMES[name]} · " + (f"200일선 위 {(s['c'][i] / s['ma200'][i] - 1) * 100:.0f}%" if name == "dip" else f"거래량 {s['v'][i] / s['v20'][i - 1]:.1f}배")
                          + f" · 손절 {(stop / s['c'][i] - 1) * 100:.1f}%"})
    picks.sort(key=lambda x: -x["score"])
    # 방식마다 상위 20개씩 (점수 단위가 달라 한 줄로 자르면 한쪽이 밀려남 — 2026-10-30 저점 매수 14개 중 5개만 남았던 문제)
    per = {}
    picks = [x for x in picks if per.setdefault(x["setup"], []).append(x) or len(per[x["setup"]]) <= 20]
    reg = False                      # 지수를 못 받으면 '모름' → 새로 안 삼
    ix = None
    if idx:
        si = B.prep(idx)
        m50 = B.sma(si["c"], 50)
        reg = bool(si["c"][-1] > m50[-1])
        ix = {"close": round(float(si["c"][-1]), 2), "ma50": round(float(m50[-1]), 2), "date": si["d"][-1]}
    out = {"market": m.upper(), "date": last, "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
           "regime": reg, "index": ix, "universe": len(S), "picks": picks}
    return out


if __name__ == "__main__":
    main()
