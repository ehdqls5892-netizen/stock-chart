"""로봇 fs(현재·미래 점수) 스윙 방식 백테스트 — 로봇의 실제 함수(SW.daily_check)와 같은 규칙으로 3년 일봉.
차트 점수만(애널리스트·수급·AI 없음) → 절대 수익보다 '규칙 A vs B 비교'용."""
import gzip, os, pickle, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bot import swing as SW  # noqa: E402

DIR = os.getenv("LOOP_DATA", str(Path(__file__).resolve().parent / "data")) + "/"
COST = {"kr": 0.0035, "us": 0.0015}
_C = {}
FAC = {}          # 이름 → (종목, 날짜) → 사도 되나 (trading_loop 가 과거 정보로 채움)


def load(m):
    if m not in _C:
        D = pickle.load(gzip.open(DIR + os.getenv("BT_PRE", "swing-") + f"{m}.pkl.gz"))
        F = pickle.load(open(DIR + os.getenv("BT_FEAT", "feat-") + f"{m}.pkl", "rb"))
        idx = D["index"]["KOSPI" if m == "kr" else "SPY"]
        dates = [b[0] for b in idx][205:]
        ic = {b[0]: b[4] for b in idx}
        il = [b[0] for b in idx]
        mk = {}
        for i, d in enumerate(il):
            if i >= 50:
                a50 = idx[i][4] > sum(x[4] for x in idx[i - 49:i + 1]) / 50
                a20 = idx[i][4] > sum(x[4] for x in idx[i - 19:i + 1]) / 20
                a10 = idx[i][4] > sum(x[4] for x in idx[i - 9:i + 1]) / 10
                mk[d] = (1 if a50 else -1, idx[i][4] / idx[i - 1][4] - 1, a20, a10, idx[i][4] / idx[i - 5][4] - 1)
        B = {c: {b[0]: b for b in bs} for c, bs in D["bars"].items()}
        BL = {c: ([b[0] for b in bs], bs) for c, bs in D["bars"].items()}
        _C[m] = (D, F, dates, mk, B, BL)
    return _C[m]


BASE = dict(min_fs=3, exit_fs=0, top=30, max_pos=6, risk=1.0, cap=20, open_risk=3.0, max_ext=0.15, max_stop=0.12, min_cq=-2,
            pt=4.0, pt_frac=0.34, be_R=1, trail_R=2, trail_atr=3, exit_ma=10, time_days=10, time_R=0.5, max_hold=60,
            early=8, early_up=7.0, rerank=True, lr="none", lr_pct=3.0, lr_k=1, guard=-2.0, entry="close", stop_atr=2.0,
            fs_floor=None, pt_be=True, regime=False, risk_hi=None, hi_k=99, max_new=99, plock=None, free=False, seed=None, drop=0.3, stop_pct=None, slip=0.0)


