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

SETUPS = {"kr": {"ep": {"gap": 0.04, "volx": 3}}, "us": {"hi52": {"volx": 1.5}}}
NAMES = {"ep": "큰 재료 갭(갭 상승+거래량 폭증+강한 마감)", "hi52": "52주 신고가 돌파+거래량"}


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
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "picks.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "picks"}, ensure_ascii=False), len(out["picks"]), "picks")


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
                          "reason": f"{NAMES[name]} · 거래량 {s['v'][i] / s['v20'][i - 1]:.1f}배 · 손절 {(stop / s['c'][i] - 1) * 100:.1f}%"})
    picks.sort(key=lambda x: -x["score"])
    reg = False                      # 지수를 못 받으면 '모름' → 새로 안 삼
    ix = None
    if idx:
        si = B.prep(idx)
        m50 = B.sma(si["c"], 50)
        reg = bool(si["c"][-1] > m50[-1])
        ix = {"close": round(float(si["c"][-1]), 2), "ma50": round(float(m50[-1]), 2), "date": si["d"][-1]}
    out = {"market": m.upper(), "date": last, "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
           "regime": reg, "index": ix, "universe": len(S), "picks": picks[:20]}
    return out


if __name__ == "__main__":
    main()
