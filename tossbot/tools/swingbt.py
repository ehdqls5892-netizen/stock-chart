"""스윙 전략 백테스트 (일봉, 포트폴리오 단위). 신호는 t일 종가로 판단 → t+1일 시가에 매수. 미래 정보 없음.

  python tools/swingbt.py swing-us.pkl.gz            # 기본 실험 묶음
  python tools/swingbt.py swing-kr.pkl.gz --only ep  # 하나만

전략 (애널리스트 관점: 남들보다 '오래' 버티는 쪽의 우위)
  ep    큰 재료 갭: 갭 상승 + 거래량 폭증 + 강하게 마감 (실적·수주 같은 큰 재료가 났다는 흔적)
  pead  실적 서프라이즈 후 추세 이어가기 (미장: 실제 EPS 서프라이즈 + 발표 반응 상승)
  rs    상대강도 상위 종목의 눌림목 (시장보다 강한 종목이 잠깐 쉴 때)
  hi52  52주 신고가 돌파 + 거래량
  flow  국장 외국인·기관 연속 순매수 + 상승 추세
공통: 시장 국면 필터(지수 50일선 위), 손절 = ATR 기반, 10일선 이탈·추적 손절·기간 손절, 비용 포함.
"""
from __future__ import annotations

import argparse
import gzip
import math
import pickle
import statistics as stt
from collections import defaultdict

import numpy as np

COST = {"kr": 0.0035, "us": 0.0015}     # 왕복 수수료·세금·미끄러짐


def prep(bars):
    d = [b[0] for b in bars]
    o, h, l, c, v = (np.array([b[k] for b in bars], float) for k in (1, 2, 3, 4, 5))
    return {"d": d, "o": o, "h": h, "l": l, "c": c, "v": v, "pos": {x: i for i, x in enumerate(d)}}