def run(m, P=None, d0=None, d1=None, detail=False):
    P = {**BASE, **(P or {})}
    D, F, dates, mk, B, BL = load(m)
    cost = COST[m]
    cfg = dict(be_after_R=P["be_R"], trail_after_R=P["trail_R"], trail_atr=P["trail_atr"], exit_ma=P["exit_ma"],
               time_stop_days=P["time_days"], time_need_R=P["time_R"], max_hold=P["max_hold"])
    eq, cash0 = 1.0, 1.0
    import random
    rng = random.Random(P["seed"]) if P["seed"] is not None else None
    pos = {}            # code -> dict
    trades, curve = [], []
    realized = 0.0
    use = [d for d in dates if (not d0 or d >= d0) and (not d1 or d <= d1)]
    prev = None

    def close_pos(code, px, why, d, frac=1.0):
        nonlocal realized
        p = pos[code]
        q = p["q"] * frac
        pnl = q * (px * (1 - cost / 2) - p["e"] * (1 + cost / 2))
        realized += pnl
        p["pnl"] += pnl
        p["q"] -= q
        if p["q"] <= 1e-12 or frac >= 1.0:
            trades.append((code, p["d"], d, p["pnl"] / p["risk0"], p["pnl"], why, p["held"], p["pnl"] / (p["q0"] * p["e"]), p.get("sc")))
            del pos[code]

    for d in use:
        mkt, iday, a20, a10, r5 = mk.get(d, (0, 0, True, True, 0))
        blocked = (P["regime"] is True and mkt < 0) or (P["regime"] == "both" and mkt < 0 and not a20) or (P["regime"] == "20" and not a20) \
            or (P["regime"] == "both10" and mkt < 0 and not a10) or (P["regime"] == "fall5" and mkt < 0 and r5 < 0)
        # 1) 장중: 손절·익절·손실 검토
        for code in list(pos):
            b = B[code].get(d)
            if not b:
                continue
            p = pos[code]
            _, o, h, l, c, v = b
            if o <= p["sl"]:
                close_pos(code, o, "stop_gap", d)
                continue
            lrp = p["e"] * (1 - P["lr_pct"] / 100)
            # 장중 -x% 닿음 (손절선보다 위일 때만 의미)
            if P["lr"] in ("hard", "half", "intra_fs") and not p.get("lr_done") and l <= lrp and lrp > p["sl"]:
                px = min(o, lrp)
                f_prev = F[code].get(prev)
                if P["lr"] == "hard":
                    close_pos(code, px, "lr_hard", d)
                    continue
                if P["lr"] == "half":
                    p["lr_done"] = True
                    close_pos(code, px, "lr_half", d, 0.5)
                elif P["lr"] == "intra_fs":
                    p["lr_done"] = True
                    weak = f_prev and (f_prev[0] <= P["lr_k"] or px < f_prev[3])
                    if weak:
                        close_pos(code, px, "lr_intra", d)
                        continue
                    p["lr_done"] = False       # 다음 날 다시 검토
            if code not in pos:
                continue
            if l <= p["sl"]:
                close_pos(code, p["sl"] * (1 - P["slip"]), "stop", d)
                continue
            if P["pt"] and not p["pt"] and h >= p["e"] * (1 + P["pt"] / 100):
                px = max(o, p["e"] * (1 + P["pt"] / 100))
                be = p["e"] * (1 + cost)
                p["pt"] = True
                if P["pt_frac"] > 0:
                    close_pos(code, px, "pt", d, P["pt_frac"])
                if code in pos and P["pt_be"]:
                    p["sl"] = max(p["sl"], be)
                    if c < be:                      # 익절 뒤 본전 아래로 밀려 마감 → 본전에서 나온 것으로 봄
                        close_pos(code, be, "be_stop", d)
            # 수익 잠금 사다리 (로봇 profit_lock 과 같음): 최고가가 +a% 넘으면 손절선을 +b% 로, trail_from 넘으면 최고가 -trail_pct%, take2 에서 일부 익절
            if code in pos and P["plock"]:
                pl = P["plock"]
                p["pk"] = max(p.get("pk", p["e"]), h)
                g = (p["pk"] / p["e"] - 1) * 100
                for a_, b_ in pl.get("steps", []):
                    if g >= a_:
                        p["sl"] = max(p["sl"], p["e"] * (1 + b_ / 100))
                if pl.get("trail_from") and g >= pl["trail_from"]:
                    p["sl"] = max(p["sl"], p["pk"] * (1 - pl["trail_pct"] / 100))
                if pl.get("take2_pct") and not p.get("t2") and g >= pl["take2_pct"]:
                    p["t2"] = True
                    close_pos(code, max(o, p["e"] * (1 + pl["take2_pct"] / 100)), "take2", d, pl.get("take2_frac", 0.5))
                if code in pos and c < p["sl"]:
                    close_pos(code, p["sl"] * (1 - P["slip"]), "lock", d)
        # 2) 마감 15분 전 점검
        for code in list(pos):
            bl, bs = BL[code]
            b = B[code].get(d)
            if not b:
                continue
            p = pos[code]
            i = bl.index(d) if p.get("i") is None else p["i"] + 1
            if bl[i] != d:
                i = bl.index(d)
            p["i"] = i
            bars = [(x[0], x[2], x[3], x[4]) for x in bs[max(0, i - 39):i + 1]]
            c = b[4]
            new, hi, why, _ = SW.daily_check(p["e"], p["sl"], p["sl0"], p["hi"], p["held"], bars, cfg, cost)
            p["hi"] = hi
            p["held"] += 1
            f = F[code].get(d)
            if not why and f and P["exit_fs"] is not None and f[0] <= P["exit_fs"] and p["held"] >= 2:
                why = "fs"
            if not why and f and P["lr"] == "close_fs" and c <= p["e"] * (1 - P["lr_pct"] / 100) and f[0] <= P["lr_k"]:
                why = "lr_close"
            if not why and f and P["lr"] == "close_weak" and c <= p["e"] * (1 - P["lr_pct"] / 100) and (c < f[3] or f[1] <= -1):
                why = "lr_weak"
            if not why and f and P["lr"] == "close_ma10" and c <= p["e"] * (1 - P["lr_pct"] / 100) and c < f[4]:
                why = "lr_ma10"
            if why:
                close_pos(code, c, why, d)
                continue
            p["sl"] = max(p["sl"], new)
        # 3) 매수 (전날 밤 후보 → 오늘 마감에 오늘 점수로 다시 점검·재정렬)
        equity = cash0 + realized + sum(p["q"] * ((B[c_].get(d) or (0, 0, 0, 0, p["last"]))[4] - p["e"]) for c_, p in pos.items())
        for c_, p in pos.items():
            bb = B[c_].get(d)
            if bb:
                p["last"] = bb[4]
        curve.append((d, equity))
        if prev and len(pos) < P["max_pos"] and not (P["guard"] is not None and iday * 100 <= P["guard"]) and not blocked:
            night = []
            for code, fd in F.items():
                f0 = fd.get(prev)
                if not f0 or f0[5] or f0[6] or code in pos:
                    continue
                night.append((f0[0], f0[7], code))
            night.sort(key=lambda t: (-t[0], -t[1]))
            cands = []
            for sc0, _, code in night[:P["top"]]:
                if rng and rng.random() < P["drop"]:
                    continue
                b, f1, f0 = B[code].get(d), F[code].get(d), F[code][prev]
                pb = B[code].get(prev)
                if not b or not f1 or not pb:
                    continue
                _, o, h, l, c, v = b
                stop = pb[4] - P["stop_atr"] * f0[2]
                if P["stop_pct"]:
                    stop = (o * 1.002 if (P["early"] is not None and sc0 >= P["early"]) else c) * (1 - P["stop_pct"] / 100)
                early = P["early"] is not None and sc0 >= P["early"] and o / pb[4] - 1 <= P["early_up"] / 100 and o > stop
                if P["entry"] == "open":
                    early = o > stop
                if early:
                    e = o * 1.002
                    if o / pb[4] - 1 > 0.10 or (e - stop) / e > P["max_stop"] or e / f0[3] - 1 > P["max_ext"]:
                        continue
                    cands.append((-sc0, code, e, stop, True))
                    continue
                if P["entry"] == "open":
                    continue
                e = c
                if l <= stop or e <= stop or o / pb[4] - 1 > 0.10:
                    continue
                if f1[0] < P["min_fs"] or f1[1] < P["min_cq"] or e / f1[3] - 1 > P["max_ext"] or (e - stop) / e > P["max_stop"]:
                    continue
                if P["fs_floor"] is not None and f1[0] < P["fs_floor"]:
                    continue
                if P.get("fac") and P["fac"] in FAC and not FAC[P["fac"]](code, prev):      # 애널리스트·내부자 등 그때 알 수 있던 정보로 거르기
                    continue
                cands.append((-(f1[0] if P["rerank"] else sc0), code, e, stop, False))
            cands.sort(key=lambda t: (not t[4], t[0]))
            newn = 0
            for _, code, e, stop, early in cands:
                if len(pos) >= P["max_pos"] or newn >= P["max_new"]:
                    break
                open_risk = sum(max(0.0, (p["last"] - p["sl"]) * p["q"]) for p in pos.values())
                per = e - stop
                if P["free"]:
                    q = 0.01 / e                       # 종목당 자본의 1% 같은 금액 → pnl/0.01 = 그 거래 수익률
                    pos[code] = dict(e=e, sl=stop, sl0=stop, hi=e, held=0, q=q, d=d, pt=False, pnl=0.0, risk0=q * per, last=B[code][d][4], i=None, q0=q, sc=-_)
                    continue
                rk = P["risk_hi"] if P["risk_hi"] and -_ >= P["hi_k"] else P["risk"]
                q = min(equity * rk / 100 / per, equity * P["cap"] / 100 / e)
                if open_risk + q * per > equity * P["open_risk"] / 100 * 1.0001:
                    q = max(0.0, (equity * P["open_risk"] / 100 - open_risk) / per)
                    if q * per < equity * P["risk"] / 100 * 0.5:
                        continue
                used = sum(p["q"] * p["e"] for p in pos.values())
                if used + q * e > equity:
                    q = max(0.0, (equity - used) / e)
                    if q * per < equity * P["risk"] / 100 * 0.5:
                        continue
                pos[code] = dict(e=e, sl=stop, sl0=stop, hi=e, held=0, q=q, d=d, pt=False, pnl=0.0, risk0=q * per, last=B[code][d][4], i=None, q0=q, sc=-_)
                newn += 1
        prev = d
    for code in list(pos):
        close_pos(code, pos[code]["last"], "end", use[-1])
    eqs = [e for _, e in curve]
    peak, mdd = eqs[0], 0.0
    for e in eqs:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    tot = eqs[-1] / eqs[0] - 1 if eqs else 0
    yrs = len(eqs) / 250
    wins = [t for t in trades if t[4] > 0]
    gp, gl = sum(t[4] for t in wins), -sum(t[4] for t in trades if t[4] <= 0)
    res = dict(ret=tot * 100, cagr=((1 + tot) ** (1 / yrs) - 1) * 100 if yrs > 0 and tot > -1 else 0, mdd=mdd * 100, n=len(trades),
               win=len(wins) / len(trades) * 100 if trades else 0, pf=gp / gl if gl else 0,
               avgR=sum(t[3] for t in trades) / len(trades) if trades else 0, hold=sum(t[6] for t in trades) / len(trades) if trades else 0)
    if detail:
        res["trades"], res["curve"] = trades, curve
    return res


