"""일봉 스윙 (며칠~몇 주 보유) — 단타 대신 '남들보다 오래 버티는' 쪽의 우위를 노림.

근거: tools/swingbt.py · tools/swing_check.py 로 3년치 일봉 백테스트 + 같은 종목군에서 '아무거나 산 것'과 비교해
      우위가 확인된 것만 켬 (미장: 52주 신고가 돌파, 국장: 큰 재료 갭).
흐름
  1) 장 끝난 뒤 GitHub Actions(swingpicks.yml)가 신호 종목을 picks.json 으로 올림 (시장 국면 판단 포함)
  2) 다음 날 장 시작 1분 뒤 로봇이 그 종목을 시장가로 삼 (손절선 아래로 갭하락해 열리면 안 삼)
  3) 손절은 토스 서버의 시장가 손절 조건주문으로 계속 걸어 둠 (밤사이에도)
  4) 매일 장 마감 15분 전 일봉 기준 점검: +1R 넘으면 본전, +2R 넘으면 '최고 종가 − 3×ATR' 추적,
     10일선 깨지면 정리, 10거래일 지나도 +0.5R 못 가면 정리, 최대 60거래일
"""
from __future__ import annotations

import datetime as dt


def load_picks(url: str, market: str, session_date: dt.date, log=print) -> dict | None:
    """전날 장 끝난 뒤 만든 신호 목록. 날짜가 너무 오래됐으면 None."""
    import requests
    try:
        r = requests.get(url.format(m=market.lower()), timeout=20)
        if r.status_code != 200:
            log(f"🧭 스윙 후보를 못 받았어요 (HTTP {r.status_code})")
            return None
        d = r.json()
    except Exception as e:
        log(f"🧭 스윙 후보를 못 받았어요: {e}")
        return None
    try:
        pd_ = dt.date.fromisoformat(d.get("date", ""))
    except ValueError:
        return None
    gap = (session_date - pd_).days
    if gap <= 0 or gap > 4:
        log(f"🧭 스윙 후보 날짜({pd_})가 오늘 장({session_date})과 안 맞아서 안 써요")
        return None
    return d


