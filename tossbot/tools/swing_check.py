"""스윙 전략 검증: 같은 종목군·같은 청산 규칙으로 '아무 날 아무 종목' 산 것과 비교 (종목군 편향 걸러내기),
비용 2배·보유 수·위험 크기 바꿔도 버티는지.   python tools/swing_check.py swing-us.pkl.gz"""
import gzip
import pickle
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import swingbt as B  # noqa: E402

path = sys.argv[1]
only = sys.argv[2:] or ["hi52", "rs", "ep", "pead", "flow"]
data = pickle.load(gzip.open(path, "rb"))
mk = "kr" if "kr" in path else "us"
S = {c: B.indicators(B.prep(b)) for c, b in data["bars"].items() if len(b) >= 200}
idx = data["index"].get("KOSPI" if mk == "kr" else "SPY")
dates = sorted({d for s in S.values() for d in s["d"]})[200:]
base = {"exit_ma": "ma10", "time_stop": 10, "time_need_R": 0.5, "trail_after_R": 2, "trail_atr": 3, "max_hold": 60}
best = {
    "hi52": ({"volx": 1.5}, "ma50"),
    "rs": ({"rs": 0.9, "rsi": 30}, "ma50"),
    "ep": ({"gap": 0.04, "volx": 3, "exit_ma": "ma20"} if "us" in sys.argv[1] else {"gap": 0.04, "volx": 3}, "ma50"),
    "pead": ({"surprise": 10, "react": 0.03, "trend": True}, "ma50"),
    "flow": ({"days": 4, "flowx": 0.3}, "ma50"),
}


def rand_sigs(n_per_day, seed, trend=False):
    rnd = random.Random(seed)
    byd = {}
    for code, s in S.items():
        for i in range(60, len(s["c"]) - 1):
            if s["tv20"][i] >= (3e9 if mk == "kr" else 3e7) and not B.np.isnan(s["atr"][i]):
                if trend and not (s["c"][i] > s["ma50"][i]):
                    continue
                byd.setdefault(s["d"][i], []).append((code, i))
    out = []
    for d, xs in byd.items():
        for code, i in rnd.sample(xs, min(len(xs), n_per_day)):
            s = S[code]
            out.append((d, code, rnd.random(), s["c"][i] - 2.0 * s["atr"][i]))
    return out


for name in only:
    if name not in best or (name == "pead" and not data.get("earn")) or (name == "flow" and not data.get("flow")):
        continue
    P0, rg = best[name]
    P = {**base, **P0}
    sig = B.signals(name, S, P, data, mk)
    per_day = max(1, round(len(sig) / max(1, len({x[0] for x in sig}))))
    print(f"\n== {name} {P0} [{rg}] · 신호 {len(sig)}개 (신호 난 날 평균 {per_day}개)")
    tr, cv = B.simulate(sig, S, mk, P, B.regime(idx, rg), dates)
    print(B.report("전략", tr, cv)[0])
    for seed in (1, 2, 3):
        rs = rand_sigs(per_day, seed, trend=True)
        # 무작위도 신호 빈도를 맞춤: 전략이 신호 낸 날에만
        sd = {x[0] for x in sig}
        rs = [x for x in rs if x[0] in sd]
        tr2, cv2 = B.simulate(rs, S, mk, P, B.regime(idx, rg), dates)
        print(B.report(f"무작위(50일선 위) #{seed}", tr2, cv2)[0])
    old = dict(B.COST)
    B.COST[mk] = old[mk] * 2
    tr3, cv3 = B.simulate(sig, S, mk, P, B.regime(idx, rg), dates)
    print(B.report("비용 2배", tr3, cv3)[0])
    B.COST.update(old)
    for mp, rk in ((3, 0.01), (10, 0.0075), (6, 0.005)):
        tr4, cv4 = B.simulate(sig, S, mk, {**P, "maxpos": mp, "risk": rk}, B.regime(idx, rg), dates)
        print(B.report(f"최대 {mp}종목·위험 {rk * 100:g}%", tr4, cv4)[0])