def show(name, r):
    cal = r["cagr"] / r["mdd"] if r["mdd"] else 0
    print(f"{name:34s} 수익 {r['ret']:+7.1f}% 연 {r['cagr']:+6.1f}% 낙폭 {r['mdd']:5.1f}% 칼마 {cal:5.2f} 거래 {r['n']:4d} 승률 {r['win']:4.1f}% PF {r['pf']:4.2f} 평균R {r['avgR']:+.2f} 보유 {r['hold']:.1f}일")


if __name__ == "__main__":
    for m in ("kr", "us"):
        print("==", m)
        show("기본(현재 설정)", run(m))


def free(m, P=None, d0=None, d1=None):
    """한도 없이 모든 신호를 같은 금액으로 → 거래당 평균 수익률(비용 뒤)·t값·전후반."""
    r = run(m, {**(P or {}), "free": True, "max_pos": 9999}, d0, d1, detail=True)
    t = r["trades"]
    if not t:
        return None
    xs = [x[7] * 100 for x in t]
    n = len(xs)
    mu = sum(xs) / n
    sd = (sum((x - mu) ** 2 for x in xs) / max(1, n - 1)) ** 0.5
    mid = sorted(x[1] for x in t)[n // 2]
    a = [x[7] * 100 for x in t if x[1] < mid]
    b = [x[7] * 100 for x in t if x[1] >= mid]
    hold = sum(x[6] for x in t) / n
    return dict(n=n, mu=mu, t=mu / (sd / n ** 0.5) if sd else 0, win=sum(1 for x in xs if x > 0) / n * 100, a=sum(a) / len(a), b=sum(b) / len(b),
                hold=hold, perday=mu / max(hold, 1), R=sum(x[3] for x in t) / n)


def showf(name, r):
    print(f"{name:34s} 거래 {r['n']:5d} 평균 {r['mu']:+.2f}% (t {r['t']:+.1f}) 승률 {r['win']:4.1f}% 전반 {r['a']:+.2f}% 후반 {r['b']:+.2f}% 보유 {r['hold']:4.1f}일 평균R {r['R']:+.3f}")


def port(m, P=None, seeds=12):
    """한도 있는 실제 계좌 방식 — 후보를 무작위로 30% 빼고 여러 번 돌려 평균 (운에 덜 좌우되게)."""
    rs = [run(m, {**(P or {}), "seed": s}) for s in range(seeds)]
    k = lambda f: sum(r[f] for r in rs) / len(rs)
    return dict(cagr=k("cagr"), mdd=k("mdd"), n=k("n"), win=k("win"), pf=k("pf"), lo=min(r["cagr"] for r in rs), hi=max(r["cagr"] for r in rs),
                pos=sum(1 for r in rs if r["cagr"] > 0))


def showp(name, r, seeds=12):
    print(f"{name:36s} 연 {r['cagr']:+6.1f}% (최저 {r['lo']:+6.1f} ~ 최고 {r['hi']:+6.1f}, {r['pos']}/{seeds} 플러스) 낙폭 {r['mdd']:5.1f}% 거래 {r['n']:4.0f} 승률 {r['win']:4.1f}% PF {r['pf']:.2f}")
