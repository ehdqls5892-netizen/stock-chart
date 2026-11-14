"""로봇의 실제 점수 함수(bot/swing.py)로 3년 일봉 전 종목·전 날짜 점수를 미리 계산 (차트 부분만: 애널리스트·수급·AI 없음)."""
import gzip, os, pickle, sys, time
from multiprocessing import Pool
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bot import swing as SW

M = sys.argv[1]
import os
PRE = os.getenv("BT_PRE", "swing-"); OUT = os.getenv("BT_FEAT", "feat-")
D = pickle.load(gzip.open(os.getenv("LOOP_DATA", str(Path(__file__).resolve().parent / "data")) + f"/{PRE}{M}.pkl.gz"))
START = 205


def one(code):
    b = D["bars"][code]
    n = len(b)
    out = {}
    for i in range(START, n):
        r = b[max(0, i - 259):i + 1]
        c = r[-1][4]
        tv = sum(x[4] * x[5] for x in r[-20:]) / 20
        thin = c < (1000 if M == "kr" else 5) or tv < (3e9 if M == "kr" else 3e7)
        sc, why = SW.forward_score(M.upper(), r, c, {}, {}, 0)
        hi = max(x[2] for x in r[-252:])
        if c >= hi * 0.95:
            sc += 1
        cq = SW.chart_quality(r)[0]
        trs = [max(r[j][2] - r[j][3], abs(r[j][2] - r[j - 1][4]), abs(r[j][3] - r[j - 1][4])) for j in range(len(r) - 14, len(r))]
        ma20 = sum(x[4] for x in r[-20:]) / 20
        ma10 = sum(x[4] for x in r[-10:]) / 10
        vols = [x[5] for x in r[-21:-1] if x[5]]
        vx = r[-1][5] / (sum(vols) / len(vols)) if vols and r[-1][5] else 0
        ld = bool(SW.long_downtrend(r, c))
        out[b[i][0]] = (sc, cq, sum(trs) / 14, ma20, ma10, thin, ld, vx)
    return code, out


if __name__ == "__main__":
    t = time.time()
    codes = list(D["bars"])
    if len(sys.argv) > 2:
        codes = codes[:int(sys.argv[2])]
    with Pool(int(os.getenv("LOOP_PROCS", "2"))) as p:
        res = dict(p.imap_unordered(one, codes, chunksize=8))
    pickle.dump(res, open(os.getenv("LOOP_DATA", str(Path(__file__).resolve().parent / "data")) + f"/{OUT}{M}.pkl", "wb"))
    print(M, len(res), "종목", round(time.time() - t), "초")
