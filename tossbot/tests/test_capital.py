"""투자금 나누기 시험: 50만 원 안에서만 사고, 팔면 다시 쓰고, 손실만큼 줄어드는지."""
from pathlib import Path

from bot.risk import Risk

cfg = {"market": "KR", "risk": {"KR": {"daily_capital": 500000, "max_position_pct": 40, "min_position_pct": 8,
                                        "risk_per_trade_pct": 1.0, "daily_loss_pct": 3.0, "max_positions": 4,
                                        "max_trades_per_day": 10, "max_losses_in_row": 3}}}
r = Risk(cfg, Path("/nonexistent/STOP"))
cost = 0.0023
inv = 0.0
buys = []
# 손절폭 3% 종목을 계속 사 보기
for i, (price, w) in enumerate([(10000, 1.0), (52000, 1.25), (7300, 0.85), (21000, 1.0), (15000, 1.0)]):
    why = r.can_enter(len(buys), inv)
    if why:
        print(f"{i+1}번째: 안 삼 → {why}")
        break
    q = r.qty_for(price, price * 0.97, None, cost, inv, w)
    amt = q * price
    loss = q * (price * 0.03 + price * cost)
    print(f"{i+1}번째: {price:,}원 × {q}주 = {amt:,}원 (세기 {w}) · 손절 시 약 {loss:,.0f}원 · 남은 돈 {r.available(inv + amt):,.0f}원")
    assert amt <= 500000 * 0.40 * w + 1
    assert loss <= 500000 * 0.01 * w + 1
    inv += amt
    buys.append(amt)
    assert inv <= 500000
# 하나 팔면 돈이 돌아옴 (손실 5천 원)
inv -= buys[0]
r.record_exit(-5000)
print(f"하나 손절(-5,000원) 후 남은 돈 {r.available(inv):,.0f}원")
assert abs(r.available(inv) - (500000 - 5000 - inv)) < 1
assert r.can_enter(len(buys) - 1, inv) is None
# 이익은 다시 넣지 않음
r.record_exit(+20000)
assert r.available(inv) == 500000 - inv
# 하루 손실 한도 3% = 15,000원
r.record_exit(-31000)   # 오늘 합계 -16,000원
assert r.can_enter(0, 0) and "손실 한도" in r.halted
print("✅ 투자금 나누기 시험 통과")