def sma(a, n):
    out = np.full(len(a), np.nan)
    if len(a) >= n:
        cs = np.cumsum(np.insert(a, 0, 0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def atr(s, n=20):
    pc = np.roll(s["c"], 1)
    pc[0] = s["c"][0]
    tr = np.maximum(s["h"], pc) - np.minimum(s["l"], pc)
    return sma(tr, n)


def rsi(c, n):
    d = np.diff(c, prepend=c[0])
    g, l = np.clip(d, 0, None), np.clip(-d, 0, None)
    ag, al = sma(g, n), sma(l, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100 - 100 / (1 + ag / np.where(al == 0, 1e-9, al))


def indicators(s):
    c, v = s["c"], s["v"]
    s["ma10"], s["ma20"], s["ma50"], s["ma150"], s["ma200"] = sma(c, 10), sma(c, 20), sma(c, 50), sma(c, 150), sma(c, 200)
    s["v20"] = sma(v, 20)
    s["tv20"] = sma(c * v, 20)
    s["atr"] = atr(s)
    s["rsi3"] = rsi(c, 3)
    s["ret126"] = np.r_[np.full(126, np.nan), c[126:] / c[:-126] - 1] if len(c) > 126 else np.full(len(c), np.nan)
    s["hi252"] = np.r_[np.full(1, np.nan), np.array([c[max(0, i - 251):i].max() if i >= 200 else np.nan for i in range(1, len(c))])]
    return s


def regime(idx, mode):
    """날짜 → 공격 가능 여부. mode: none / ma50 / ma50up(50일선 위 + 50일선 상승)"""
    if mode == "none" or not idx:
        return lambda d: True
    s = prep(idx)
    m50 = sma(s["c"], 50)
    ok = {}
    for i, d in enumerate(s["d"]):
        if np.isnan(m50[i]):
            ok[d] = True
            continue
        good = s["c"][i] > m50[i]
        if mode == "ma50up":
            good = good and i >= 10 and m50[i] > m50[i - 10]
        ok[d] = bool(good)
    return lambda d: ok.get(d, True)


def signals(name, S, P, data, mk):
    """(날짜, 코드, 점수, 손절가 기준) 목록. 날짜 = 신호 난 날(종가 기준)."""
    out = []
    minp = 1000 if mk == "kr" else 5
    mintv = P.get("mintv", 3e9 if mk == "kr" else 3e7)
    # 상대강도 순위 (날짜별 ret126 백분위)
    rsrank = {}
    if name == "rs":
        byd = defaultdict(list)
        for code, s in S.items():
            for i in range(len(s["d"])):
                if not np.isnan(s["ret126"][i]):
                    byd[s["d"][i]].append((s["ret126"][i], code))
        for d, xs in byd.items():
            xs.sort()
            n = len(xs)
            for k, (_, code) in enumerate(xs):
                rsrank[(d, code)] = k / max(1, n - 1)
    earn = data.get("earn") or {}
    flow = data.get("flow") or {}
    for code, s in S.items():
        c, o, h, l, v = s["c"], s["o"], s["h"], s["l"], s["v"]
        if name == "flow":
            fl = {x[0]: x[1] + x[2] for x in flow.get(code, [])}
            fseries = np.array([fl.get(d, np.nan) for d in s["d"]])
        if name == "pead":
            ev = {}
            for e in earn.get(code, []):
                if e[3] is None:
                    continue
                ev[e[0][:10]] = (e[3], e[0][11:16])
        for i in range(60, len(c) if P.get("include_last") else len(c) - 1):   # 실전 신호는 마지막 날까지
            if c[i] < minp or not (s["tv20"][i] >= mintv) or np.isnan(s["atr"][i]):
                continue
            a = s["atr"][i]
            if name == "ep":
                gap = o[i] / c[i - 1] - 1
                rng = h[i] - l[i]
                if (gap >= P.get("gap", 0.04) and v[i] >= P.get("volx", 3) * s["v20"][i - 1] and c[i] >= o[i]
                        and rng > 0 and (c[i] - l[i]) / rng >= P.get("closepos", 0.6)
                        and (not P.get("trend") or c[i] > s["ma50"][i])):
                    out.append((s["d"][i], code, v[i] / s["v20"][i - 1], min(l[i], c[i] - P.get("stop_atr", 1.5) * a)))
            elif name == "pead":
                # 발표가 장 전이면 그날, 장 후면 다음 날이 반응일
                for back in (0, 1):
                    d0 = s["d"][i - back]
                    if d0 in ev:
                        sp, hm = ev[d0]
                        react = i if (back == 0 and hm < "12:00") or (back == 1 and hm >= "12:00") else None
                        if react == i and sp >= P.get("surprise", 5) and c[i] / c[i - 1] - 1 >= P.get("react", 0.02) \
                                and c[i] >= o[i] and (not P.get("trend") or c[i] > s["ma50"][i]):
                            out.append((s["d"][i], code, sp, min(l[i], c[i] - P.get("stop_atr", 1.5) * a)))
            elif name == "rs":
                r = rsrank.get((s["d"][i], code))
                if (r is not None and r >= P.get("rs", 0.9) and c[i] > s["ma50"][i] > s["ma150"][i]
                        and s["rsi3"][i - 1] < P.get("rsi", 30) and c[i] > o[i] and c[i] > c[i - 1]):
                    out.append((s["d"][i], code, r, c[i] - P.get("stop_atr", 2.0) * a))
            elif name == "dip":
                # 하락 국면용 저점 매수: 200일선 위(장기 우상향) 종목이 3일 RSI 10 아래·20일 고점 대비 10% 넘게 밀림
                hi20 = c[max(0, i - 20):i + 1].max()
                if ((not P.get("need200", True) or c[i] > s["ma200"][i]) and s["rsi3"][i] < P.get("rsi", 10)
                        and c[i] <= hi20 * (1 - P.get("drop", 0.10))):
                    out.append((s["d"][i], code, -s["rsi3"][i], c[i] - P.get("stop_atr", 2.5) * a))
            elif name == "hi52":
                if (not np.isnan(s["hi252"][i]) and c[i] > s["hi252"][i] and v[i] >= P.get("volx", 1.5) * s["v20"][i - 1]
                        and c[i] > s["ma50"][i]):
                    out.append((s["d"][i], code, v[i] / s["v20"][i - 1], c[i] - P.get("stop_atr", 2.0) * a))
            elif name == "flow":
                w = fseries[i - 4:i + 1]
                if np.isnan(w).any():
                    continue
                if ((w > 0).sum() >= P.get("days", 4) and w.sum() >= P.get("flowx", 0.3) * s["tv20"][i]
                        and c[i] > s["ma20"][i] > s["ma50"][i] and c[i] / c[i - 5] - 1 < P.get("maxrun", 0.15)):
                    out.append((s["d"][i], code, w.sum() / s["tv20"][i], c[i] - P.get("stop_atr", 2.0) * a))
    return out


def simulate(sigs, S, mk, P, reg, dates):
    """포트폴리오 시뮬레이션. 리스크 1%/거래, 최대 보유 N, 종목당 최대 비중."""
    eq0 = 1.0
    cash, eq = eq0, eq0
    pos = {}            # code → dict
    trades, curve = [], []
    by_day = defaultdict(list)
    for d, code, sc, stop in sigs:
        by_day[d].append((sc, code, stop))
    cost = COST[mk]
    maxpos, risk, cap = P.get("maxpos", 6), P.get("risk", 0.01), P.get("cap", 0.2)
    pend = []
    for di, d in enumerate(dates):
        # 1) 어제 신호 → 오늘 시가 매수
        for sc, code, stop in pend:
            s = S[code]
            i = s["pos"].get(d)
            if i is None or code in pos or len(pos) >= maxpos:
                continue
            e = s["o"][i]
            if stop >= e * 0.995:
                continue
            per = e - stop
            mv = sum(p["q"] * S[k]["c"][S[k]["pos"].get(d, p["li"])] for k, p in pos.items())
            equity = cash + mv
            q = min(equity * risk / per, equity * cap / e, cash / e)
            if q * e < equity * 0.02:
                continue
            cash -= q * e * (1 + cost / 2)
            pos[code] = {"q": q, "e": e, "stop": stop, "R": per, "d0": d, "i0": i, "li": i, "hi": e, "why": ""}
        pend = []
        # 2) 보유 관리 (오늘 장중 손절 → 종가 기준 청산 신호는 내일 시가)
        for code in list(pos):
            p = pos[code]
            s = S[code]
            i = s["pos"].get(d)
            if i is None:
                continue
            p["li"] = i
            if p.get("exit_next"):
                x = s["o"][i]
                cash += p["q"] * x * (1 - cost / 2)
                trades.append((p["d0"], code, (x / p["e"] - 1) - cost, (x - p["e"]) / p["R"], p["exit_next"], i - p["i0"]))
                del pos[code]
                continue
            if s["l"][i] <= p["stop"]:
                x = min(p["stop"], s["o"][i])
                cash += p["q"] * x * (1 - cost / 2)
                trades.append((p["d0"], code, (x / p["e"] - 1) - cost, (x - p["e"]) / p["R"], "손절", i - p["i0"]))
                del pos[code]
                continue
            c = s["c"][i]
            p["hi"] = max(p["hi"], c)
            held = i - p["i0"]
            a = s["atr"][i]
            # 추적 손절: +1R 넘으면 본전, 이후 최고 종가 − k·ATR
            if p["hi"] >= p["e"] + P.get("be_R", 1) * p["R"]:
                p["stop"] = max(p["stop"], p["e"] * (1 + cost))
            if P.get("ratchet") and p["hi"] > p["e"]:          # 이익 잠금: 최고 이익의 일정 비율은 지킴
                p["stop"] = max(p["stop"], p["e"] + (p["hi"] - p["e"]) * P["ratchet"])
            if p["hi"] >= p["e"] + P.get("trail_after_R", 2) * p["R"]:
                p["stop"] = max(p["stop"], p["hi"] - P.get("trail_atr", 3) * a)
            if P.get("exit_ma") and held >= 2 and c < s[P["exit_ma"]][i] and c < p["e"] + 3 * p["R"]:
                p["exit_next"] = f"{P['exit_ma']} 이탈"
            elif held >= P.get("time_stop", 10) and c < p["e"] + P.get("time_need_R", 0.5) * p["R"]:
                p["exit_next"] = "기간(안 오름)"
            elif held >= P.get("max_hold", 60):
                p["exit_next"] = "최대 보유"
        # 3) 오늘 신호 → 내일 매수 대기 (시장 국면 좋을 때만)
        if reg(d) and by_day.get(d):
            pend = sorted(by_day[d], reverse=True)[: max(0, maxpos)]
        mv = sum(p["q"] * S[k]["c"][p["li"]] for k, p in pos.items())
        curve.append((d, cash + mv, len(pos)))
    return trades, curve


def report(name, trades, curve, years=True):
    if not trades:
        return f"{name:34s} 매매 없음", None
    rs = [t[2] for t in trades]
    R = [t[3] for t in trades]
    w = sum(x for x in rs if x > 0)
    l = -sum(x for x in rs if x <= 0)
    eqs = [x[1] for x in curve]
    peak, mdd = eqs[0], 0
    for e in eqs:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    tot = eqs[-1] / eqs[0] - 1
    n_years = len(curve) / 252
    cagr = (eqs[-1] / eqs[0]) ** (1 / n_years) - 1 if n_years > 0 and eqs[-1] > 0 else -1
    dr = [eqs[i] / eqs[i - 1] - 1 for i in range(1, len(eqs))]
    sh = stt.mean(dr) / stt.pstdev(dr) * math.sqrt(252) if len(dr) > 2 and stt.pstdev(dr) else 0
    expo = stt.mean(x[2] for x in curve)
    by_y = defaultdict(list)
    for t in trades:
        by_y[t[0][:4]].append(t[2])
    yr = defaultdict(float)
    for i in range(1, len(curve)):
        yr[curve[i][0][:4]] += math.log(curve[i][1] / curve[i - 1][1]) if curve[i - 1][1] > 0 and curve[i][1] > 0 else 0
    ys = " ".join(f"{y}:{(math.exp(v) - 1) * 100:+.0f}%" for y, v in sorted(yr.items()))
    line = (f"{name:34s} {len(trades):4d}건 승률 {sum(x > 0 for x in rs) / len(rs) * 100:3.0f}% PF {w / l if l else 9.9:4.2f} "
            f"평균R {stt.mean(R):+.2f} 보유 {stt.mean(t[5] for t in trades):4.1f}일 | 총 {tot * 100:+6.1f}% 연 {cagr * 100:+5.1f}% "
            f"최대낙폭 {mdd * 100:4.1f}% 샤프 {sh:4.2f} 평균보유 {expo:.1f}종목 | {ys}")
    return line, {"tot": tot, "cagr": cagr, "mdd": mdd, "sharpe": sh, "pf": w / l if l else 9.9, "n": len(trades), "years": dict(yr)}


def bench(idx, dates):
    s = prep(idx)
    c = [s["c"][s["pos"][d]] for d in dates if d in s["pos"]]
    if len(c) < 2:
        return ""
    yr = defaultdict(float)
    ds = [d for d in dates if d in s["pos"]]
    for i in range(1, len(c)):
        yr[ds[i][:4]] += math.log(c[i] / c[i - 1])
    return f"지수 그냥 보유: 총 {(c[-1] / c[0] - 1) * 100:+.1f}% | " + " ".join(f"{y}:{(math.exp(v) - 1) * 100:+.0f}%" for y, v in sorted(yr.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    data = pickle.load(gzip.open(a.data, "rb"))
    mk = "kr" if "kr" in a.data else "us"
    S = {c: indicators(prep(b)) for c, b in data["bars"].items() if len(b) >= 200}
    idx = data["index"].get("KOSPI" if mk == "kr" else "SPY")
    dates = sorted({d for s in S.values() for d in s["d"]})
    dates = dates[200:]                 # 지표 준비 기간 제외
    print(f"{mk}: 종목 {len(S)} · 기간 {dates[0]} ~ {dates[-1]} ({len(dates)}일) · 수급 {len(data.get('flow') or {})} · 실적 {len(data.get('earn') or {})}")
    print(bench(idx, dates))
    base_exit = {"exit_ma": "ma10", "time_stop": 10, "time_need_R": 0.5, "trail_after_R": 2, "trail_atr": 3, "max_hold": 60}
    exps = [
        ("ep", {"gap": 0.04, "volx": 3}, "ma50"),
        ("ep", {"gap": 0.04, "volx": 3}, "none"),
        ("ep", {"gap": 0.06, "volx": 4, "trend": True}, "ma50"),
        ("ep", {"gap": 0.03, "volx": 2, "trend": True}, "ma50"),
        ("ep", {"gap": 0.04, "volx": 3, "exit_ma": "ma20"}, "ma50"),
        ("ep", {"gap": 0.04, "volx": 3, "exit_ma": None, "time_stop": 15}, "ma50"),
        ("pead", {"surprise": 5, "react": 0.02}, "ma50"),
        ("pead", {"surprise": 5, "react": 0.02}, "none"),
        ("pead", {"surprise": 10, "react": 0.03, "trend": True}, "ma50"),
        ("pead", {"surprise": 3, "react": 0.0}, "ma50"),
        ("pead", {"surprise": 5, "react": 0.02, "exit_ma": "ma20"}, "ma50"),
        ("rs", {"rs": 0.9, "rsi": 30}, "ma50"),
        ("rs", {"rs": 0.9, "rsi": 30}, "none"),
        ("rs", {"rs": 0.8, "rsi": 20}, "ma50up"),
        ("rs", {"rs": 0.9, "rsi": 30, "exit_ma": "ma20"}, "ma50"),
        ("hi52", {"volx": 1.5}, "ma50"),
        ("hi52", {"volx": 2.0}, "ma50up"),
        ("hi52", {"volx": 1.5, "exit_ma": "ma20"}, "ma50"),
        ("flow", {"days": 4, "flowx": 0.3}, "ma50"),
        ("flow", {"days": 4, "flowx": 0.3}, "none"),
        ("flow", {"days": 5, "flowx": 0.5}, "ma50"),
        ("flow", {"days": 3, "flowx": 0.2, "exit_ma": "ma20"}, "ma50"),
    ]
    cache = {}
    for name, P0, rg in exps:
        if a.only and name != a.only:
            continue
        if name == "pead" and not data.get("earn"):
            continue
        if name == "flow" and not data.get("flow"):
            continue
        P = {**base_exit, **P0}
        key = (name, tuple(sorted((k, v) for k, v in P0.items() if k in ("gap", "volx", "closepos", "trend", "surprise", "react", "rs", "rsi", "days", "flowx", "maxrun", "stop_atr"))))
        if key not in cache:
            cache[key] = signals(name, S, P, data, mk)
        tr, cv = simulate(cache[key], S, mk, P, regime(idx, rg), dates)
        lbl = f"{name} {','.join(f'{k}={v}' for k, v in P0.items())} [{rg}]"
        print(report(lbl, tr, cv)[0], flush=True)


if __name__ == "__main__":
    main()
