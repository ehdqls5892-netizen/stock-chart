"""스윙 신호 종목 만들기 (장 끝난 뒤 GitHub Actions 에서).  python tools/swing_picks.py kr|us --out out
→ out/picks.json  {market, date(신호 난 날), regime(시장 국면 좋음?), index, picks:[{code,name,setup,close,stop,atr,score,reason}]}
백테스트(tools/swingbt.py)와 같은 함수로 신호를 판단한다."""
from __future__ import annotations

import argparse
import io
import os
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import swing_data as SD  # noqa: E402
import swingbt as B  # noqa: E402
from bot import swing as SW  # noqa: E402  (현재·미래 점수 — 로봇과 같은 함수)

SETUPS = {"kr": {"ep": {"gap": 0.04, "volx": 3}}, "us": {"hi52": {"volx": 1.5}, "dip": {"rsi": 10, "drop": 0.10}}}
MIN_RR = float(json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))["daily_swing"].get("min_rr", 1.5))
NAMES = {"ep": "큰 재료 갭(갭 상승+거래량 폭증+강한 마감)", "hi52": "52주 신고가 돌파+거래량",
         "dip": "하락장 저점 매수(200일선 위 종목·3일 RSI 10↓·20일 고점 -10%↓)"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["kr", "us"])
    ap.add_argument("--out", default="out")
    ap.add_argument("--live", action="store_true", help="장중 다시 고르기: 오늘 흐름(진행 중 봉)으로 후보만 다시 → live.json")
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
    # 데스크(실적 일정·가격 계획·이슈)에 나오는데 평소 분석 범위 밖인 종목도 같이 받아 판정 (앱에 '정보 적음' 안 뜨게)
    try:
        extra = [c for c in desk_codes(m) if c not in bars][:80]
        if extra:
            if m == "kr":
                got = SD.yf_bars({c: c + ".KS" for c in extra}, 2)
                miss = [c for c in extra if c not in got]
                got.update(SD.yf_bars({c: c + ".KQ" for c in miss}, 2) if miss else {})
            else:
                got = SD.yf_bars({c: c.replace(".", "-") for c in extra}, 2)
            for c, b in got.items():
                bars[c] = b
                names.setdefault(c, c)
            EXTRA.update(got.keys())
            print(f"데스크 종목 추가 분석: {len(got)}/{len(extra)}", file=sys.stderr)
    except Exception as e:
        print("데스크 종목 추가 실패:", e, file=sys.stderr)
    out = build(m, bars, names, idx)
    if a.live:
        return live_main(a, m, bars, names, idx, out, (lambda c: yfs.get(c, c)) if m == "kr" else None)
    try:
        enrich_us(out["picks"], bars, (lambda c: yfs.get(c, c)) if m == "kr" else None)
    except Exception as e:
        print("실적·애널리스트·업종 정보 실패:", e, file=sys.stderr)
    try:
        fs = scan_now(m, bars, names, idx, (lambda c: yfs.get(c, c)) if m == "kr" else None)
        out["picks"] = fs + out["picks"]
        out["fs_count"] = len(fs)
    except Exception as e:
        print("현재·미래 점수 스캔 실패:", e, file=sys.stderr)
    try:
        out["etf"] = etf_metrics(m)
    except Exception as e:
        print("ETF 상태 계산 실패:", e, file=sys.stderr)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    try:
        add_targets(m, [c for c in EXTRA if c in STATS])
    except Exception as e:
        print("추가 종목 목표가 실패:", e, file=sys.stderr)
    try:                                                   # 휴대폰 앱 데스크 판정용 전 종목 상태
        (Path(a.out) / "scores.json").write_text(json.dumps(STATS, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except Exception as e:
        print("상태 목록 저장 실패:", e, file=sys.stderr)
    try:                                                   # 휴대폰 앱 종목 검색용 이름 목록
        (Path(a.out) / "names.json").write_text(json.dumps({str(k): str(v) for k, v in names.items()}, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("이름 목록 저장 실패:", e, file=sys.stderr)
    (Path(a.out) / "picks.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "picks"}, ensure_ascii=False), len(out["picks"]), "picks")


REUSE: dict = {}       # 장중 다시 고르기: 밤에 이미 AI 가 읽은 종목은 그 판독을 다시 씀 (비용 절약)


def add_targets(m, codes):
    """범위 밖 종목의 증권사 평균 목표가·의견 (미장 야후, 국장 네이버) → STATS[code]['tgt'], ['rec']."""
    import requests
    if m == "us":
        import yfinance as yf
        for c in codes[:60]:
            try:
                info = yf.Ticker(c.replace(".", "-")).info or {}
                STATS[c]["tgt"] = info.get("targetMeanPrice")
                STATS[c]["rec"] = info.get("recommendationMean")
                if info.get("shortName"):
                    STATS[c]["name"] = info["shortName"]
            except Exception:
                pass
    else:
        import estimates_snapshot as ES
        ses = requests.Session()
        ses.headers["User-Agent"] = "Mozilla/5.0 (stock-chart bot)"
        for c in codes[:60]:
            try:
                row = ES.kr_api(c, ses)
                STATS[c]["tgt"] = SW._num(row.get("cs_priceTargetMean"))
                STATS[c]["rec"] = SW._num(row.get("cs_recommMean"))
            except Exception:
                pass


def live_main(a, m, bars, names, idx, base, tick):
    """장중(마감 약 2시간 전) 다시 고르기. 야후 일봉의 마지막 줄 = 오늘 진행 중인 봉 → 오늘 흐름이 반영된 점수.
    밤 후보와 비교해 새로 들어온 종목·빠진 종목을 적어 live.json 으로. 로봇은 매수 직전에 이걸 우선 씀."""
    import requests
    prev = {}
    try:
        prev = requests.get(f"https://raw.githubusercontent.com/ehdqls5892-netizen/stock-chart/swingpicks-{m}/picks.json", timeout=30).json()
    except Exception as e:
        print("밤 후보를 못 받음:", e, file=sys.stderr)
    for x in prev.get("picks", []):
        ai = (x.get("ev") or {}).get("ai")
        if x.get("setup") == "fs" and ai:
            REUSE[x["code"]] = ai
    fs = scan_now(m, bars, names, idx, tick)
    old_codes = [x["code"] for x in prev.get("picks", []) if x.get("setup") == "fs"][:20]
    new_codes = [x["code"] for x in fs]
    ix = base.get("index") or {}
    try:
        c0, c1 = (float(idx[-2][4]) if len(idx) > 1 else None), float(idx[-1][4])
        ix["today_pct"] = round((c1 / c0 - 1) * 100, 2) if c0 else None
    except Exception:
        pass
    out = {"market": m.upper(), "date": base.get("date"), "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
           "live": True, "base_date": prev.get("date"), "regime": base.get("regime"), "index": ix, "picks": fs,
           "added": [c for c in new_codes if c not in old_codes], "dropped": [c for c in old_codes if c not in new_codes]}
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "live.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("장중 다시 고르기:", len(fs), "개 · 새로", len(out["added"]), "· 빠짐", len(out["dropped"]), "· 지수 오늘", ix.get("today_pct"))


def react_day(ts: str) -> str:
    """발표 시각(뉴욕) → 주가 반응일: 정오 전 발표면 그날, 뒤면 다음 평일."""
    d = dt.date.fromisoformat(ts[:10])
    if int(ts[11:13] or 0) >= 12:
        d += dt.timedelta(days=1)
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
    return d.isoformat()


def enrich_us(picks, bars, tick=None):
    """후보마다 다음 실적일·과거 실적 갭 크기·애널리스트 전망치 변화·목표가·업종을 붙임 (ev).
    국장도 같은 함수(tick: 코드 → 야후 티커 005930.KS). 국장은 야후에 실적일·전망치가 비어 있는 경우가 많아 있는 것만 씀."""
    import yfinance as yf
    today = dt.date.today().isoformat()
    for p in picks:
        ev = {}
        t = yf.Ticker(tick(p["code"]) if tick else p["code"].replace(".", "-"))
        try:
            df = t.get_earnings_dates(limit=12)
            rows = sorted((ts.strftime("%Y-%m-%d %H:%M"), r.get("Reported EPS")) for ts, r in (df if df is not None else []).iterrows())
            try:                                       # 직전 실적: 예상 대비 서프라이즈 %, 1년 전 같은 분기 대비 EPS
                past = sorted(((ts.strftime("%Y-%m-%d"), r.get("Reported EPS"), r.get("Surprise(%)"))
                               for ts, r in df.iterrows() if r.get("Reported EPS") == r.get("Reported EPS")
                               and r.get("Reported EPS") is not None), reverse=True)
                if past and past[0][0] < (dt.date.today() - dt.timedelta(days=200)).isoformat():
                    past = []                          # 야후 실적 기록이 반년 넘게 멈춘 종목(국장에 흔함, 예: 2021년) → 안 씀
                if past:
                    ev["last_earn"] = past[0][0]
                    if past[0][2] == past[0][2] and past[0][2] is not None:
                        ev["last_surprise"] = round(float(past[0][2]), 2)
                    if len(past) >= 5 and past[4][1]:
                        ev["eps_yoy"] = round(float(past[0][1]) / abs(float(past[4][1])) - 1, 4) if float(past[4][1]) else None
            except Exception:
                pass
            fut = [ts for ts, rep in rows if ts[:10] >= today and rep != rep]
            if not fut and tick:                       # 국장: 달력(calendar)에 다음 실적일이 있는 경우
                try:
                    cal = t.calendar or {}
                    eds = cal.get("Earnings Date") or []
                    fut = [f"{x.isoformat()} 16:00" for x in eds if x.isoformat() >= today][:1]
                except Exception:
                    pass
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
            if info.get("recommendationMean") and not tick:   # 국장은 네이버 의견(5점=매수)을 씀 — 야후(1점=매수)와 섞이면 헷갈림
                ev["rec"] = round(float(info["recommendationMean"]), 2)
            ev["n_analysts"] = info.get("numberOfAnalystOpinions")
            ev["fwd_eps"], ev["ttm_eps"] = info.get("forwardEps"), info.get("trailingEps")
            ev["fpe"] = info.get("forwardPE")
            ev["short_pct"] = info.get("shortPercentOfFloat")
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


def est_latest(m):
    """estimates 브랜치의 가장 최근 컨센서스 CSV (오늘~5일 전)."""
    return est_old(m, days=0, back=6)


def desk_codes(m):
    """주식비서 데스크가 좋게 본 종목: 마감 리포트 가격 계획·관심, 장중 1순위·호재 이슈, 실적 좋음·실적 기대."""
    import requests
    base = "https://raw.githubusercontent.com/ehdqls5892-netizen/stock-chart/main/docs/data/desk-{m}-{k}.json"
    out = []
    for k in ("report", "live", "earn"):
        try:
            d = requests.get(base.format(m=m, k=k), timeout=20).json()
        except Exception:
            continue
        out += list((d.get("plans") or {}))
        if k == "report":
            out += (d.get("watch") or [])[:20]
        if k == "live":
            out += [c for c, x in (d.get("impact") or {}).items() if (x or {}).get("impact") == "호재"]
        if k == "earn":
            out += list((d.get("good") or {}))
            out += list((d.get("upcoming") or {}))
            out += [c for c, x in (d.get("events") or {}).items() if "기대" in str((x or {}).get("bias", ""))]
    return list(dict.fromkeys(str(c) for c in out))


def est_old(m, days=28, back=None):
    """estimates 브랜치에서 약 days 일 전(없으면 가장 오래된 것) 컨센서스 CSV → 코드별 행."""
    import csv
    import requests
    base = "https://raw.githubusercontent.com/ehdqls5892-netizen/stock-chart/estimates/{m}/{d}.csv"
    today = dt.date.today()
    ks = list(range(days, days + back)) if back else list(range(days, days + 7)) + list(range(days - 1, 5, -1))
    for k in ks:
        d = (today - dt.timedelta(days=k)).isoformat()
        try:
            r = requests.get(base.format(m=m, d=d), timeout=20)
        except Exception:
            continue
        if r.status_code == 200 and r.text.strip():
            rows = {}
            for row in csv.DictReader(io.StringIO(r.text)):
                c = str(row.get("code") or "")
                rows[c.zfill(6) if m == "kr" and c.isdigit() else c] = row
            print(f"컨센서스 비교 기준일 {d} ({len(rows)}종목)", file=sys.stderr)
            return rows
    return {}


def _rows(b, n=30):
    """swing_data 일봉 [(날짜, 시가, 고가, 저가, 종가, 거래량)] 그대로 최근 n개 (날짜는 date)."""
    out = []
    for x in b[-n:]:
        d = x[0] if isinstance(x[0], dt.date) else dt.date.fromisoformat(str(x[0])[:10])
        out.append((d, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5] or 0)))
    return out


THIN: set = set()      # 거래가 너무 적어 매수 후보로는 안 쓰는 종목
EXTRA: set = set()     # 데스크 때문에 추가로 받은 종목 (목표가·의견도 따로 붙임)
STATS: dict = {}       # 전 종목 지금 상태 (앱 데스크 판정용): 차트 점수·장기 하락·1년 고점 대비·3개월·20일선 거리


def scan_now(m, bars, names, idx, tick=None, top=50, keep=20):
    """현재·미래 점수 스캔: 전체 종목을 '지금' 기준으로 점수 매겨 상위 종목을 후보(setup 'fs')로.
    1) 차트(최근 봉 양봉·마감 위치·거래량 실린 상승·20일선·52주 신고가 근처·시장 추세)로 전 종목 1차 점수
    2) 상위 top 개만 애널리스트·이익 성장·실적일·업종(미장 야후) / 증권사 의견·외국인·기관·이익 성장(국장 네이버) 붙여 최종 점수
    3) 최종 점수 높은 순 keep 개. 뉴스는 로봇이 사기 직전에 최신으로 더함."""
    mkt = 0
    if idx:
        si = B.prep(idx)
        m50 = B.sma(si["c"], 50)
        mkt = 1 if si["c"][-1] > m50[-1] else -1
    try:
        est_all = est_latest(m)
    except Exception:
        est_all = {}
    first = []
    for code, b in bars.items():
        if len(b) < 60:
            continue
        r = _rows(b, 260)
        c = r[-1][4]
        tv = sum(x[4] * x[5] for x in r[-20:]) / 20
        thin = c < (1000 if m == "kr" else 5) or tv < (3e9 if m == "kr" else 3e7)
        if thin and code not in EXTRA:
            continue
        if thin:
            THIN.add(code)                                   # 데스크 종목: 판정용 상태만 계산, 매수 후보로는 안 씀
        sc, why = SW.forward_score(m.upper(), r, c, {}, {}, 0)
        hi = max(x[2] for x in r[-252:])
        if c >= hi * 0.95:
            sc += 1
            why.append("＋52주 신고가 근처")
        if mkt:
            sc += mkt
            why.append("＋시장 상승 추세(지수 50일선 위)" if mkt > 0 else "－시장 하락 추세(지수 50일선 아래)")
        sc_chart = sc
        # 1차부터 '앞으로 오를 여지'도 봄: 증권사 평균 목표가까지 남은 폭 ÷ 손절(2×ATR) 폭, 의견
        e0 = est_all.get(code) or {}
        tg = SW._num(e0.get("targetMeanPrice") if m == "us" else e0.get("cs_priceTargetMean"))
        if tg and c and not SW.long_downtrend(r, c):
            trs0 = [max(r[i][2] - r[i][3], abs(r[i][2] - r[i - 1][4]), abs(r[i][3] - r[i - 1][4])) for i in range(len(r) - 14, len(r))]
            adj0, t0, _ = SW.rr_score(tg / c - 1, 2 * sum(trs0) / len(trs0) / c)
            if t0:
                sc += adj0
                why.append(t0)
        rec = SW._num(e0.get("recommendationMean") if m == "us" else e0.get("cs_recommMean"))
        if rec is not None and ((m == "us" and rec <= 2.0) or (m == "kr" and rec >= 4.0)):
            sc += 1
            why.append("＋증권사 의견 매수 우세")
        vols = [x[5] for x in r[-21:-1] if x[5]]
        vx = r[-1][5] / (sum(vols) / len(vols)) if vols and r[-1][5] else 0
        first.append((sc, vx, code, r, why))
        try:
            hi1 = max(x[2] for x in r[-252:])
            ma20 = sum(x[4] for x in r[-20:]) / 20
            STATS[code] = {"c": sc_chart - mkt, "ld": bool(SW.long_downtrend(r, c)), "dd": round(c / hi1 - 1, 3),
                           "r3": round(c / r[-64][4] - 1, 3) if len(r) >= 64 else None, "d20": round(c / ma20 - 1, 3),
                           "close": round(c, 4)}
        except Exception:
            pass
    first.sort(key=lambda t: (-t[0], -t[1]))
    first = [t for t in first if t[2] not in THIN]
    try:
        dk = set(desk_codes(m))
    except Exception:
        dk = set()
    pick = first[:top] + [t for t in first[top:] if t[2] in dk][:20]      # 데스크가 좋게 본 종목도 깊게 점검
    print(f"깊게 점검: 점수 상위 {min(top, len(first))} + 데스크 종목 {len(pick) - min(top, len(first))}", file=sys.stderr)
    cand = []
    for sc, vx, code, r, why in pick:
        c = r[-1][4]
        trs = [max(r[i][2] - r[i][3], abs(r[i][2] - r[i - 1][4]), abs(r[i][3] - r[i - 1][4])) for i in range(len(r) - 14, len(r))]
        atr = sum(trs) / len(trs)
        cand.append({"code": code, "name": names.get(code, code), "setup": "fs", "close": round(c, 4),
                     "stop": round(c - 2 * atr, 4), "atr": round(atr, 4), "score": sc, "_chart": why, "_rows": r})
    if m == "us":
        try:
            enrich_us(cand, bars, None)
        except Exception as e:
            print("미장 애널리스트 정보 실패:", e, file=sys.stderr)
    else:
        try:
            enrich_us(cand, bars, tick)                       # 업종·실적일(있으면)
        except Exception as e:
            print("국장 업종 실패:", e, file=sys.stderr)
        import requests
        import estimates_snapshot as ES
        ses = requests.Session()
        ses.headers["User-Agent"] = "Mozilla/5.0 (stock-chart bot)"
        for x in cand:
            try:
                row = ES.kr_api(x["code"], ses)
                x.setdefault("ev", {})["est"] = {k: v for k, v in row.items() if v is not None and k != "code"}
            except Exception as e:
                print("국장 컨센서스 실패", x["code"], e, file=sys.stderr)
    # 컨센서스 변화: 우리가 매일 모은 기록(estimates 브랜치)에서 약 4주 전과 비교
    old = est_old(m)
    import extra_sources as XS
    for x in cand:
        ev = x.setdefault("ev", {})
        o = old.get(x["code"]) or {}
        e = ev.get("est") or {}
        if m == "us":
            t0, t1 = SW._num(o.get("targetMeanPrice")), (x["close"] * (1 + ev["tgt_up"]) if ev.get("tgt_up") is not None else None)
        else:
            t0, t1 = SW._num(o.get("cs_priceTargetMean")), SW._num(e.get("cs_priceTargetMean"))
            p0, p1 = SW._num(o.get("ti_cnsEps")), SW._num(e.get("ti_cnsEps"))
            if p0 and p1:
                ev["eps_chg"] = round(p1 / abs(p0) - 1, 4)
        if t0 and t1:
            ev["tgt_chg"] = round(t1 / t0 - 1, 4)
        if m == "kr":
            ev.update(XS.dart(x["code"]))
            nn = XS.naver_news(x["name"]) or XS.naver_stock_news(x["code"])
            if nn:
                ev["news"] = nn
    out = []
    for x in cand:
        ev = x.get("ev") or {}
        if m == "us":
            est = {"forwardEps": ev.get("fwd_eps"), "trailingEps": ev.get("ttm_eps")}
        else:
            est = ev.get("est") or {}
        sc, why = SW.forward_score(m.upper(), x["_rows"], x["close"], est, ev, 0)
        extra = [w for w in x["_chart"] if "52주" in w or "시장" in w]
        sc += sum(1 if w.startswith("＋") else -1 for w in extra)
        why += extra
        ld = SW.long_downtrend(x["_rows"], x["close"])
        if ld:
            ok, ev_n = SW.turnaround(why)
            if not ok:
                continue                                      # 장기 하락 + 실적 반전 근거 부족 → 후보 아님
            why.append(f"＊장기 하락 중이지만 반전 근거 {ev_n}개")
        # 손익비: 평균 목표가까지 남은 폭 vs 손절선(2×ATR)까지 폭 — 장기 하락 중엔 목표가가 늦게 내려오니 안 씀
        if m == "us":
            tu = ev.get("tgt_up")
        else:
            tg = SW._num((ev.get("est") or {}).get("cs_priceTargetMean"))
            tu = (tg / x["close"] - 1) if tg and x["close"] else None
        stop_pct = (x["close"] - x["stop"]) / x["close"] if x["close"] else None
        if not ld:
            adj, txt, rr = SW.rr_score(tu, stop_pct)
            if txt:
                sc += adj
                why.insert(0, txt)
                x["rr"] = {"up": round(tu, 4), "stop": round(stop_pct, 4), "rr": round(rr, 2)}
                if rr < MIN_RR:
                    x["no_buy"] = f"손익비 {rr:.1f} — 기준 {MIN_RR:g} 미만(목표가까지 남은 게 적음)"
        x["score"] = sc
        x["reason"] = f"현재·미래 점수 {sc:+d}: " + " ".join(why[:10])
        nws = ev.get("news") or []
        if not nws and os.getenv("ANTHROPIC_API_KEY"):
            try:
                from bot.news import recent_news
                nws = recent_news(x["name"], m.upper(), days=7, n=8)
            except Exception:
                nws = []
        x["_news"] = nws
        x["_base"] = sc
        ai = REUSE.get(x["code"]) or XS.ai_read(m.upper(), x, nws)
        if ai and REUSE.get(x["code"]):
            ev["ai"] = ai
            x["score"] = sc = sc + int(ai.get("adj") or 0)
            x["_reused"] = True
        elif ai:
            ev["ai"] = ai
            x["score"] = sc = sc + ai["adj"]
        x.pop("_chart", None)
        x.pop("_rows", None)
        out.append(x)
    out.sort(key=lambda x: -x["score"])
    # 2단계: 실제로 살 만한 상위 후보만 가장 좋은 모델로 다시 깊게 읽고 확정 (치명적 위험이면 매수 금지 표시)
    nf = int(os.getenv("CLAUDE_FINAL_N", "") or 10)
    for x in out[:nf]:
        ev = x["ev"]
        if ev.get("dart_flags") or (x.get("_reused") and (ev.get("ai") or {}).get("final")):
            continue
        fin = XS.ai_final(m.upper(), x, x["_news"], ev.get("ai") or {})
        if fin:
            ev["ai"] = fin
            x["score"] = x["_base"] + fin["adj"]
    for x in out:
        ev, ai = x["ev"], x["ev"].get("ai")
        if ai:
            tag = "AI 최종" if ai.get("final") else "AI 1차"
            x["reason"] += f" · {tag} {ai['view']}({ai['adj']:+d}): {ai['catalyst']} / 위험: {ai['risk']}"
            if ai.get("veto"):
                x["reason"] += f" · ⛔ AI 매수 금지: {ai['veto']}"
        if ev.get("dart_flags"):
            x["reason"] += " · ⚠️ 최근 공시: " + " · ".join(ev["dart_flags"])
        x.pop("_news", None)
        x.pop("_base", None)
        x.pop("_reused", None)
    out.sort(key=lambda x: (bool((x["ev"].get("ai") or {}).get("veto")) or bool(x.get("no_buy")), -x["score"]))
    if XS.USAGE:
        print("Claude 사용량(입력·출력 토큰):", XS.USAGE, file=sys.stderr)
    return out[:keep]


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
        # 시장 상태 기준선: 미장 200일선(찾는 기간 17~22·확인 기간 23~26 둘 다 칼마 개선, 50일선은 겨우 넘었다 내렸다 오락가락),
        # 국장 50일선(국장은 200일선이 더 나빴음)
        n = 200 if m == "us" else 50
        mref = B.sma(si["c"], n)
        m50 = B.sma(si["c"], 50)
        reg = bool(si["c"][-1] > mref[-1])
        ix = {"close": round(float(si["c"][-1]), 2), "ma50": round(float(mref[-1]), 2), "ma_n": n,
              "ma50_real": round(float(m50[-1]), 2), "date": si["d"][-1]}
    out = {"market": m.upper(), "date": last, "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
           "regime": reg, "index": ix, "universe": len(S), "picks": picks}
    return out


if __name__ == "__main__":
    main()
