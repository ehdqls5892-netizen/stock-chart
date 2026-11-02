"""실적 발표를 낀 스윙 매매를 어떻게 다룰지 백테스트 (미장).  python tools/earnbt.py swing-us.pkl.gz

실전과 같은 묶음: 지수 50일선 위 → 52주 신고가(hi52), 아래 → 저점 매수(dip, 짧게).
실적 반응일 = 발표가 정오 전이면 그날, 정오 뒤(장 마감 후)면 다음 거래일.

비교하는 방식
  A 무시        지금처럼 실적과 상관없이 사고, 그냥 들고 감
  B 안 삼       보유 예정 기간 안에 반응일이 있으면 안 삼 (이미 가진 건 들고 감)
  C 안 삼+전날   B + 가진 종목도 반응일 전날 종가에 팖
  D 전날 팖      사긴 사고, 반응일 전날 종가에 팖 (실적 전 기대감만 먹기)
  E 강하면 보유   최근 4분기 중 3번 이상 예상 상회 + 평균 서프라이즈 플러스 → 들고 감(크기는 실적 갭 기준),
                 아니면 전날 팖
  F 강하면 보유·아니면 안 삼
  G 전부 보유(크기만 실적 갭 기준)
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import pickle
import statistics as stt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import swingbt as B  # noqa: E402

EXIT = {"hi52": {"exit_ma": "ma10", "time_stop": 10, "time_need_R": 0.5, "trail_after_R": 2, "trail_atr": 3, "max_hold": 60, "window": 10},
        "dip": {"exit_ma": None, "time_stop": 5, "time_need_R": -99, "trail_after_R": 1.5, "trail_atr": 2, "max_hold": 7, "window": 7}}


def earn_events(data, S):
    """코드 → [(반응일 인덱스, 서프라이즈%, 발표일)] (서프라이즈는 발표 뒤에야 앎)."""
    out = {}
    for code, rows in (data.get("earn") or {}).items():
        s = S.get(code)
        if not s:
            continue
        ev = []
        for ts, est, rep, sp in rows:
            d, hh = ts[:10], int(ts[11:13] or 0)
            k = bisect.bisect_left(s["d"], d)          # 발표일(또는 그 다음 거래일)
            if k >= len(s["d"]):
                continue
            if s["d"][k] == d and hh >= 12:
                k += 1                                 # 장 마감 뒤 발표 → 다음 날 반응
            if k < len(s["d"]):
                ev.append((k, sp, d))
        ev.sort(key=lambda x: x[0])
        out[code] = ev
    return out


def history(ev, i):
    """i 일 시점에 이미 알려진 과거 실적: (최근 4번 중 상회 횟수, 평균 서프라이즈, 최근 8번 반응일 평균 |갭|)."""
    past = [e for e in ev if e[0] < i and e[1] is not None][-4:]
    beats = sum(1 for e in past if e[1] > 0)
    avg = stt.mean(e[1] for e in past) if past else 0.0
    return beats, avg, len(past)


def gap_size(s, ev, i):
    gs = [abs(s["o"][k] / s["c"][k - 1] - 1) for k, _, _ in ev if 0 < k < i][-8:]
    return (stt.mean(gs), max(gs)) if gs else (0.06, 0.12)


def next_react(ev, i):
    for k, sp, d in ev:
        if k >= i:
            return k
    return None


def simulate(sigs, S, ev_all, mode, P):
    cost = B.COST["us"]
    cash = 1.0
    pos, trades, curve, thru = {}, [], [], []
    by_day = defaultdict(list)
    for d, code, sc, stop, kind in sigs:
        by_day[d].append((sc, code, stop, kind))
    dates = P["dates"]
    pend = []
    for d in dates:
        for sc, code, stop, kind in pend:
            s = S[code]
            i = s["pos"].get(d)
            if i is None or code in pos or len(pos) >= P["maxpos"]:
                continue
            e = s["o"][i]
            ent = P.get("entry", "open")
            if ent != "open":                         # 장 마감 무렵에 사기 (그날 흐름을 보고)
                if s["l"][i] <= stop or (ent == "close_green" and s["c"][i] < s["o"][i]) \
                        or (ent == "close_hold" and s["c"][i] < s["c"][i - 1] * (1 - 0.0)):
                    continue
                e = s["c"][i]
            if stop >= e * 0.995:
                continue
            ev = ev_all.get(code, [])
            r = next_react(ev, i + 1)                  # 오늘 시가에 사면 오늘 반응(장 전 발표)은 이미 반영됨
            X = EXIT[kind]
            within = r is not None and r - i <= X["window"]
            beats, avg, n = history(ev, i)
            strong = n >= 3 and beats >= 3 and avg > 0
            plan = "hold"
            if within and mode != "A":
                if mode in ("B", "C"):
                    continue
                if mode == "D":
                    plan = "exit"
                elif mode == "E":
                    plan = "hold" if strong else "exit"
                elif mode == "F":
                    if not strong:
                        continue
                elif mode == "G":
                    plan = "hold"
                if plan == "exit" and r - i < 2:          # 하루도 못 들고 있으면 안 삼
                    continue
            per = e - stop
            sized = per
            if within and plan == "hold" and mode in ("E", "F", "G"):
                gavg, gmax = gap_size(s, ev, i)
                sized = max(per, e * gavg * 2)            # 실적 갭(평균의 2배)이 나도 1% 안에서
            mv = sum(p["q"] * S[k]["c"][S[k]["pos"].get(d, p["li"])] for k, p in pos.items())
            eq = cash + mv
            q = min(eq * P["risk"] / sized, eq * P["cap"] / e, cash / e)
            if q <= 0 or q * e < eq * 0.02:
                continue
            cash -= q * e * (1 + cost / 2)
            pos[code] = {"q": q, "e": e, "stop": stop, "R": per, "d0": d, "i0": i, "li": i, "hi": e, "kind": kind,
                         "plan": plan, "eq0": eq, "within": within, "strong": strong, "skip_day": d if ent != "open" else None}
        pend = []
        for code in list(pos):
            p = pos[code]
            s = S[code]
            i = s["pos"].get(d)
            if i is None:
                continue
            p["li"] = i
            if p.get("skip_day") == d:
                continue
            X = EXIT[p["kind"]]
            ev = ev_all.get(code, [])

            def close(x, why):
                nonlocal cash
                cash += p["q"] * x * (1 - cost / 2)
                trades.append((p["d0"], code, (x / p["e"] - 1) - cost, (x - p["e"]) / p["R"], why, i - p["i0"],
                               p["q"] * (x - p["e"]) / p["eq0"], p["kind"], p["within"], p["strong"]))
                del pos[code]

            if p.get("exit_next"):
                close(s["o"][i], p["exit_next"])
                continue
            if s["l"][i] <= p["stop"]:
                x = min(p["stop"], s["o"][i])
                if any(k == i for k, _, _ in ev):
                    thru.append(p["q"] * (x - p["e"]) / p["eq0"])
                close(x, "손절")
                continue
            if any(k == i for k, _, _ in ev):
                thru.append(p["q"] * (s["c"][i] - s["c"][i - 1]) / p["eq0"])
            c = s["c"][i]
            # 반응일 전날 종가에 팖
            r = next_react(ev, i + 1)
            sell_pre = p["plan"] == "exit" or mode == "C" or (mode == "D") or (mode == "E" and p["plan"] != "hold")
            if mode in ("E", "F") and p["plan"] == "hold" and r is not None and r == i + 1 and p["i0"] < i:
                # 들고 가기로 했던 종목도 새로 맞는 실적은 그때 다시 판단
                beats, avg, n = history(ev, i + 1)
                if not (n >= 3 and beats >= 3 and avg > 0):
                    sell_pre = True
            if sell_pre and r is not None and r == i + 1:
                close(c, "실적 전 정리")
                continue
            p["hi"] = max(p["hi"], c)
            held = i - p["i0"]
            a = s["atr"][i]
            if p["hi"] >= p["e"] + p["R"]:
                p["stop"] = max(p["stop"], p["e"] * (1 + cost))
            if p["hi"] >= p["e"] + X["trail_after_R"] * p["R"]:
                p["stop"] = max(p["stop"], p["hi"] - X["trail_atr"] * a)
            if X["exit_ma"] and held >= 2 and c < s[X["exit_ma"]][i] and c < p["e"] + 3 * p["R"]:
                p["exit_next"] = "10일선"
            elif held >= X["time_stop"] and c < p["e"] + X["time_need_R"] * p["R"]:
                p["exit_next"] = "기간"
            elif held >= X["max_hold"]:
                p["exit_next"] = "최대 보유"
        if by_day.get(d):
            pend = sorted(by_day[d], key=lambda x: -x[0])[: P["maxpos"]]
        mv = sum(p["q"] * S[k]["c"][p["li"]] for k, p in pos.items())
        curve.append((d, cash + mv, len(pos)))
    return trades, curve, thru


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--risk", type=float, default=0.01)
    a = ap.parse_args()
    data = pickle.load(gzip.open(a.data, "rb"))
    S = {c: B.indicators(B.prep(b)) for c, b in data["bars"].items() if len(b) >= 200}
    dates = sorted({d for s in S.values() for d in s["d"]})[200:]
    reg = B.regime(data["index"].get("SPY"), "ma50")
    ev = earn_events(data, S)
    sigs = []
    for kind, P0, want in (("hi52", {"volx": 1.5}, True), ("dip", {"rsi": 10, "drop": 0.10}, False)):
        for d, code, sc, stop in B.signals(kind, S, P0, data, "us"):
            if reg(d) == want:
                sigs.append((d, code, sc, stop, kind))
    print(f"종목 {len(S)} · 실적 자료 {len(ev)} · 기간 {dates[0]}~{dates[-1]} · 신호 {len(sigs)} · 위험 {a.risk * 100:g}%")
    P = {"dates": dates, "maxpos": 6, "risk": a.risk, "cap": 0.2}
    names = {"A": "A 무시(지금)", "B": "B 실적 있으면 안 삼", "C": "C 안 삼+보유도 전날 팖", "D": "D 사고 전날 팖",
             "E": "E 강하면 보유·아니면 전날 팖", "F": "F 강하면 보유·아니면 안 삼", "G": "G 전부 보유(갭 기준 크기)"}
    import os
    if os.environ.get("ENTRY"):
        for ent in ("open", "close", "close_green", "close_hold"):
            for kind in ("hi52", "dip", None):
                sg = [x for x in sigs if kind is None or x[4] == kind]
                tr, cv, _ = simulate(sg, S, ev, "G", {**P, "entry": ent})
                print(B.report(f"{ent} {kind or '합계'}", [t[:6] for t in tr], cv)[0], flush=True)
        return
    for m in "ABCDEFG":
        tr, cv, thru = simulate(sigs, S, ev, m, P)
        line, _ = B.report(names[m], [t[:6] for t in tr], cv)
        pre = [t for t in tr if t[4] == "실적 전 정리"]
        worst = min((t[6] for t in tr), default=0)
        extra = f" | 실적 넘긴 날 {len(thru)}번 평균 {stt.mean(thru) * 100 if thru else 0:+.2f}% 최악 {min(thru, default=0) * 100:+.2f}%" \
                f" | 실적 전 정리 {len(pre)}건 평균R {stt.mean(t[3] for t in pre) if pre else 0:+.2f} | 한 건 최악 {worst * 100:+.2f}%"
        print(line + extra, flush=True)
        if m in "AG":
            for kind in ("hi52", "dip"):
                for w in (False, True):
                    for st in ((None,) if not w else (True, False)):
                        g = [t for t in tr if t[7] == kind and t[8] == w and (st is None or t[9] == st)]
                        if g:
                            print(f"   {kind} 실적{'있음' if w else '없음'}{'' if st is None else ('·강함' if st else '·약함')}: {len(g)}건 "
                                  f"승률 {sum(t[2] > 0 for t in g) / len(g) * 100:.0f}% 평균R {stt.mean(t[3] for t in g):+.2f} "
                                  f"최악R {min(t[3] for t in g):+.2f} 계좌영향 합 {sum(t[6] for t in g) * 100:+.1f}%")


if __name__ == "__main__":
    main()
