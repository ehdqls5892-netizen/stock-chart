"""주문 실행: 연습(paper) / 실전(live).

공통 흐름
  진입 → (체결되면) 익절·손절을 토스 서버에 OCO 조건주문으로 건다
  → 이익이 어느 정도 나면 손절선을 산 가격 위로 올린다(본전 지키기)
  → 장 마감 전 정해진 시각에 남은 물량은 전부 판다.

실전에서는 익절·손절을 토스 서버가 감시하므로 PC·인터넷이 끊겨도 손절은 걸려 있다.
연습 모드는 실제 주문 없이 실시간 가격으로 체결을 흉내 낸다.
"""
from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .api import TossError
from .util import TZ, add_ticks, f, money, price_str, round_tick


@dataclass
class Position:
    symbol: str
    name: str
    qty: int
    entry: float
    tp: float
    sl: float
    reason: str
    opened_at: str
    state: str = "pending"      # pending → open → closing → closed
    buy_order_id: str | None = None
    buy_sent_at: float = 0.0
    cond_id: str | None = None
    sell_order_id: str | None = None
    be_moved: bool = False
    peak: float = 0.0
    partial_done: bool = False
    exit_price: float | None = None
    exit_reason: str = ""
    pnl: float = 0.0
    last_check: float = 0.0
    extra: dict = field(default_factory=dict)


