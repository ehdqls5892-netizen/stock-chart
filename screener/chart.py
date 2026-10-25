"""차트 분석 엔진 — 캔들·이동평균·지지/저항·차트 패턴·거래량으로 신호를 계산한다.

규칙 출처: 초보자용 차트 교재에 흔히 나오는 기본 규칙(캔들 4대 패턴, 장악형, 도지, 정/역배열,
골든·데드크로스, 지지·저항과 역할 전환, 쌍바닥·쌍봉, 박스 돌파, 저점 높이기,
'반등 신호 vs 추격 금지 신호')을 기계적으로 옮긴 것이다. 같은 캔들도 '어디서' 나왔는지(자리)가
중요하므로 캔들 신호는 추세·지지/저항 위치와 함께 판정한다.

출력: docs/data/chart-<kr|us>-latest.json
  {asOf, market, generatedAt, stocks:{code:{name,market,close,pct,score,label,setups[],signals[],warns[],
   support,resistance,ma:{5,20,60,120},volX,buyPct,bars:[[YYMMDD,o,h,l,c,v,buy],...]}},
   scan:{rebound:[code..], breakout:[...], trend:[...], caution:[...]}}
이 결과는 매수·매도 추천이 아니라 차트 조건을 기계적으로 분류한 것이다.

실행: python screener/chart.py kr|us
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
BARS_OUT = 120

# ─────────────────────────── 데이터 ───────────────────────────

def load_universe(mk: str) -> dict[str, dict]:
    """code -> {name, market}"""
    uni: dict[str, dict] = {}
    f = ROOT / "config" / f"universe_{mk}.json"
    if f.exists():
        for x in json.loads(f.read_text(encoding="utf-8")):
            uni[x["code"]] = {"name": x["name"], "market": x.get("market", "")}
    w = ROOT / "config" / "watch.json"
    if w.exists():
        for x in json.loads(w.read_text(encoding="utf-8")).get(mk, []):
            uni.setdefault(x["code"], {"name": x.get("name", x["code"]), "market": x.get("market", "")})
    if mk == "kr":  # 전날 COF 스크리너 후보도 포함
        latest = OUT / "latest.json"
        if latest.exists():
            d = json.loads(latest.read_text(encoding="utf-8"))
            for r in d.get("danta", []) + d.get("swing", []) + d.get("universe", []):
                uni.setdefault(r["ticker"], {"name": r["name"], "market": r.get("market", "")})
    return uni


def download(mk: str, uni: dict[str, dict]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    def sym(code, market, alt=False):
        if mk == "us":
            return code.replace(".", "-")
        kq = market in ("코스닥", "KOSDAQ")
        if alt:
            kq = not kq
        return f"{code}.{'KQ' if kq else 'KS'}"

    out: dict[str, pd.DataFrame] = {}

    def fetch(pairs):
        syms = [s for _, s in pairs]
        if not syms:
            return []
        df = yf.download(syms, period="1y", interval="1d", group_by="ticker",
                         auto_adjust=False, progress=False, threads=True)
        miss = []
        for code, s in pairs:
            try:
                h = df[s] if len(syms) > 1 else df
                h = h.dropna(subset=["Close"])
                h = h[h["Volume"] > 0]
                if len(h) >= 40:
                    out[code] = h[["Open", "High", "Low", "Close", "Volume"]].copy()
                else:
                    miss.append(code)
            except Exception:
                miss.append(code)
        return miss

    pairs = [(c, sym(c, v["market"])) for c, v in uni.items()]
    for i in range(0, len(pairs), 80):
        miss = fetch(pairs[i:i + 80])
        if mk == "kr" and miss:  # 시장 구분이 틀렸을 수 있으니 반대 접미사로 재시도
            fetch([(c, sym(c, uni[c]["market"], alt=True)) for c in miss])
    return out

# ─────────────────────────── 지표 ───────────────────────────

def pivots(h: pd.Series, l: pd.Series, k: int = 3):
    """좌우 k봉보다 높은/낮은 지점 (인덱스 위치)"""
    hi, lo = [], []
    n = len(h)
    for i in range(k, n - k):
        if h.iloc[i] == h.iloc[i - k:i + k + 1].max() and not (hi and i - hi[-1] <= k):
            hi.append(i)
        if l.iloc[i] == l.iloc[i - k:i + k + 1].min() and not (lo and i - lo[-1] <= k):
            lo.append(i)
    return hi, lo


def zones(prices: list[float], tol: float = 0.02):
    """비슷한 가격(±tol)끼리 묶어 [가격, 터치수] 목록"""
    zs: list[list[float]] = []
    for p in sorted(prices):
        if zs and abs(p - zs[-1][0]) / zs[-1][0] <= tol:
            zs[-1][1] += 1
            zs[-1][0] = (zs[-1][0] * (zs[-1][1] - 1) + p) / zs[-1][1]
        else:
            zs.append([p, 1])
    return zs


def analyze(df: pd.DataFrame) -> dict:
    o, h, l, c, v = (df[k].astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    n = len(df)
    ma = {k: c.rolling(k).mean() for k in (5, 20, 60, 120)}
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    upw = h - np.maximum(o, c)
    low = np.minimum(o, c) - l
    avg_body = body.rolling(20).mean()
    vol20 = v.shift(1).rolling(20).mean()
    buy = v * ((c - l) / rng).fillna(0.5)  # X-ray: 봉 위치로 매수 몫 추정

    i = n - 1
    last = lambda s, k=0: float(s.iloc[i - k])
    pct = (last(c) / last(c, 1) - 1) * 100
    vol_x = last(v) / last(vol20) if last(vol20) > 0 else None
    disp20 = (last(c) / last(ma[20]) - 1) * 100 if not math.isnan(last(ma[20])) else None
    down5 = last(c) < last(c, 5)
    up_trend = last(c) > last(ma[20]) and last(ma[20]) > float(ma[20].iloc[i - 5])

    # 지지/저항: 최근 120봉 피벗 군집, 2회 이상 반응한 구간 우선
    look = df.iloc[-120:]
    ph, pl = pivots(look["High"].astype(float), look["Low"].astype(float))
    zs = zones([float(look["High"].iloc[j]) for j in ph] + [float(look["Low"].iloc[j]) for j in pl])
    px = last(c)
    sup = [z for z in zs if z[0] < px * 0.995]
    res = [z for z in zs if z[0] > px * 1.005]
    sup_strong = [z for z in sup if z[1] >= 2] or sup
    res_strong = [z for z in res if z[1] >= 2] or res
    support = max(sup_strong, key=lambda z: z[0])[0] if sup_strong else None
    resistance = min(res_strong, key=lambda z: z[0])[0] if res_strong else None
    near_sup = support is not None and (min(last(l), px) - support) / support <= 0.03
    near_res = resistance is not None and (resistance - px) / px <= 0.03

    sig, warn, setups = [], [], []
    score = 0

    def add(name, pts, setup=None):
        nonlocal score
        sig.append(name)
        score += pts
        if setup and setup not in setups:
            setups.append(setup)

    def caution(name, pts=2):
        nonlocal score
        warn.append(name)
        score -= pts

    b, r, ab = last(body), last(rng) if not math.isnan(last(rng)) else 0, last(avg_body)
    bull = last(c) > last(o)

    # ── 캔들 (자리와 함께) ──
    if bull and ab and b >= 1.6 * ab and r and b / r >= 0.6:
        add("장대양봉", 2 if (vol_x or 0) >= 1.5 else 1)
    if not bull and ab and b >= 1.6 * ab and r and b / r >= 0.6:
        caution("장대음봉", 2)
    if r and last(low) >= 2 * max(b, r * 0.05) and last(upw) <= max(b, r * 0.1):
        if down5 or near_sup:
            add("망치형(바닥)", 2 if near_sup else 1, "반등")
    if r and last(upw) >= 2 * max(b, r * 0.05) and last(low) <= max(b, r * 0.1):
        if down5 or near_sup:
            add("역망치형(바닥)", 1, "반등")
        elif up_trend or near_res:
            caution("유성형(고점)", 2)
    if r and last(upw) >= 2 * max(b, r * 0.05) and not bull and ab and b >= ab:
        caution("윗꼬리 음봉", 2)
    po, pc = last(o, 1), last(c, 1)
    if bull and pc < po and last(c) >= po and last(o) <= pc and b > abs(pc - po):
        add("상승장악형", 2 if (down5 or near_sup) else 1, "반등")
    if not bull and pc > po and last(o) >= pc and last(c) <= po and b > abs(pc - po):
        caution("하락장악형", 2)

    candle_names = ("장대양봉", "망치형(바닥)", "역망치형(바닥)", "상승장악형")
    if r and b <= 0.1 * r and not any(x in sig for x in candle_names) and not warn:
        add("도지(방향 고민)", 0)

    # ── 이동평균 ──
    m5, m20, m60, m120 = (last(ma[k]) for k in (5, 20, 60, 120))
    if not any(math.isnan(x) for x in (m5, m20, m60)):
        if m5 > m20 > m60 and (math.isnan(m120) or m60 > m120):
            add("정배열", 2, "추세")
        elif m5 < m20 < m60:
            caution("역배열", 1)
    for k in range(0, 3):
        a5, a20, b5, b20 = float(ma[5].iloc[i - k]), float(ma[20].iloc[i - k]), float(ma[5].iloc[i - k - 1]), float(ma[20].iloc[i - k - 1])
        if b5 <= b20 and a5 > a20:
            add("골든크로스(5·20)", 2, "추세")
            break
        if b5 >= b20 and a5 < a20:
            caution("데드크로스(5·20)", 2)
            break
    if not math.isnan(m20):
        crossed_up = any(float(c.iloc[i - k - 1]) < float(ma[20].iloc[i - k - 1]) and float(c.iloc[i - k]) > float(ma[20].iloc[i - k]) for k in range(3))
        crossed_dn = any(float(c.iloc[i - k - 1]) > float(ma[20].iloc[i - k - 1]) and float(c.iloc[i - k]) < float(ma[20].iloc[i - k]) for k in range(2))
        if crossed_up:
            add("20일선 회복", 2, "반등")
        if crossed_dn and last(c) < m20:
            caution("20일선 이탈", 2)
    if disp20 is not None and disp20 >= 20:
        caution(f"20일선 과열 이격 +{disp20:.0f}%", 2)

    # ── 지지/저항 자리 ──
    if near_sup and bull:
        add("지지선 반등", 2, "반등")
    if resistance is not None and near_res and not bull:
        caution("저항선 앞 밀림", 1)
    prev_high20 = float(h.iloc[-21:-1].max())
    prev_low20 = float(l.iloc[-21:-1].min())
    box = (prev_high20 - prev_low20) / prev_low20 if prev_low20 else 1
    if last(c) > prev_high20 and box <= 0.20:
        add("박스권 돌파", 3 if (vol_x or 0) >= 1.5 else 2, "돌파")
    elif box <= 0.15 and (prev_high20 - last(c)) / last(c) <= 0.03:
        add("박스 상단 근접", 1, "돌파 대기")
    if last(c) < prev_low20:
        caution("20일 저점 이탈", 2)

    # ── 차트 패턴 (피벗 기반) ──
    P_h = [j + (n - len(look)) for j in ph]
    P_l = [j + (n - len(look)) for j in pl]
    if len(P_l) >= 2:
        a, b2 = P_l[-2], P_l[-1]
        la, lb = float(l.iloc[a]), float(l.iloc[b2])
        if b2 - a >= 8 and abs(la - lb) / la <= 0.04 and b2 >= n - 25:
            neck = float(h.iloc[a:b2 + 1].max())
            if last(c) > neck:
                add("쌍바닥 넥라인 돌파", 3, "돌파")
            elif (neck - last(c)) / last(c) <= 0.05:
                add("쌍바닥 형성(넥라인 근접)", 2, "돌파 대기")
            else:
                add("쌍바닥 형성", 1, "반등")
    if len(P_l) >= 3:
        x1, x2, x3 = (float(l.iloc[j]) for j in P_l[-3:])
        if x1 < x2 < x3:
            add("저점 높이기", 1, "추세")
        if x2 < x1 and x2 < x3 and abs(x1 - x3) / x1 <= 0.06 and P_l[-1] >= n - 30:
            neck = float(h.iloc[P_l[-3]:P_l[-1] + 1].max())
            if last(c) > neck * 0.97:
                add("역헤드앤숄더", 2, "돌파 대기" if last(c) <= neck else "돌파")
    if len(P_h) >= 2:
        a, b2 = P_h[-2], P_h[-1]
        ha, hb = float(h.iloc[a]), float(h.iloc[b2])
        if b2 - a >= 8 and abs(ha - hb) / ha <= 0.03 and b2 >= n - 20:
            neck = float(l.iloc[a:b2 + 1].min())
            if last(c) < neck:
                caution("쌍봉 넥라인 이탈", 3)
            elif hb < ha:
                caution("쌍봉 의심", 1)
    if len(P_h) >= 3:
        y1, y2, y3 = (float(h.iloc[j]) for j in P_h[-3:])
        if y1 > y2 > y3:
            caution("고점 낮추기", 1)

    # ── 거래량 ──
    if vol_x is not None:
        if vol_x >= 2 and bull:
            add(f"거래량 {vol_x:.1f}배 동반 상승", 2)
        if vol_x >= 2 and not bull and last(o) < last(c, 1) * 0.98:
            caution("갭하락+거래량 급증", 3)
    run10 = (last(c) / float(c.iloc[i - 10]) - 1) * 100 if n > 11 else 0
    peak_v = float(v.iloc[-10:].max())
    if run10 >= 15 and float(v.iloc[-3:].mean()) < 0.5 * peak_v:
        caution("급등 후 거래량 감소", 2)
    if sum(1 for k in range(5) if float(c.iloc[i - k]) < float(c.iloc[i - k - 1])) == 5:
        caution("5일 연속 하락", 2)
    # 다이버전스: 20일 신고가인데 매수 거래량은 직전 고점 때보다 적음
    if last(h) >= float(h.iloc[-20:].max()) and len(P_h) >= 1:
        j = P_h[-1] if P_h[-1] < i - 2 else (P_h[-2] if len(P_h) > 1 else None)
        if j is not None and float(buy.iloc[i]) < 0.7 * float(buy.iloc[j]):
            caution("가격↑ 매수량↓ 다이버전스", 1)

    label = "관심" if score >= 4 and len(warn) == 0 else "주의" if len(warn) >= 2 or score <= -2 else "관망"
    rnd = (lambda x: round(x, 2)) if px < 1000 else (lambda x: int(round(x)))
    return {
        "close": rnd(px), "pct": round(pct, 2), "score": score, "label": label,
        "setups": setups, "signals": sig, "warns": warn,
        "support": rnd(support) if support else None, "resistance": rnd(resistance) if resistance else None,
        "ma": {str(k): (rnd(last(ma[k])) if not math.isnan(last(ma[k])) else None) for k in (5, 20, 60, 120)},
        "volX": round(vol_x, 1) if vol_x else None,
        "buyPct": round(float(buy.iloc[i]) / last(v) * 100) if last(v) else None,
        "disp20": round(disp20, 1) if disp20 is not None else None,
        "bars": [[d.strftime("%y%m%d"), rnd(float(r_.Open)), rnd(float(r_.High)), rnd(float(r_.Low)),
                  rnd(float(r_.Close)), int(r_.Volume), int(bv)]
                 for (d, r_), bv in zip(df.iloc[-BARS_OUT:].iterrows(), buy.iloc[-BARS_OUT:])],
    }


def main():
    mk = (sys.argv[1] if len(sys.argv) > 1 else "kr").lower()
    uni = load_universe(mk)
    data = download(mk, uni)
    print(f"{mk}: 유니버스 {len(uni)} · 시세 {len(data)}", file=sys.stderr)
    stocks = {}
    for code, df in data.items():
        try:
            a = analyze(df)
            a.update(name=uni[code]["name"], market=uni[code]["market"])
            stocks[code] = a
        except Exception as e:
            print("skip", code, e, file=sys.stderr)
    if not stocks:
        raise SystemExit("분석된 종목이 없습니다 (휴장이거나 시세 수신 실패)")

    def top(pred, key, k=15):
        return [c for c, _ in sorted(((c, s) for c, s in stocks.items() if pred(s)), key=key)[:k]]

    def cat(s):
        if len(s["warns"]) >= 2:
            return "caution"
        if len(s["warns"]) > 1:
            return None
        if "돌파" in s["setups"] or "돌파 대기" in s["setups"]:
            return "breakout"
        if "반등" in s["setups"]:
            return "rebound"
        if "추세" in s["setups"] and s["label"] == "관심":
            return "trend"
        return None

    scan = {k: [] for k in ("breakout", "rebound", "trend", "caution")}
    for c, s in sorted(stocks.items(), key=lambda x: -x[1]["score"]):
        k = cat(s)
        if k and k != "caution" and s["score"] > 0:
            scan[k].append(c)
    scan["caution"] = [c for c, s in sorted(stocks.items(), key=lambda x: x[1]["score"]) if cat(s) == "caution"]
    scan = {k: v[:15] for k, v in scan.items()}
    as_of = max(v["bars"][-1][0] for v in stocks.values())
    tz = timezone(timedelta(hours=9)) if mk == "kr" else timezone(timedelta(hours=-4))
    report = {"market": mk, "asOf": f"20{as_of[:2]}-{as_of[2:4]}-{as_of[4:]}",
              "generatedAt": datetime.now(tz).isoformat(timespec="minutes"),
              "note": "차트 조건을 기계적으로 분류한 결과이며 매수·매도 추천이 아닙니다.",
              "scan": scan, "stocks": stocks}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"chart-{mk}-latest.json").write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{mk}: 저장 {len(stocks)}종목 · " + " · ".join(f"{k} {len(v)}" for k, v in scan.items()), file=sys.stderr)


if __name__ == "__main__":
    main()
