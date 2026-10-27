"""안전장치 + 자금 나누기.

'오늘 투자금'(daily_capital) 을 정하면 그 안에서만 사요.
  - 한 종목에 넣는 돈: 투자금의 max_position_pct% 까지, 신호가 셀수록 조금 더 (0.85~1.25배)
  - 손절되면 잃는 돈: 투자금의 risk_per_trade_pct% 이하가 되게 수량 조절
  - 팔면 그 돈이 다시 '쓸 수 있는 돈'으로 돌아와 다른 종목에 써요 (이익은 다시 넣지 않고, 손실만큼은 줄어요)
  - 하루 손실 한도: 투자금의 daily_loss_pct%
daily_capital 이 없으면 예전 방식(budget_per_trade 고정)으로 동작.
"""
from __future__ import annotations

from pathlib import Path


class Risk:
    def __init__(self, cfg: dict, stop_file: Path):
        self.c = cfg["risk"][cfg["market"]]
        self.stop_file = stop_file
        self.capital: float | None = self.c.get("daily_capital")
        self.realized = 0.0      # 오늘 확정 손익 (수수료·세금 포함)
        self.trades = 0          # 오늘 진입 횟수
        self.losses_in_row = 0
        self.halted: str | None = None
        self.peak = 0.0          # 오늘 확정 손익의 최고점 (번 돈 지키기)

    # ───────── 한도 숫자 ─────────
    @property
    def loss_limit(self) -> float:
        if self.capital:
            return self.capital * self.c.get("daily_loss_pct", 3.0) / 100
        return abs(self.c["daily_loss_limit"])

    def available(self, invested: float) -> float | None:
        """지금 새로 쓸 수 있는 돈 = 투자금 − 들고 있는 종목에 들어간 돈 − 오늘 손실."""
        if not self.capital:
            return None
        return max(0.0, self.capital + min(self.realized, 0.0) - invested)

    def kill_switch(self) -> bool:
        return self.stop_file.exists()

    def can_enter(self, open_positions: int, invested: float = 0.0) -> str | None:
        """진입 불가 이유를 돌려준다. 가능하면 None."""
        if self.kill_switch():
            return "긴급 정지 파일(STOP)이 있어요"
        if self.halted:
            return self.halted
        if self.realized <= -self.loss_limit:
            self.halted = f"오늘 손실 한도({self.loss_limit:,.0f})에 닿아 오늘은 매매를 멈춰요"
            return self.halted
        if self.losses_in_row >= self.c["max_losses_in_row"]:
            self.halted = f"연속 {self.losses_in_row}번 손절이라 오늘은 매매를 멈춰요"
            return self.halted
        pg = self.profit_guard()
        if pg:
            self.halted = pg
            return self.halted
        if self.trades >= self.c["max_trades_per_day"]:
            return "오늘 매매 횟수를 다 썼어요"
        if open_positions >= self.c["max_positions"]:
            return "동시 보유 한도"
        av = self.available(invested)
        if av is not None and av < self.capital * self.c.get("min_position_pct", 8) / 100:
            return "투자금을 다 쓰고 있어요 (팔리면 다시 사요)"
        return None

    # ───────── 번 돈 지키기 ─────────
    def _protect_amt(self) -> float:
        if not self.capital:
            return 0.0
        return self.capital * self.c.get("protect_after_pct", 2.0) / 100

    def size_mult(self) -> float:
        """오늘 이미 투자금의 2% 넘게 벌었으면 새 매매는 절반 크기로 (번 돈을 크게 걸지 않게)."""
        a = self._protect_amt()
        return self.c.get("protect_size", 0.5) if a and self.realized >= a else 1.0

    def profit_guard(self) -> str | None:
        """오늘 번 돈이 최고점의 절반 아래로 줄면 새 매수 멈춤 (이미 번 날을 손실의 날로 만들지 않게)."""
        a = self._protect_amt()
        if a and self.peak >= a and self.realized < self.peak * (1 - self.c.get("protect_giveback_pct", 50) / 100):
            return f"오늘 번 돈 지키기: 최고 {self.peak:,.2f} → 지금 {self.realized:,.2f} (절반 넘게 반납) 이라 오늘 새 매수는 멈춰요"
        return None

    def strength(self, sig: dict | None) -> float:
        """확신도 → 비중 배수. 신호 점수(매수 방식·거래량·차트 '관심'·데스크 1순위/픽·실적 서프라이즈)로 등급을 매긴다.
        A+ 1.7배 · A 1.35배 · B 1.0배 · C 0.75배. 등급은 sig['grade'] 에 적어 알림에 보여 준다."""
        sig = sig or {}
        sc = float(sig.get("score") or 0)
        if not sc:                                   # 점수가 없으면(예전 방식) 거래량으로만
            vx = float(sig.get("vol_x") or 4.0)
            return max(0.85, min(1.25, 0.85 + 0.075 * (vx - 2)))
        tiers = self.c.get("conviction") or [[5.2, 1.7, "A+"], [4.4, 1.35, "A"], [3.6, 1.0, "B"], [0, 0.75, "C"]]
        for cut, w, g in tiers:
            if sc >= cut:
                sig["grade"], sig["weight"] = g, w
                return float(w)
        return 0.75

    def qty_for(self, price: float, stop_exec: float, cash: float | None, cost_rate: float = 0.0025,
                invested: float = 0.0, w: float = 1.0) -> int:
        """금액 한도와 '손절 시 손실 한도'를 둘 다 지키는 수량.
        stop_exec 는 손절 주문이 실제로 나갈 가격, 수수료·세금까지 손실에 넣는다."""
        w = w * self.size_mult()
        if self.capital:
            pos_pct = min(self.c.get("max_position_pct", 40) * w, self.c.get("max_position_pct_cap", 65))
            risk_pct = min(self.c.get("risk_per_trade_pct", 1.0) * w, self.c.get("risk_per_trade_pct_cap", 1.8))
            budget = min(self.available(invested), self.capital * pos_pct / 100)
            max_loss = self.capital * risk_pct / 100
            min_amt = self.capital * self.c.get("min_position_pct", 8) / 100
        else:
            budget, max_loss, min_amt = self.c["budget_per_trade"], self.c["max_loss_per_trade"], 0
        if cash is not None:
            budget = min(budget, cash * 0.95)
        q1 = int(budget // price)
        per = max(price - stop_exec, price * 0.002) + price * cost_rate
        q2 = int(max_loss // per)
        q = max(0, min(q1, q2))
        if q and q * price < min_amt and q1 > q:   # 너무 조금만 사지는 경우(손절폭이 아주 넓음)는 건너뜀
            return 0
        if q < self.c.get("min_qty", 1):            # 1주만 살 수 있으면 안 삼 (나눠 익절이 안 되고 수익도 작음)
            return 0
        return q

    def record_exit(self, pnl: float):
        self.realized += pnl
        self.peak = max(self.peak, self.realized)
        self.losses_in_row = self.losses_in_row + 1 if pnl < 0 else 0