def qty_for(capital: float, risk_pct: float, cap_pct: float, entry: float, stop: float, cash: float | None,
            min_fill: float = 0.5) -> int:
    """손절 시 손실 = 투자금의 risk_pct%, 한 종목 최대 cap_pct%, 현금 안에서."""
    per = entry - stop
    if per <= 0 or entry <= 0 or not capital:
        return 0
    q = min(capital * risk_pct / 100 / per, capital * cap_pct / 100 / entry)
    if cash is not None and cash * 0.97 / entry < q:
        if cash * 0.97 / entry < q * min_fill:
            return 0                   # 현금이 모자라 계획 수량의 절반도 못 사면 안 삼 (몇 주짜리 의미 없는 매수 방지)
        q = cash * 0.97 / entry
    return int(q)


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def atr(highs, lows, closes, n=20):
    if len(closes) < n + 1:
        return None
    trs = [max(highs[i], closes[i - 1]) - min(lows[i], closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    return sum(trs) / n


def daily_check(p_entry: float, stop: float, stop0: float, hi: float, held: int, bars: list, c: dict, cost: float = 0.003):
    """일봉 점검 (오늘 = bars 마지막, 종가 자리에 지금 가격).
    bars: [(날짜, 고가, 저가, 종가)] 오래된 것부터.
    돌려줌: (새 손절선, 새 최고 종가, 정리 이유 또는 None, 설명)"""
    highs = [b[1] for b in bars]
    lows = [b[2] for b in bars]
    closes = [b[3] for b in bars]
    close = closes[-1]
    R = max(p_entry - stop0, p_entry * 0.005)
    hi = max(hi or p_entry, close)
    a = atr(highs, lows, closes) or R / 2
    new, notes = stop, []
    if hi >= p_entry + c.get("be_after_R", 1) * R:
        be = p_entry * (1 + cost)
        if be > new:
            new = be
            notes.append("+1R 넘음 → 손절선 본전(수수료 포함)")
    if hi >= p_entry + c.get("trail_after_R", 2) * R:
        tr = hi - c.get("trail_atr", 3) * a
        if tr > new:
            new = tr
            notes.append(f"추적: 최고 종가 − {c.get('trail_atr', 3):g}×ATR")
    m = sma(closes, c["exit_ma"]) if c.get("exit_ma") else None
    why = None
    if m and held >= 2 and close < m and close < p_entry + 3 * R:
        why = f"{c.get('exit_ma', 10)}일선 아래로 마감 (추세 꺾임)"
    elif held >= c.get("time_stop_days", 10) and close < p_entry + c.get("time_need_R", 0.5) * R:
        why = f"{held}거래일 지나도 +{c.get('time_need_R', 0.5):g}R 못 감 (힘 없음)"
    elif held >= c.get("max_hold", 60):
        why = f"최대 보유 {c.get('max_hold', 60)}거래일"
    return new, hi, why, " · ".join(notes)


# ── 실적 발표 다루기 ───────────────────────────────────────────────────────────
# 백테스트(tools/earnbt.py, 미장 2년·500종목): 실적 전에 무조건 파는 것(-50%p)·무조건 안 사는 것(-16%p)은 수익을 크게 깎음.
# '실적 갭 크기 기준으로 수량을 줄이고 들고 가기'가 수익은 그대로(+135%)·낙폭은 줄임(19.2→18.4%).
# 과거 '예상 상회 횟수'는 예측력이 없었음(강함 10건 승률 10%) → 대신 최근 30일 애널리스트 전망치 방향을 봄
# (전망치 하향은 흔한 위험 신호, 백테스트 자료가 아직 없어 '실적 앞둔 매수'에만 씀).

def biz_days(a: dt.date, b: dt.date) -> int:
    """a 다음 날부터 b 까지 평일 수 (b 가 a 이전이면 음수/0)."""
    if b <= a:
        return 0
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def next_biz(a: dt.date) -> dt.date:
    d = a + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def rev_state(ev: dict) -> tuple[str, str]:
    """애널리스트 전망치 방향: ('up'|'down'|'flat'|'?', 설명)."""
    now_, ago = ev.get("eps0q"), ev.get("eps0q_30d")
    up, dn = ev.get("up30") or 0, ev.get("down30") or 0
    ch = None
    if now_ is not None and ago:
        ch = (now_ - ago) / abs(ago) * 100
    if ch is None and not (up or dn):
        return "?", "전망치 자료 없음"
    txt = (f"이번 분기 EPS 전망 30일간 {ch:+.1f}%" if ch is not None else "EPS 전망") + f" (상향 {up:g}·하향 {dn:g})"
    if (ch is not None and ch <= -1.0) or (dn >= 2 and dn > up):
        return "down", txt
    if (ch is not None and ch >= 1.0) or up > dn:
        return "up", txt
    return "flat", txt


def in_kr_season(today: dt.date, ec: dict) -> bool:
    """국장 실적 시즌(분기 끝난 뒤 한 달쯤) 안인지. 국장은 실적일 자료가 비어 있는 경우가 많아 시즌으로 보수적으로 판단."""
    md = today.strftime("%m-%d")
    return any(a <= md <= b for a, b in ec.get("kr_season") or [])


def earn_plan(x: dict, today: dt.date, entry: float, stop: float, ec: dict, market: str = "US") -> tuple[str, float, str]:
    """매수 전 실적 점검 → (결정, 수량 계산용 1주 위험, 설명).
    결정: ok(실적 없음) / hold(실적 끼고 들고 감, 실적 갭 기준으로 작게) / skip(안 삼)."""
    ev = x.get("ev") or {}
    per = entry - stop
    react = ev.get("react")
    if ec.get("enabled") and not react and market == "KR" and in_kr_season(today, ec):
        gap = float(ev.get("gap_avg") or ec.get("kr_season_gap", 0.08))
        per2 = max(per, entry * gap * ec.get("gap_mult", 2))
        return "hold", per2, (f"국장 실적 시즌인데 실적일을 몰라서 실적이 있다고 보고 수량 줄임 "
                              f"(갭 {gap * 100:.0f}%의 {ec.get('gap_mult', 2):g}배가 나도 손실 한도 안)")
    if not ec.get("enabled") or not react:
        return "ok", per, ("실적일 모름" if ec.get("enabled") and x.get("setup") in ("hi52", "dip") and not ev.get("earn") else "")
    try:
        rd = dt.date.fromisoformat(react)
    except ValueError:
        return "ok", per, ""
    n = biz_days(today, rd)
    win = (ec.get("window") or {}).get(x.get("setup"), 10)
    rv, rtxt = rev_state(ev)
    when = f"실적 반응일 {rd.month}/{rd.day}({n}거래일 뒤)"
    if rd <= today or n > win:
        return "ok", per, f"{when} · 보유 기간 밖" if n > win else ""
    if rv == "down" and ec.get("skip_if_rev_down", True):
        return "skip", per, f"{when}인데 애널리스트 전망치가 내려가는 중 ({rtxt})"
    gap = float(ev.get("gap_avg") or ec.get("default_gap", 0.07))
    per2 = max(per, entry * gap * ec.get("gap_mult", 2))
    return "hold", per2, (f"{when} · 들고 감(평균 실적 갭 {gap * 100:.0f}%의 {ec.get('gap_mult', 2):g}배가 나도 손실 한도 안으로 수량 줄임) · {rtxt}")


def ev_text(ev: dict, etxt: str) -> str:
    """매수 알림에 붙일 실적·애널리스트 요약."""
    parts = []
    if etxt:
        parts.append(etxt)
    elif ev.get("react"):
        parts.append(f"다음 실적 {ev['react']}")
    if ev.get("tgt_up") is not None:
        parts.append(f"애널리스트 목표가 평균 {ev['tgt_up'] * 100:+.0f}% 위"
                     + (f"·추천 {ev['rec']:.1f}/5(낮을수록 매수)" if ev.get("rec") else ""))
    if ev.get("sector"):
        parts.append(f"업종 {ev.get('industry') or ev['sector']}")
    return ("\n📅 " + " · ".join(parts)) if parts else ""


# ── 보유 종목 브리핑: 오늘 봉·거래량·애널리스트·실적일·수급·뉴스 ─────────────────────────────
def load_estimates(url: str, market: str, today: dt.date, log=print) -> dict:
    """GitHub 'estimates' 브랜치의 하루 치 컨센서스 CSV(코드 → 행). 오늘 것이 없으면 5일 전까지 거슬러 감."""
    import csv
    import io
    import requests
    for k in range(6):
        d = (today - dt.timedelta(days=k)).isoformat()
        try:
            r = requests.get(url.format(m=market.lower(), d=d), timeout=20)
        except Exception:
            continue
        if r.status_code == 200 and r.text.strip():
            rows = {}
            for row in csv.DictReader(io.StringIO(r.text)):
                code = str(row.get("code") or "")
                if market == "KR" and code.isdigit():
                    code = code.zfill(6)
                rows[code] = row
            return rows
    log("📋 컨센서스 자료를 못 받았어요 (브리핑에서 애널리스트 정보 빠짐)")
    return {}


def _num(x):
    try:
        v = float(x)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def brief_text(market: str, bars: list, last: float, est: dict | None, earn_react: str | None, news: list[str],
               today: dt.date, money=lambda v: f"{v:,.2f}") -> str:
    """bars: [(날짜, 시가, 고가, 저가, 종가, 거래량)] 오래된 것부터, 마지막이 오늘(장중이면 지금까지)."""
    parts = []
    if len(bars) >= 2:
        _, o, h, l, c, v = bars[-1]
        c = last or c
        prev = bars[-2][4]
        day = (c / prev - 1) * 100 if prev else 0
        candle = "양봉" if c >= o else "음봉"
        rng = h - l
        pos = "고가 근처 마감" if rng and (c - l) / rng >= 0.7 else "저가 근처" if rng and (c - l) / rng <= 0.3 else ""
        vols = [b[5] for b in bars[-21:-1] if b[5]]
        vx = v / (sum(vols) / len(vols)) if vols and v else None
        lbl = "오늘" if bars[-1][0] == today else f"{bars[-1][0].month}/{bars[-1][0].day}"
        parts.append(f"{lbl} {candle} {day:+.1f}%" + (f"·{pos}" if pos else "") + (f" · 거래량 평소 {vx:.1f}배" if vx else ""))
        if len(bars) >= 20:
            ma20 = sum(b[4] for b in bars[-20:]) / 20
            parts.append(f"20일선 {'위' if c >= ma20 else '아래'}({(c / ma20 - 1) * 100:+.1f}%)")
    est = est or {}
    if market == "US":
        tg, rec, n = _num(est.get("targetMeanPrice")), _num(est.get("recommendationMean")), _num(est.get("numberOfAnalystOpinions"))
        if tg and last:
            parts.append(f"애널리스트 {int(n) if n else '?'}명 목표가 평균 {money(tg)} ({(tg / last - 1) * 100:+.0f}%)"
                         + (f"·의견 {rec:.1f}(1=강력매수, 5=매도)" if rec else ""))
        e0, e30 = _num(est.get("eps_0q_current")), _num(est.get("eps_0q_30daysAgo"))
        if e0 is not None and e30:
            parts.append(f"이번 분기 이익 전망 30일간 {(e0 - e30) / abs(e30) * 100:+.1f}%")
    else:
        tg, rec = _num(est.get("cs_priceTargetMean")), _num(est.get("cs_recommMean"))
        if tg and last:
            parts.append(f"증권사 목표가 평균 {money(tg)} ({(tg / last - 1) * 100:+.0f}%)" + (f"·의견 {rec:.1f}(5=강력매수)" if rec else ""))
        fo, og = _num(est.get("foreignerPureBuyQuant")), _num(est.get("organPureBuyQuant"))
        if fo is not None or og is not None:
            parts.append(f"전날 외국인 {fo or 0:+,.0f}주·기관 {og or 0:+,.0f}주")
    if earn_react:
        try:
            rd = dt.date.fromisoformat(earn_react)
            if rd >= today:
                parts.append(f"다음 실적 반응일 {rd.month}/{rd.day} ({biz_days(today, rd)}거래일 뒤)")
        except ValueError:
            pass
    out = "📊 " + " · ".join(parts) if parts else ""
    if news:
        out += "\n📰 " + " / ".join(t[:90] for t in news[:3])
    return out