class BaseBroker:
    def __init__(self, market: str, cfg: dict, journal_dir: Path, log, notify):
        self.m, self.cfg, self.log, self.notify = market, cfg, log, notify
        self.fee = cfg["fees"][market]
        self.pos: dict[str, Position] = {}
        self.closed: list[Position] = []
        self.dir = journal_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.on_closed = None  # 콜백(pos)
        self.judge = None      # 콜백(sym) → {vwap, vol_x, market, bad}: 손절 판단용 (main 이 넣어 줌)

    def today(self) -> dt.date:
        from .util import now
        return now(self.m).date()

    # 수수료·세금 포함 손익
    def calc_pnl(self, p: Position, exit_price: float) -> float:
        buy = p.entry * p.qty
        sell = exit_price * p.qty
        cost = buy * self.fee["buy_pct"] / 100 + sell * (self.fee["sell_pct"] + self.fee["sell_tax_pct"]) / 100
        return sell - buy - cost

    def open_count(self, day_only: bool = False) -> int:
        return sum(1 for p in self.pos.values() if p.state in ("pending", "open", "closing")
                   and not (day_only and p.extra.get("carry")))

    def invested(self) -> float:
        """들고 있는(사는 중 포함) 종목에 들어간 돈."""
        return sum(p.entry * p.qty for p in self.pos.values() if p.state in ("pending", "open", "closing"))

    def carried(self) -> list:
        return [p for p in self.pos.values() if p.extra.get("carry")]

    def _journal(self, kind: str, p: Position, **kw):
        day = self.today().isoformat()
        rec = {"t": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind, **asdict(p), **kw}
        with open(self.dir / f"trades-{day}.jsonl", "a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.save_state()

    def save_state(self):
        st = {"date": self.today().isoformat(), "positions": [asdict(p) for p in self.pos.values()]}
        (self.dir / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

    def _close(self, p: Position, price: float, why: str):
        p.exit_price, p.exit_reason, p.state = price, why, "closed"
        p.pnl = self.calc_pnl(p, price)
        self.pos.pop(p.symbol, None)
        self.closed.append(p)
        pct = (price / p.entry - 1) * 100
        msg = (f"{'🟢 매도(이익)' if p.pnl >= 0 else '🔴 매도(손실)'} {p.name}({p.symbol}) {p.qty}주 "
               f"{money(self.m, p.entry)} → {money(self.m, price)} ({pct:+.2f}%) 손익 {money(self.m, p.pnl)} · {why}")
        self.log(msg)
        self.notify(msg)
        self._journal("exit", p)
        if self.on_closed:
            self.on_closed(p)

    # ───── 보유 중 관리: 나눠 익절 · 숨 쉴 공간 · 본전 지키기 · 추적 손절 ─────
    def manage(self, p: Position, last: float):
        """(새 손절선 또는 None, 나눠 팔기 (비율, 이름) 또는 None, 이유). 손절선은 올리기만 한다.

        1R(처음 손절 폭만큼) 오르면 1/3 익절해서 이익을 실제로 챙기고, 손절선은 '산 값 −0.4R'까지만 올린다
          → 남은 물량이 다시 밀려 팔려도 전체로는 본전 이상 (예전엔 바로 본전에 걸어서 +0.1~0.3% 에 자주 털렸음)
        1.5R → 남은 물량 손절선을 본전(수수료 포함) 위로.  2R → 남은 것의 절반 2차 익절 + 추적 손절 시작.
        """
        t = self.cfg["strategy"]["take_profit"]
        R = p.extra.get("R") or max(p.entry - p.sl, p.entry * 0.01)
        a = p.extra.get("atr") or p.entry * 0.004
        p.peak = max(p.peak or p.entry, last)
        new_sl, why = p.sl, ""
        if t.get("cushion_at_R") and last >= p.entry + R * t["cushion_at_R"]:
            cu = round_tick(self.m, p.entry - R * t.get("cushion_R", 0.4), "up")
            if cu > new_sl:
                new_sl, why = cu, f"이익 {t['cushion_at_R']:g}R → 손절선을 산 값 −{t.get('cushion_R', 0.4):g}R 로 (1차 익절로 전체는 본전 이상)"
        if t.get("breakeven_at_R") and last >= p.entry + R * t["breakeven_at_R"]:
            fee = self.fee["buy_pct"] + self.fee["sell_pct"] + self.fee["sell_tax_pct"]
            be = round_tick(self.m, p.entry * (1 + fee / 100), "up")
            be = max(be, add_ticks(self.m, p.entry, 1))
            if be > new_sl:
                new_sl, why = be, "본전 지키기(수수료 포함)"
                p.be_moved = True
        if t.get("trail_start_R") and p.peak >= p.entry + R * t["trail_start_R"]:
            tmin = self.cfg.get("swing", {}).get("trail_min_pct", 6.0) if p.extra.get("swing") else t["trail_min_pct"]
            gap = max(a * t["trail_atr_mult"], p.peak * tmin / 100)
            tr = round_tick(self.m, p.peak - gap, "down")
            if tr > new_sl:
                new_sl, why = tr, f"추적 손절: 고점 {money(self.m, p.peak)}에서 {gap / p.peak * 100:.1f}% 아래"
        part = None
        plist = t.get("partials") or ([[t["partial_at_R"], t.get("partial_ratio", 0.5), "절반 익절"]]
                                      if t.get("partial_at_R") else [])
        i = int(p.extra.get("parts", 1 if p.partial_done else 0))
        if i < len(plist) and p.qty >= 2:
            at, ratio, label = (list(plist[i]) + ["익절"])[:3]
            if last >= p.entry + R * at:
                part = (ratio, label)
        return (new_sl if new_sl > p.sl else None), part, why

    def part_qty(self, p: Position, ratio: float) -> int:
        """나눠 팔 수량: 최소 1주, 최소 1주는 남김."""
        return max(1, min(p.qty - 1, int(round(p.qty * ratio))))

    # ───── 손절 판단: 손절선(판단선)에 닿아도 바로 팔지 않고 '진짜 빠지는지' 본다 ─────
    def hard_level(self, p: Position, sl: float | None = None) -> float:
        """토스 서버에 거는 최후 손절선. 손실 구간에서만 판단선보다 조금 아래(지금 손절 폭의 절반, 최대 −6%).
        이익 구간(손절선 ≥ 산 값)·ETF·추가 매수한 종목은 판단선 그대로 → 챙긴 이익은 정확히 지킨다."""
        sl = p.sl if sl is None else sl
        j = self.cfg.get("stop_judge", {})
        if not j.get("enabled") or sl >= p.entry or p.extra.get("etf") or p.extra.get("adds"):
            return sl                                  # 추가 매수한 종목도 손절선 그대로(더 산 만큼 위험을 늘리지 않음)
        h = sl - (p.entry - sl) * j.get("hard_extra_R", 0.5)
        h = max(h, p.entry * (1 - j.get("hard_max_pct", 6.0) / 100))
        return min(round_tick(self.m, h, "down"), sl)

    def judge_stop(self, p: Position, last: float):
        """판단선 아래일 때: ('sell', 이유) / ('wait', 이유) / 판단선 위면 None.
        바로 파는 경우: 파는 거래량이 많음 · 오늘 평균 단가(VWAP) 1% 넘게 아래 · 시장 전체가 약함 · 악재(실적 실망·차트 주의)
                       · 최후 손절선 닿음 · 정해진 시간(10분) 안에 회복 못 함
        기다리는 경우: 거래량 적은 '살짝 밀림' + 시장 괜찮음 + 평균 단가 근처 → 일시적 흔들림일 가능성"""
        j = self.cfg.get("stop_judge", {})
        if last > p.sl:
            if p.extra.pop("soft_since", None) is not None:
                p.extra.pop("soft_logged", None)
                self.log(f"↩️ {p.name} 손절 판단선 {money(self.m, p.sl)} 위로 회복 — 계속 보유")
            return None
        hard = self.hard_level(p)
        if not j.get("enabled") or hard >= p.sl:
            return "sell", ("추적 손절선 도달" if p.sl >= p.entry else "손절선 도달")
        if last <= hard:
            return "sell", f"최후 손절선 {money(self.m, hard)} 도달"
        info = {}
        if self.judge:
            try:
                info = self.judge(p.symbol) or {}
            except Exception:
                info = {}
        why = []
        if (info.get("vol_x") or 0) >= j.get("heavy_vol_x", 1.5):
            why.append(f"파는 거래량이 평소의 {info['vol_x']:.1f}배")
        vw = info.get("vwap")
        if vw and last < vw * (1 - j.get("below_vwap_pct", 1.0) / 100):
            why.append("오늘 평균 단가보다 1% 넘게 아래")
        mk = info.get("market")
        if mk is not None and mk <= j.get("market_weak_pct", -0.5):
            why.append(f"시장 전체가 약함(최근 15분 평균 {mk:+.1f}%)")
        if info.get("bad"):
            why.append(info["bad"])
        since = p.extra.setdefault("soft_since", time.time())
        if why:
            return "sell", "손절선 아래 + " + ", ".join(why)
        g = j.get("grace_min", 10)
        if time.time() - since > g * 60:
            return "sell", f"손절선 아래에서 {g}분 동안 회복 못 함"
        msg = (f"⏳ {p.name} 손절 판단선 {money(self.m, p.sl)} 아래지만 거래량 적고 시장 괜찮아서 최대 {g}분 지켜봐요 "
               f"(최후 손절선 {money(self.m, hard)} 아래로 가면 토스 서버가 바로 팔아요)")
        if not p.extra.get("soft_logged"):
            p.extra["soft_logged"] = 1
            self.log(msg)
            self.notify(msg)
        return "wait", msg

    # ───── 이익 난 종목 다음 날까지 들고 가기 ─────
    def carry_check(self, p: Position, last: float | None, vw: float | None) -> str | None:
        """들고 가도 되면 None, 안 되면 이유."""
        c = self.cfg.get("carry", {})
        if not c.get("enabled"):
            return "들고 가기 꺼짐"
        if p.extra.get("etf"):
            return "ETF·레버리지는 하루 안에 정리 (밤사이 갭·녹아내림 위험)"
        if p.state != "open" or last is None:
            return "보유 상태 아님"
        sw = self.cfg.get("swing", {}) if p.extra.get("swing") else {}
        prof = (last / p.entry - 1) * 100
        need = sw.get("min_profit_pct", c["min_profit_pct"])
        slack = sw.get("vwap_slack_pct", 0)
        if prof < need:
            # 손절선이 아직 살아 있고(1% 넘게 위), 손실이 작고, 오늘 평균 단가 근처 이상이면 → 손절선 그대로 들고 감
            intact = (c.get("hold_if_intact", True) and p.sl and last >= p.sl * 1.01
                      and prof >= -c.get("hold_max_loss_pct", 2.0) and (not vw or last >= vw * 0.99)
                      and prof >= c.get("hold_min_profit_pct", -99))   # 손실 중인 종목은 밤을 넘기지 않음(갭하락 위험)
            if not intact:
                return (f"수익 {prof:+.1f}% < 기준 {need}% — 손실 중이거나 평균 단가 아래라 밤사이 갭하락 위험을 피해요"
                        if c.get("hold_if_intact", True) else f"수익 {prof:+.1f}% < 기준 {need}%")
            slack = max(slack, 1.0)
        if c.get("require_above_vwap") and vw and last < vw * (1 - slack / 100):
            return "오늘 평균 단가(VWAP) 아래로 밀림"
        days = len(set(p.extra.get("days_held", []) + [self.today().isoformat()]))
        maxd = sw.get("max_days", c["max_days"])
        if days > maxd:
            return f"최대 {maxd}일 보유 끝"
        if not p.extra.get("carry") and len(self.carried()) >= sw.get("max_positions", c["max_positions"]):
            return "들고 가는 종목 수 한도"
        return None

    def carry_stop(self, p: Position, last: float) -> float:
        """밤사이 손절선: 지금 손절선, 본전+α, '지금가 −carry_stop_pct%' 중 가장 높은 값 (올리기만)."""
        c = self.cfg["carry"]
        if p.extra.get("swing"):
            sw = self.cfg.get("swing", {})
            prof = (last / p.entry - 1) * 100
            # 스윙: 숨 쉴 공간을 넓게(지금가 −7% 또는 일봉 지지선 아래), 수익이 2% 넘으면 본전은 지킴
            room = round_tick(self.m, last * (1 - sw.get("carry_stop_pct", 7.0) / 100), "down")
            sup = p.extra.get("support")
            if sup and sup < last:
                room = max(room, round_tick(self.m, sup * 0.985, "down"))
            lock = round_tick(self.m, p.entry * 1.002, "down") if prof >= sw.get("lock_after_pct", 2.0) else p.sl
            return max(p.sl, lock, min(room, round_tick(self.m, last, "down")))
        lock = round_tick(self.m, p.entry * (1 + c["lock_profit_pct"] / 100), "down")
        trail = round_tick(self.m, last * (1 - c["carry_stop_pct"] / 100), "down")
        if last < lock * 1.005:
            # 이익이 아직 작음 → '본전+1%' 를 강제하면 바로 팔리니, 원래 손절선을 쓰되 밤사이엔 '산 값 −1%' 보다 낮지 않게
            tight = round_tick(self.m, p.entry * (1 - c.get("small_carry_stop_pct", 1.0) / 100), "down")
            return max(p.sl, min(tight, round_tick(self.m, last * 0.995, "down")))
        return max(p.sl, lock, min(trail, round_tick(self.m, last, "down")))

    # ───── 추가 매수(불타기) ─────
    def can_add(self, p: Position, last: float) -> bool:
        """이미 본전 이상 손절선이 걸려 있고(잃지 않는 상태), 1R 이상 이익일 때만."""
        a = self.cfg.get("pyramid", {})
        if not a.get("enabled") or p.state != "open" or p.extra.get("add_oid") or p.extra.get("etf"):
            return False
        if p.extra.get("adds", 0) >= a.get("max_adds", 2):
            return False
        if time.time() - p.extra.get("add_last", 0) < a.get("gap_min", 15) * 60:
            return False                                   # 추가 매수 사이엔 최소 15분 (같은 신호로 연달아 사지 않게)
        R = p.extra.get("R") or max(p.entry - p.sl, p.entry * 0.01)
        return p.sl >= p.entry and last >= p.entry + R * a.get("min_R", 1.0)

    def add_qty(self, p: Position, price: float, max_loss: float, budget: float) -> int:
        """추가할 수량: 처음 수량의 add_ratio 배까지, 남은 돈 안에서,
        추가 후 평균 단가가 손절선보다 높아지면 '손절 시 손실'이 max_loss 이하가 되도록."""
        a = self.cfg.get("pyramid", {})
        base = p.extra.get("q0") or p.qty
        q = min(int(base * a.get("add_ratio", 0.5)), int(budget // price))
        while q > 0:
            avg = (p.entry * p.qty + price * q) / (p.qty + q)
            if (avg - p.sl) * (p.qty + q) <= max_loss:
                break
            q -= 1
        return max(q, 0)

    def _added(self, p: Position, q: int, price: float, why: str):
        old_q = p.qty
        p.entry = (p.entry * old_q + price * q) / (old_q + q)
        p.qty = old_q + q
        p.extra["adds"] = p.extra.get("adds", 0) + 1
        p.extra["add_last"] = time.time()
        msg = (f"➕ 추가 매수 {p.name} {q}주 @ {money(self.m, price)} → 총 {p.qty}주 · 평균 {money(self.m, p.entry)} · "
               f"손절선 {money(self.m, p.sl)} 그대로 · {why}")
        self.log(msg)
        self.notify(msg)
        self._journal("add", p)
        self.save_state()

    def mark_carry(self, p: Position, last: float):
        d = self.today().isoformat()
        hist = p.extra.get("days_held", [])
        if d not in hist:
            hist.append(d)
        p.extra["days_held"] = hist
        p.extra["carry"] = True
        msg = (f"🌙 들고 감 {p.name} {p.qty}주 · 지금 {(last / p.entry - 1) * 100:+.1f}% · "
               f"밤사이 손절선 {money(self.m, p.sl)} ({(p.sl / p.entry - 1) * 100:+.1f}%, 이 아래로 가면 다음 장에 팔아요)")
        self.log(msg)
        self.notify(msg)
        self._journal("carry", p)

    def restore(self):
        """다시 켰을 때: 오늘 열려 있던 것 + 전날 들고 온 것을 이어받는다."""
        fpath = self.dir / "state.json"
        if not fpath.exists():
            return
        try:
            st = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            return
        same_day = st.get("date") == self.today().isoformat()
        for d in st.get("positions", []):
            try:
                p = Position(**d)
            except TypeError:
                continue
            if p.state not in ("pending", "open", "closing"):
                continue
            if not same_day and not p.extra.get("carry"):
                continue
            if not same_day and p.state != "open":
                continue
            p.last_check = 0
            self.pos[p.symbol] = p
            tag = "전날부터 보유" if p.extra.get("carry") else "이어받음"
            self.log(f"{tag}: {p.name} {p.qty}주 @ {money(self.m, p.entry)} · 손절선 {money(self.m, p.sl)}")

    def _log_raise(self, p: Position, new_sl: float, why: str, tag: str):
        """손절선 올림 기록 — 너무 자주 남기지 않게 0.5% 이상 올랐을 때나 처음 한 번만."""
        last = p.extra.get("logged_sl", 0)
        if not last or new_sl >= last * 1.005 or "본전" in why:
            self.log(f"{tag} {p.name} 손절선 ↑ {money(self.m, new_sl)} ({why}) · 지금 팔려도 {(new_sl / p.entry - 1) * 100:+.1f}%")
            p.extra["logged_sl"] = new_sl

    def record_partial(self, p: Position, qty: int, price: float, label: str = "나눠 익절"):
        part = Position(p.symbol, p.name, qty, p.entry, p.tp, p.sl, p.reason, p.opened_at, state="closed",
                        extra={k: p.extra[k] for k in ("setup", "grade") if k in p.extra})
        part.exit_price, part.exit_reason = price, label
        part.pnl = self.calc_pnl(part, price)
        self.closed.append(part)
        msg = (f"🟢 {label} {p.name} {qty}주 {money(self.m, p.entry)} → {money(self.m, price)} "
               f"({(price / p.entry - 1) * 100:+.2f}%) 손익 {money(self.m, part.pnl)} · 남은 {p.qty}주는 계속 보유(손절선 {money(self.m, p.sl)})")
        self.log(msg)
        self.notify(msg)
        self._journal("partial", part)
        if self.on_closed:
            self.on_closed(part)


class PaperBroker(BaseBroker):
    """실제 주문 없이 실시간 가격으로 체결을 흉내 낸다 (살 때·팔 때 1호가씩 불리하게)."""

    def enter(self, sym, name, price, qty, tp, sl, reason, r=None, atr=None) -> Position | None:
        fill = add_ticks(self.m, price, 1)
        p = Position(sym, name, qty, fill, tp, sl, reason, dt.datetime.now().isoformat(timespec="seconds"), state="open",
                     peak=fill, extra={"R": r, "atr": atr})
        self.pos[sym] = p
        msg = (f"🛒 [연습] 매수 {name}({sym}) {qty}주 @ {money(self.m, fill)} · 손절 {money(self.m, sl)} "
               f"({(sl / fill - 1) * 100:.1f}%) · 익절은 추적 손절(최대 {money(self.m, tp)}) · {reason}")
        self.log(msg)
        self.notify(msg)
        self._journal("entry", p)
        return p

    def on_price(self, sym, last, now):
        p = self.pos.get(sym)
        if not p or p.state != "open":
            return
        if last >= p.tp:
            self._close(p, p.tp, "익절가 도달")
            return
        d = self.judge_stop(p, last)
        if d and d[0] == "sell":
            hard = self.hard_level(p)
            px = min(hard, last) if last <= hard else last
            self._close(p, add_ticks(self.m, px, -1), d[1])
        elif d is None:
            new_sl, part, why = self.manage(p, last)
            if part:
                q = self.part_qty(p, part[0])
                p.qty -= q
                p.partial_done = True
                p.extra["parts"] = int(p.extra.get("parts", 0)) + 1
                if new_sl:
                    p.sl = new_sl
                self.record_partial(p, q, add_ticks(self.m, last, -1), part[1])
            if new_sl:
                self._log_raise(p, new_sl, why, "[연습]")
                p.sl = new_sl
                self.save_state()

    def poll(self, now, last_prices):
        pass

    def add(self, p: Position, q: int, price: float, why: str):
        self._added(p, q, add_ticks(self.m, price, 1), "[연습] " + why)

    def carry(self, p: Position, last: float):
        p.tp = max(p.tp, round_tick(self.m, p.entry * (1 + self.cfg["carry"]["max_gain_pct"] / 100), "up"))
        p.sl = self.carry_stop(p, last)
        self.mark_carry(p, last)

    def hold(self, p: Position):
        """종료 버튼(보유 유지): 손절선은 그대로 두고 다음에 켜면 이어받게 표시."""
        p.extra["carry"] = True
        d = self.today().isoformat()
        if d not in p.extra.setdefault("days_held", []):
            p.extra["days_held"].append(d)
        self.save_state()

    def flatten(self, sym, why, last):
        p = self.pos.get(sym)
        if p and p.state == "open":
            self._close(p, add_ticks(self.m, last, -1) if last else p.entry, why)


class LiveBroker(BaseBroker):
    """토스증권 실전 주문."""

    def __init__(self, api, market, cfg, journal_dir, log, notify):
        super().__init__(market, cfg, journal_dir, log, notify)
        self.api = api
        self.tz_today = None

    def _expire(self, p: Position | None = None) -> str:
        if p is not None and p.extra.get("expire"):
            return p.extra["expire"]
        if self.m == "US":
            # 토스는 만료일을 '한국 날짜'로 본다. 미장은 한국 밤~새벽에 열려서 뉴욕 날짜를 넣으면
            # 한국 자정 이후엔 '지난 날짜'로 거절된다 → 한국 날짜 + 1일(장 끝날 때까지 넉넉히)
            return (dt.datetime.now(TZ["KR"]).date() + dt.timedelta(days=1)).isoformat()
        return self.today().isoformat()

    def carry(self, p: Position, last: float):
        p.tp = max(p.tp, round_tick(self.m, p.entry * (1 + self.cfg["carry"]["max_gain_pct"] / 100), "up"))
        new_sl = self.carry_stop(p, last)
        base = dt.datetime.now(TZ["KR"]).date() if self.m == "US" else self.today()
        exp = (base + dt.timedelta(days=int(self.cfg["carry"]["max_days"] * 1.6) + 4)).isoformat()
        p.extra["expire"] = exp
        try:
            self._modify(p, p.qty, new_sl)
        except TossError as e:
            self.log(f"[실전] 들고 가기 설정 실패 {p.name}: {e} → 오늘 팔아요")
            p.extra.pop("expire", None)
            self._market_sell(p, "들고 가기 설정 실패")
            return
        p.sl = new_sl
        self.mark_carry(p, last)

    def hold(self, p: Position):
        """종료 버튼(보유 유지): 손절선은 그대로, 시장가 손절 조건주문의 만료일만 늘려서 다음에 켤 때까지 지킨다."""
        p.extra["expire"] = (self.today() + dt.timedelta(days=int(self.cfg["carry"]["max_days"] * 1.6) + 3)).isoformat()
        try:
            self._modify(p, p.qty, p.sl)
        except TossError as e:
            self.log(f"[실전] {p.name} 손절 조건주문 연장 실패: {e}")
        p.extra["carry"] = True
        d = self.today().isoformat()
        if d not in p.extra.setdefault("days_held", []):
            p.extra["days_held"].append(d)
        self.log(f"[실전] {p.name} {p.qty}주 보유 유지 · 시장가 손절 {money(self.m, p.sl)} (토스 서버 감시, ~{p.extra['expire']})")
        self.save_state()

    def verify_carried(self):
        """이어받은 종목(전날 것 + 오늘 껐다 켠 것)이 실제로 계좌에 있는지, 손절 조건주문이 살아 있는지 확인."""
        opened = [p for p in self.pos.values() if p.state == "open"]
        if not opened:
            return
        try:
            hold = {h["symbol"]: int(f(h.get("quantity"), 0) or 0) for h in (self.api.holdings() or {}).get("items", [])}
        except TossError as e:
            self.log(f"[실전] 보유 확인 실패: {e}")
            return
        for p in opened:
            q = hold.get(p.symbol, 0)
            if q <= 0:
                self.log(f"[실전] {p.name} 은 계좌에 없어요(직접 팔았거나 밤사이 정리) → 관리 목록에서 뺌")
                self.pos.pop(p.symbol, None)
                continue
            if q < p.qty:
                p.qty = q
            try:
                c = self.api.get_conditional(p.cond_id) if p.cond_id else {}
                if c.get("status") not in ("WATCHING", "PAUSED"):
                    raise TossError(0, "cond", "조건주문이 살아 있지 않음")
            except TossError:
                self.log(f"[실전] {p.name} 손절 조건주문이 없어서 다시 걸어요")
                p.cond_id = None
                self._arm_exit(p)
        self.save_state()

    def enter(self, sym, name, price, qty, tp, sl, reason, r=None, atr=None) -> Position | None:
        r_risk = r
        # 매도 1호가(없으면 현재가+1호가)에 지정가 → 사실상 즉시 체결, 너무 비싸게는 안 삼
        limit = add_ticks(self.m, price, self.cfg["live"]["buy_slip_ticks"])
        try:
            ob = self.api.orderbook(sym)
            ask = f((ob.get("asks") or [{}])[0].get("price"))
            bid = f((ob.get("bids") or [{}])[0].get("price"))
            if ask and bid:
                spread = (ask - bid) / bid * 100
                if spread > self.cfg["strategy"]["max_spread_pct"]:
                    self.log(f"[실전] {name} 호가 간격 {spread:.2f}% 가 넓어 건너뜀")
                    return None
                limit = min(limit, ask) if ask >= price else ask
        except TossError as e:
            self.log(f"[실전] 호가 조회 실패 {sym}: {e}")
        cid = "buy-" + uuid.uuid4().hex[:24]
        market = self.cfg["live"].get("entry_order", "MARKET").upper() == "MARKET"
        try:
            if market:
                r = self.api.create_order(sym, "BUY", qty, "MARKET", None, cid)
            else:
                r = self.api.create_order(sym, "BUY", qty, "LIMIT", price_str(self.m, limit), cid)
        except TossError as e:
            self.log(f"[실전] 매수 주문 실패 {name}: {e}")
            self.notify(f"⚠️ 매수 주문 실패 {name}: {e.code} {e.message}")
            if not hasattr(self, "rejected"):
                self.rejected = {}
            if e.code != "insufficient-buying-power":
                self.rejected[sym] = e.code                  # 같은 종목을 오늘 계속 시도하지 않게
            if e.code == "insufficient-buying-power" and self.m == "US":
                self.notify("💵 달러가 부족해요. 토스 앱에서 환전하거나 '원화로 바로 주문'(자동 환전)을 켜 주세요.")
            return None
        p = Position(sym, name, qty, limit, tp, sl, reason, dt.datetime.now().isoformat(timespec="seconds"),
                     state="pending", buy_order_id=r["orderId"], buy_sent_at=time.time(),
                     extra={"R": r_risk, "atr": atr})
        self.pos[sym] = p
        self.log(f"🛒 [실전] 매수 주문 {name}({sym}) {qty}주 " + ("시장가" if market else f"지정가 {money(self.m, limit)}") + f" · {reason}")
        self._journal("buy_sent", p)
        return p

    # 웹소켓 주문 이벤트 → 빨리 반영 (폴링으로도 다시 확인)
    def on_order_event(self, data):
        o = (data or {}).get("order") or {}
        sym = o.get("symbol")
        p = self.pos.get(sym)
        if p:
            p.last_check = 0  # 다음 poll 에서 바로 확인

    def _arm_exit(self, p: Position):
        try:
            # 손절은 '시장가' 조건주문: 손절선에 닿으면 토스 서버가 바로 시장가로 판다 (갭하락·PC 꺼짐에도 작동)
            hard = self.hard_level(p)
            r = self.api.create_stop_market(p.symbol, p.qty, price_str(self.m, hard), self._expire(p))
            p.cond_id = r["conditionalOrderId"]
            p.state = "open"
            p.peak = p.peak or p.entry
            both = (f"손절 판단선 {money(self.m, p.sl)}(로봇이 거래량·시장 보고 판단) · 최후 손절 {money(self.m, hard)}(토스 서버)"
                    if hard < p.sl else f"손절 {money(self.m, p.sl)}(토스 서버)")
            self.log(f"[실전] {p.name} {both} 시장가 조건주문 등록 · 익절은 나눠 팔기+추적 손절")
            self.notify(f"✅ 체결 {p.name} {p.qty}주 @ {money(self.m, p.entry)} · {both} · 익절은 나눠 팔기+추적 손절")
        except TossError as e:
            # 조건주문을 못 걸면 위험하니 바로 판다
            self.log(f"[실전] 조건주문 실패 {p.name}: {e} → 즉시 매도")
            self.notify(f"⚠️ {p.name} 손절 조건주문 실패({e.code}) → 바로 팔아요")
            p.state = "open"
            self._market_sell(p, "조건주문 실패로 즉시 정리")
        self._journal("armed", p)

    def _market_sell(self, p: Position, why: str):
        if p.cond_id:
            try:
                self.api.cancel_conditional(p.cond_id)
            except TossError as e:
                if e.code not in ("conditional-order-not-found",):
                    self.log(f"[실전] 조건주문 취소 실패 {p.name}: {e}")
            p.cond_id = None
        try:
            q = int(f((self.api.sellable(p.symbol) or {}).get("sellableQuantity"), p.qty) or 0)
        except TossError:
            q = p.qty
        if q <= 0:
            self._close(p, p.entry, why + " (팔 수량 없음 — 이미 팔렸을 수 있어요)")
            return
        try:
            r = self.api.create_order(p.symbol, "SELL", min(q, p.qty), "MARKET", None, "sell-" + uuid.uuid4().hex[:24])
            p.sell_order_id, p.state, p.exit_reason = r["orderId"], "closing", why
            p.last_check = 0
            self.log(f"[실전] {p.name} 시장가 매도 주문 · {why}")
        except TossError as e:
            self.log(f"[실전] 매도 실패 {p.name}: {e}")
            self.notify(f"🚨 매도 실패 {p.name}: {e.code} {e.message} — 토스 앱에서 직접 확인하세요")
        self.save_state()

    def on_price(self, sym, last, now):
        p = self.pos.get(sym)
        if not p or p.state != "open" or not p.cond_id:
            return
        # 하루 급등 최대치 도달 → 바로 팔기
        if last >= p.tp:
            self._market_sell(p, "최대 익절가 도달")
            return
        # 예비 장치: 서버 손절선(최후 손절선) 아래에서 20초 넘게 안 팔렸으면(서버 조건주문 지연 등) 직접 시장가 정리
        gap = add_ticks(self.m, self.hard_level(p), -3)
        if last < gap and time.time() - p.extra.get("below_since", time.time()) > 20:
            self._market_sell(p, "손절선 아래 — 직접 시장가 정리")
            return
        if last < gap:
            p.extra.setdefault("below_since", time.time())
        else:
            p.extra.pop("below_since", None)
        # 손절 판단선 아래: 진짜 빠지는 것 같으면 바로 팔고, 살짝 밀린 거면 잠깐 지켜봄
        # (이익 구간 손절선·최후 손절선은 토스 서버가 판다 → 로봇이 같이 팔지 않음)
        hard = self.hard_level(p)
        if last <= hard:
            return
        if hard < p.sl:
            d = self.judge_stop(p, last)
            if d:
                if d[0] == "sell":
                    self._market_sell(p, d[1])
                return
        new_sl, part, why = self.manage(p, last)
        if part and not p.extra.get("partial_oid"):
            q = self.part_qty(p, part[0])
            remain = p.qty - q
            try:
                # 조건주문 수량을 먼저 줄여야 팔 수량이 남는다
                self._modify(p, remain, new_sl or p.sl)
                r = self.api.create_order(p.symbol, "SELL", q, "MARKET", None, "part-" + uuid.uuid4().hex[:24])
                p.partial_done = True
                p.extra["parts"] = int(p.extra.get("parts", 0)) + 1
                p.extra.update(partial_oid=r["orderId"], partial_q=q, partial_label=part[1])
                p.qty = remain
                if new_sl:
                    p.sl = new_sl
                self.log(f"[실전] {p.name} {part[1]} {q}주 시장가 주문")
                self.save_state()
                return
            except TossError as e:
                self.log(f"[실전] {part[1]} 실패 {p.name}: {e}")
        if new_sl and time.time() - p.extra.get("mod_t", 0) > self.cfg["live"].get("trail_update_sec", 15):
            p.extra["mod_t"] = time.time()
            try:
                self._modify(p, p.qty, new_sl)
                self._log_raise(p, new_sl, why, "[실전]")
                p.sl = new_sl
                self.save_state()
            except TossError as e:
                self.log(f"[실전] 손절선 올리기 실패 {p.name}: {e}")

    def add(self, p: Position, q: int, price: float, why: str):
        try:
            r = self.api.create_order(p.symbol, "BUY", q, "MARKET", None, "add-" + uuid.uuid4().hex[:24])
            p.extra.update(add_oid=r["orderId"], add_q=q, add_why=why, add_t=time.time(), add_last=time.time())
            p.last_check = 0
            self.log(f"[실전] {p.name} 추가 매수 주문 {q}주 (시장가) · {why}")
            self.save_state()
        except TossError as e:
            self.log(f"[실전] 추가 매수 실패 {p.name}: {e}")

    def _poll_add(self, p: Position):
        oid = p.extra.get("add_oid")
        if not oid:
            return
        o = self.api.get_order(oid)
        ex = o.get("execution") or {}
        filled = int(f(ex.get("filledQuantity"), 0) or 0)
        st = o.get("status")
        if st == "FILLED" or (st in ("CANCELED", "REJECTED") or time.time() - p.extra.get("add_t", 0) > 30):
            if st not in ("FILLED", "CANCELED", "REJECTED"):
                try:
                    self.api.cancel_order(oid)
                except TossError:
                    pass
            why = p.extra.get("add_why", "")
            for k in ("add_oid", "add_q", "add_why", "add_t"):
                p.extra.pop(k, None)
            if filled > 0:
                self._added(p, filled, f(ex.get("averageFilledPrice"), p.entry), why)
                try:
                    self._modify(p, p.qty, p.sl)       # 손절 주문 수량을 늘어난 만큼 맞춤
                except TossError as e:
                    self.log(f"[실전] 추가 매수 뒤 손절 수량 맞추기 실패 {p.name}: {e}")
            else:
                self.log(f"[실전] {p.name} 추가 매수 {st or '시간 초과'} — 체결 없음")
            self.save_state()

    def _modify(self, p: Position, qty: int, sl: float):
        old = p.cond_id
        try:
            p.cond_id, how = self.api.replace_stop_market(old, p.symbol, qty, price_str(self.m, self.hard_level(p, sl)),
                                                          self._expire(p))
            if how != "정정":
                self.log(f"[실전] {p.name} 손절 주문 교체: {how}")
            self.save_state()
        except TossError:
            # 새 손절을 못 걸었는데 옛 손절도 없어졌으면 → 손절 없는 보유는 금지, 바로 판다
            if not self.api.is_alive(old):
                p.cond_id = None
                self.notify(f"⚠️ {p.name} 손절 주문이 사라져서 바로 팔아요")
                self._market_sell(p, "손절 주문 교체 실패로 즉시 정리")
            raise

    def poll(self, now, last_prices):
        t = time.time()
        for p in list(self.pos.values()):
            if t - p.last_check < self.cfg["live"]["poll_sec"]:
                continue
            p.last_check = t
            try:
                if p.state == "pending":
                    o = self.api.get_order(p.buy_order_id)
                    ex = o.get("execution") or {}
                    filled = int(f(ex.get("filledQuantity"), 0) or 0)
                    st = o.get("status")
                    if st == "FILLED" or (filled > 0 and t - p.buy_sent_at > self.cfg["live"]["buy_timeout_sec"]):
                        if st != "FILLED":
                            try:
                                self.api.cancel_order(p.buy_order_id)
                            except TossError:
                                pass
                        p.qty = filled
                        p.entry = f(ex.get("averageFilledPrice"), p.entry)
                        self._arm_exit(p)
                    elif st in ("CANCELED", "REJECTED") and filled == 0:
                        self.log(f"[실전] {p.name} 매수 주문 {st}")
                        self.pos.pop(p.symbol, None)
                        self.save_state()
                    elif t - p.buy_sent_at > self.cfg["live"]["buy_timeout_sec"] and filled == 0:
                        try:
                            self.api.cancel_order(p.buy_order_id)
                        except TossError:
                            pass
                        self.log(f"[실전] {p.name} {self.cfg['live']['buy_timeout_sec']}초 안에 안 사져서 취소")
                        self.pos.pop(p.symbol, None)
                        self.save_state()
                elif p.state == "open" and p.cond_id:
                    self._poll_add(p)
                    if p.extra.get("partial_oid"):
                        o = self.api.get_order(p.extra["partial_oid"])
                        if o.get("status") == "FILLED":
                            ex = o.get("execution") or {}
                            px = f(ex.get("averageFilledPrice"), last_prices.get(p.symbol, p.entry))
                            self.record_partial(p, int(p.extra.pop("partial_q")), px, p.extra.pop("partial_label", "나눠 익절"))
                            p.extra.pop("partial_oid", None)
                    c = self.api.get_conditional(p.cond_id)
                    if c.get("status") in ("COMPLETED", "ORDERED", "EXPIRED"):
                        legs = [c.get("first") or {}, c.get("second") or {}]
                        for i, leg in enumerate(legs):
                            oid = leg.get("triggeredOrderId")
                            if not oid:
                                continue
                            o = self.api.get_order(oid)
                            ex = o.get("execution") or {}
                            if o.get("status") == "FILLED":
                                px = f(ex.get("averageFilledPrice"), p.sl)
                                hard = self.hard_level(p)
                                self._close(p, px, "추적 손절선에서 매도(이익 확정)" if self.calc_pnl(p, px) >= 0
                                            else "본전 지키기 선에서 매도" if p.sl >= p.entry
                                            else "최후 손절선 체결" if hard < p.sl else "손절 체결")
                                break
                        else:
                            if c.get("status") == "EXPIRED":
                                self._market_sell(p, "조건주문 만료")
                elif p.state == "closing" and p.sell_order_id:
                    o = self.api.get_order(p.sell_order_id)
                    if o.get("status") == "FILLED":
                        ex = o.get("execution") or {}
                        self._close(p, f(ex.get("averageFilledPrice"), last_prices.get(p.symbol, p.entry)), p.exit_reason)
            except TossError as e:
                self.log(f"[실전] 상태 확인 실패 {p.name}: {e}")

    def flatten(self, sym, why, last):
        p = self.pos.get(sym)
        if not p:
            return
        if p.state == "pending":
            try:
                self.api.cancel_order(p.buy_order_id)
            except TossError:
                pass
            p.last_check = 0
            return
        if p.state == "open":
            self._market_sell(p, why)
