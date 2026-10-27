"""단타 진입 신호 6가지: 시초 돌파 · VWAP 눌림 반등 · 오늘 고점 재돌파 · 지지선 반등 · 레드→그린 · 데스크 가격 계획.

쉬운 설명
- 장 시작 후 처음 N분(기본 15분)의 최고가·최저가를 '시초 범위'로 잡는다.
- 그 뒤 주가가 시초 범위 최고가를 뚫고(돌파), 오늘 평균 매수 단가(VWAP) 위에 있고,
  직전 1분 거래량이 평소 1분 거래량의 몇 배(기본 2배) 이상이면 '진입 신호'.
- 이미 너무 많이 오른 종목(기본 +18% 이상), 시초 범위가 너무 넓은 종목은 뺀다.

분봉은 REST 1분 캔들(정확한 거래량)로 채우고, 웹소켓 체결로 '지금 가격'을 바로 반영한다.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .util import f, parse_ts


@dataclass
class Bar:
    t: dt.datetime  # 봉 시작 시각 (분 단위)
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class SymState:
    symbol: str
    name: str = ""
    prev_close: float | None = None
    bars: dict = field(default_factory=dict)  # 분 시각 -> Bar
    last: float | None = None
    last_ts: float = 0.0
    excluded: str | None = None  # 제외 이유 (경고 종목 등)
    cooldown_until: dt.datetime | None = None
    etf: bool = False            # ETF·ETN(레버리지 포함): 하루 안에 정리, 불타기·스윙 안 함
    entries_today: int = 0

    # REST 캔들로 분봉 갱신 (거래량은 이게 정확)
    def load_candles(self, candles: list[dict], market: str):
        for c in candles:
            t = parse_ts(c.get("timestamp"), market)
            if t is None:
                continue
            t = t.replace(second=0, microsecond=0)
            self.bars[t] = Bar(t, f(c.get("openPrice")), f(c.get("highPrice")), f(c.get("lowPrice")),
                               f(c.get("closePrice")), f(c.get("volume"), 0.0))
        if self.bars and self.last is None:
            self.last = self.sorted_bars()[-1].c

    # 웹소켓 체결 반영 (가격 즉시, 거래량은 근사치)
    def on_trade(self, price: float, vol: float, t: dt.datetime):
        self.last = price
        self.last_ts = t.timestamp()
        k = t.replace(second=0, microsecond=0)
        b = self.bars.get(k)
        if b is None:
            self.bars[k] = Bar(k, price, price, price, price, vol)
        else:
            b.h, b.l, b.c, b.v = max(b.h, price), min(b.l, price), price, b.v + vol

    def sorted_bars(self) -> list[Bar]:
        return [self.bars[k] for k in sorted(self.bars)]


def session_bars(st: SymState, open_t: dt.datetime) -> list[Bar]:
    return [b for b in st.sorted_bars() if b.t >= open_t and b.o is not None]


def vwap(bars: list[Bar]) -> float | None:
    pv = sum(((b.h + b.l + b.c) / 3) * b.v for b in bars)
    v = sum(b.v for b in bars)
    return pv / v if v > 0 else None


def opening_range(bars: list[Bar], open_t: dt.datetime, minutes: int):
    end = open_t + dt.timedelta(minutes=minutes)
    rb = [b for b in bars if open_t <= b.t < end]
    if len(rb) < max(3, minutes // 3):
        return None
    return max(b.h for b in rb), min(b.l for b in rb)


def _common(st: SymState, now: dt.datetime, open_t: dt.datetime, cfg: dict, early: bool = False, earn: bool = False,
            adding: bool = False):
    """모든 매수 방식에 공통인 거름망. 통과하면 (bars, done, p, vw) 를, 아니면 None.
    early=True 는 장 시작 직후(⑦ 장초반 출발)용: 15분 기다림·봉 개수 조건을 건너뜀."""
    s = cfg["strategy"]
    if st.excluded or st.last is None:
        return None
    if not adding and st.cooldown_until and now < st.cooldown_until:
        return None
    if not adding and st.entries_today >= s.get("max_entries_per_symbol", 1):
        return None
    if not early and now < open_t + dt.timedelta(minutes=s["orb_minutes"]):
        return None
    if now.timestamp() - st.last_ts > 90 and st.last_ts:  # 시세가 1분 반 넘게 멈춤 (VI·거래정지 가능)
        return None
    p = st.last
    lo_p, hi_p = s["price_range"][cfg["market"]]
    if p < lo_p or p > hi_p:
        return None
    if st.prev_close:
        chg = (p / st.prev_close - 1) * 100
        cap = s.get("earn_max_day_change_pct", 25) if earn else s["max_day_change_pct"]
        if chg > cap or chg < s.get("min_day_change_pct_any", -8):
            return None
    bars = session_bars(st, open_t)
    cur_min = now.replace(second=0, microsecond=0)
    done = [b for b in bars if b.t < cur_min]
    if len(done) < (1 if early else s["orb_minutes"]):
        return None
    return bars, done, p, vwap(bars)


def _volx(done: list[Bar], n: int | None = None) -> float:
    last, prev = done[-1], done[:-1]
    if n:
        prev = prev[-n:]
    avg = sum(b.v for b in prev) / len(prev) if prev else 0
    return last.v / avg if avg > 0 else 0.0


def sig_orb(st, now, open_t, cfg, bars, done, p, vw, ctx):
    """① 시초 돌파: 장 시작 15분 고점을 거래량 실어 뚫음."""
    s = cfg["strategy"]
    rng = opening_range(bars, open_t, s["orb_minutes"])
    if not rng or vw is None:
        return None
    hi, lo = rng
    max_rng = s.get("earn_max_range_pct", 15) if st.symbol in (ctx or {}).get("earn_good", {}) else s["max_range_pct"]
    if lo <= 0 or (hi - lo) / lo * 100 > max_rng:
        return None
    if st.prev_close and (p / st.prev_close - 1) * 100 < s["min_day_change_pct"]:
        return None
    if p < hi * (1 + s["breakout_buffer_pct"] / 100) or p > hi * (1 + s["max_chase_pct"] / 100):
        return None
    if p < vw:
        return None
    vx = _volx(done)
    if vx < s["volume_mult"] or done[-1].c < done[-1].o:
        return None
    return {"setup": "orb", "reason": f"시초 {s['orb_minutes']}분 고점 {hi:g} 돌파 · VWAP 위 · 거래량 {vx:.1f}배",
            "or_high": hi, "or_low": lo, "vwap": vw, "vol_x": vx, "level": min(hi, vw)}


def sig_vwap(st, now, open_t, cfg, bars, done, p, vw, ctx):
    """② VWAP 눌림 반등: 아침에 강했던 종목이 VWAP 까지 쉬었다가 거래량 실어 다시 올라섬."""
    c = cfg["strategy"]["setups"]["vwap"]
    if vw is None or now < open_t + dt.timedelta(minutes=c["after_min"]):
        return None
    day_hi = max(b.h for b in done)
    if day_hi < vw * (1 + c["min_run_pct"] / 100):          # 아침에 VWAP 보다 충분히 올랐던 종목만
        return None
    look = done[-c["look_bars"]:]
    pull_lo = min(b.l for b in look)
    if not (vw * (1 - c["max_below_pct"] / 100) <= pull_lo <= vw * (1 + c["touch_pct"] / 100)):
        return None                                           # VWAP 근처까지 눌렸고, 크게 깨지진 않음
    lb, pb = done[-1], done[-2]
    if not (lb.c > lb.o and lb.c > vw and lb.c >= pb.h):      # 양봉으로 VWAP 위·직전 봉 고점 위 마감
        return None
    if not (vw < p <= vw * (1 + c["max_chase_pct"] / 100)):
        return None
    vx = _volx(done, 10)
    if vx < c["volume_mult"]:
        return None
    return {"setup": "vwap", "reason": f"VWAP {vw:g} 눌림 후 반등 · 오늘 고점 {day_hi:g} · 거래량 {vx:.1f}배",
            "or_high": vw, "vwap": vw, "vol_x": vx, "level": pull_lo}


def sig_hod(st, now, open_t, cfg, bars, done, p, vw, ctx):
    """③ 오늘 고점 재돌파: 고점 아래에서 좁게 쉬다가(받침) 거래량 실어 오늘 최고가를 다시 뚫음."""
    c = cfg["strategy"]["setups"]["hod"]
    if vw is None or now < open_t + dt.timedelta(minutes=c["after_min"]):
        return None
    n = c["base_bars"]
    if len(done) < n + 10:
        return None
    base, before = done[-n - 1:-1], done[:-n - 1]           # 받침 = 돌파 봉 직전 n개
    hod = max(b.h for b in before)
    b_hi, b_lo = max(b.h for b in base), min(b.l for b in base)
    if b_hi > hod * 1.002 or b_lo <= 0 or (b_hi - b_lo) / b_lo * 100 > c["max_base_pct"] or b_lo < vw:
        return None
    if not (hod * 1.001 < p <= hod * (1 + c["max_chase_pct"] / 100)):
        return None
    vx = _volx(done)
    if vx < c["volume_mult"] or done[-1].c < done[-1].o:
        return None
    return {"setup": "hod", "reason": f"오늘 고점 {hod:g} 재돌파 · {n}분 받침 · 거래량 {vx:.1f}배",
            "or_high": hod, "vwap": vw, "vol_x": vx, "level": b_lo}


def sig_support(st, now, open_t, cfg, bars, done, p, vw, ctx):
    """④ 지지선 반등: 일봉 차트의 지지선(여러 번 버틴 가격)에 닿았다가 거래량 실어 돌아섬."""
    c = cfg["strategy"]["setups"]["support"]
    d = (ctx or {}).get("daily", {}).get(st.symbol) or {}
    sup = d.get("support")
    if not sup:
        return None
    day_lo = min(b.l for b in done)
    if not (sup * (1 - c["max_break_pct"] / 100) <= day_lo <= sup * (1 + c["touch_pct"] / 100)):
        return None
    lb = done[-1]
    prev3 = done[-4:-1]
    if not (lb.c > lb.o and prev3 and lb.c > max(b.h for b in prev3)):
        return None                                            # 양봉으로 직전 3개 봉 고점을 넘김(반전 확인)
    if not (sup * 1.003 <= p <= sup * (1 + c["max_chase_pct"] / 100)):
        return None
    vx = _volx(done, 15)
    if vx < c["volume_mult"]:
        return None
    return {"setup": "support", "reason": f"일봉 지지선 {sup:g} 반등 · 오늘 저점 {day_lo:g} · 거래량 {vx:.1f}배",
            "or_high": sup, "vwap": vw, "vol_x": vx, "level": min(day_lo, sup)}


def sig_r2g(st, now, open_t, cfg, bars, done, p, vw, ctx):
    """⑤ 레드→그린: 어제보다 낮게 시작했다가 어제 종가를 거래량 실어 위로 넘어섬(매도세가 꺾임)."""
    c = cfg["strategy"]["setups"]["r2g"]
    pc = st.prev_close
    if not pc or vw is None or not bars:
        return None
    if bars[0].o > pc * (1 - c["min_gap_pct"] / 100):          # 시가가 어제 종가보다 충분히 낮게 시작
        return None
    if not (pc * 1.001 < p <= pc * (1 + c["max_chase_pct"] / 100)) or p < vw:
        return None
    if max(b.c for b in done[:-1]) > pc:                        # 오늘 처음 넘는 순간만
        return None
    vx = _volx(done)
    if vx < c["volume_mult"] or done[-1].c < done[-1].o:
        return None
    return {"setup": "r2g", "reason": f"갭하락 뒤 어제 종가 {pc:g} 회복 · VWAP 위 · 거래량 {vx:.1f}배",
            "or_high": pc, "vwap": vw, "vol_x": vx, "level": min(vw, min(b.l for b in done[-10:]))}


def sig_plan(st, now, open_t, cfg, bars, done, p, vw, ctx):
    """⑥ 데스크 가격 계획: 주식비서 리포트의 1순위·픽이 진입 구간 안에서 반등."""
    c = cfg["strategy"]["setups"]["plan"]
    pl = (ctx or {}).get("plans", {}).get(st.symbol)
    if not pl or not pl.get("stop") or not pl.get("entryLow") or not pl.get("entryHigh"):
        return None
    lo, hi, stop = float(pl["entryLow"]), float(pl["entryHigh"]), float(pl["stop"])
    if not (lo <= p <= hi * 1.003) or p <= stop * 1.01:
        return None
    lb, pb = done[-1], done[-2]
    if not (lb.c > lb.o and lb.c >= pb.c):
        return None
    vx = _volx(done, 15)
    if vx < c["volume_mult"]:
        return None
    return {"setup": "plan", "reason": f"데스크 {pl.get('src', '추천')} 진입 구간 {lo:g}~{hi:g} 반등 · 거래량 {vx:.1f}배",
            "or_high": lo, "vwap": vw, "vol_x": vx, "level": stop, "stop": stop, "t1": pl.get("t1")}


def sig_open(st, now, open_t, cfg, bars, done, p, vw, ctx):
    """⑦ 장초반 출발: 전날 데스크·차트가 고른 종목이 장 시작 직후 예상대로 힘 있게 출발하면 15분 안 기다리고 바로.
    조건: 고른 종목 · 시가가 어제보다 적당히 높게(갭 0.5~6%) · 첫 1~2분 봉 고점 돌파 · 시가·VWAP 위 ·
          첫 봉 거래량이 평소 1분 거래량의 4배 이상 · 데스크 1차 목표가 아래."""
    c = cfg["strategy"]["setups"]["open"]
    if not (open_t + dt.timedelta(minutes=c["start_min"]) <= now < open_t + dt.timedelta(minutes=c["end_min"])):
        return None
    eg = (ctx or {}).get("earn_good", {}).get(st.symbol)
    pre = (ctx or {}).get("pre", {}).get(st.symbol)        # 프리마켓에서 이미 오른 종목
    if st.symbol not in (ctx or {}).get("watched", ()) and not eg and pre is None:
        return None
    pc = st.prev_close
    if not pc or not bars or vw is None:
        return None
    o = bars[0].o
    gap = (o / pc - 1) * 100
    mx = c.get("earn_max_gap_pct", 12) if eg else c.get("pre_max_gap_pct", 10) if pre is not None else c["max_gap_pct"]
    if not (c["min_gap_pct"] <= gap <= mx):
        return None
    first = done[:2]
    hi = max(b.h for b in first)
    if not (hi * 1.001 < p <= hi * (1 + c["max_chase_pct"] / 100)) or p < o or p < vw:
        return None
    if done[-1].c < done[-1].o:
        return None
    d = ctx.get("daily", {}).get(st.symbol) or {}
    pl = ctx.get("plans", {}).get(st.symbol) or {}
    if pl.get("t1") and p >= float(pl["t1"]) * 0.98:        # 데스크 1차 목표에 이미 가까우면 안 삼
        return None
    vm = d.get("vol_min")                                    # 최근 20일 평균 1분 거래량
    if vm:
        vx = done[0].v / vm
        if vx < c["vol_mult_min"]:
            return None
    else:                                                    # 평소 거래량 자료가 없는 데스크 종목: 연속 양봉으로 대신 확인
        if (not pl and pre is None) or len(done) < 2 or not all(b.c > b.o for b in done[-2:]) or done[-1].c <= done[-2].c:
            return None
        vx = c["vol_mult_min"]
    src = ("실적 서프라이즈" if eg else pl.get("src") or (f"프리마켓 {pre:+.1f}%" if pre is not None else "")
           or ("차트 " + d.get("scan", "")).strip())
    return {"setup": "open", "reason": f"장초반 출발({src}) · 갭 {gap:+.1f}% · 첫 봉 고점 {hi:g} 돌파 · 거래량 평소 {vx:.0f}배",
            "or_high": hi, "vwap": vw, "vol_x": min(vx / 2, 6), "level": max(min(b.l for b in done), vw * 0.995)}


SETUPS = [("open", sig_open), ("orb", sig_orb), ("hod", sig_hod), ("vwap", sig_vwap), ("plan", sig_plan),
          ("support", sig_support), ("r2g", sig_r2g)]
PRIORITY = {"open": 3.2, "orb": 3.0, "hod": 2.6, "plan": 2.8, "vwap": 2.4, "support": 2.2, "r2g": 2.0}


def entry_signal(st: SymState, now: dt.datetime, open_t: dt.datetime, cfg: dict, ctx: dict | None = None,
                 adding: bool = False) -> dict | None:
    """켜 둔 매수 방식들을 차례로 확인해 가장 점수 높은 신호 하나를 돌려준다.
    신호: {setup, reason, level(손절 기준 가격), vwap, vol_x, score, ...}"""
    ctx = ctx or {}
    if st.symbol in ctx.get("earn_block", ()):       # 오늘·내일 실적 발표 → 새로 사지 않음
        return None
    on = cfg["strategy"].get("setups", {})
    early = now < open_t + dt.timedelta(minutes=cfg["strategy"]["orb_minutes"])
    if early and not (on.get("open") or {}).get("enabled"):
        return None
    base = _common(st, now, open_t, cfg, early=early, earn=st.symbol in ctx.get("earn_good", {}), adding=adding)
    if not base:
        return None
    bars, done, p, vw = base
    best = None
    for name, fn in SETUPS:
        if early and name != "open":
            continue
        if name != "orb" and not (on.get(name) or {}).get("enabled"):
            continue
        if name == "orb" and not cfg["strategy"].get("orb_enabled", True):
            continue
        try:
            sig = fn(st, now, open_t, cfg, bars, done, p, vw, ctx)
        except (ValueError, ZeroDivisionError, IndexError):
            sig = None
        if not sig:
            continue
        sc = PRIORITY.get(name, 2.0) + min(sig.get("vol_x", 0), 6) * 0.25
        d = ctx.get("daily", {}).get(st.symbol) or {}
        if d.get("label") == "관심":
            sc += 0.6
        elif d.get("label") == "주의":
            sc -= 0.8
        if st.symbol in ctx.get("plans", {}):
            sc += 0.8                                # 데스크가 고른 종목이면 가산
            pl = ctx["plans"][st.symbol]
            if (pl.get("score") or 0) >= 70 or pl.get("src") in ("1순위", "장중 1순위"):
                sc += 0.5                            # 데스크 종합 점수 70+ 또는 1순위
        if st.symbol in ctx.get("earn_good", {}):
            sc += 1.2                                # 실적 서프라이즈 종목 가산
            sig["reason"] = "실적 서프라이즈 · " + sig["reason"]
        elif st.symbol in ctx.get("watched", ()) or st.symbol in ctx.get("pre", {}):
            sc += 0.3
        sig["score"] = round(sc, 2)
        if not best or sig["score"] > best["score"]:
            best = sig
    return best


def atr(bars: list[Bar], n: int = 14) -> float | None:
    """1분봉 평균 흔들림 폭(ATR). 이 종목이 평소 1분에 얼마나 출렁이는지."""
    if len(bars) < 3:
        return None
    trs = []
    for a, b in zip(bars, bars[1:]):
        trs.append(max(b.h - b.l, abs(b.h - a.c), abs(b.l - a.c)))
    trs = trs[-n:]
    return sum(trs) / len(trs) if trs else None


def exit_levels(market: str, entry: float, sig: dict, cfg: dict, st: SymState | None = None, open_t=None):
    """첫 손절선과 '안전용' 최대 익절가.

    손절: 잠깐 출렁임에 털리지 않게 ①돌파 지점·VWAP 바로 아래 ②평소 1분 흔들림(ATR)의 몇 배
          중 더 넓은 쪽을 쓰되, 최소 min_pct ~ 최대 max_pct 안으로 맞춘다.
    익절: 고정 목표로 일찍 팔지 않는다. 최대 익절가(hard_pct)는 급등 대비 안전장치일 뿐이고,
          실제로는 오르는 동안 손절선을 따라 올리며(추적 손절) 꺾일 때 판다.
    """
    from .util import round_tick
    s = cfg["strategy"]["stop"]
    t = cfg["strategy"]["take_profit"]
    a = None
    if st is not None and open_t is not None:
        a = atr(session_bars(st, open_t))
    a = a or entry * 0.004
    lvl = sig.get("level") or min(sig["or_high"], sig.get("vwap") or sig["or_high"])
    struct = lvl * (1 - s["below_level_pct"] / 100)
    dist = max(entry - struct, a * s["atr_mult"])
    if sig.get("stop"):                       # 데스크 계획 손절선이 있으면 그걸 우선(허용 범위 안에서)
        dist = entry - float(sig["stop"])
    max_pct = s.get("etf_max_pct", 7.0) if (st is not None and getattr(st, "etf", False)) else s["max_pct"]
    dist = min(max(dist, entry * s["min_pct"] / 100), entry * max_pct / 100)
    sl = round_tick(market, entry - dist, "down")
    tp = round_tick(market, entry * (1 + t["hard_pct"] / 100), "up")
    return tp, sl, entry - sl, a
