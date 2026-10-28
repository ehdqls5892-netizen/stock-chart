"""추가 매수(불타기)·스윙 시험 (python -m tests.test_pyramid)."""
import json
import tempfile
from pathlib import Path

from bot.broker import PaperBroker, Position

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
cfg["market"] = "KR"
logs = []
b = PaperBroker("KR", cfg, Path(tempfile.mkdtemp()), logs.append, lambda m: None)

# 1) 산 지 얼마 안 됨(손절선이 산 값 아래) → 추가 매수 안 함
p = Position("TEST", "시험주", 10, 10000, 11500, 9700, "시험", "2026-10-27T09:20", state="open", peak=10000,
             extra={"R": 300, "q0": 10})
b.pos["TEST"] = p
assert not b.can_add(p, 10350), "손절선이 본전 아래인데 추가함"
# 2) 1R 이상 올라 손절선을 본전 위로 올림 → 추가 가능
p.sl = 10020
assert b.can_add(p, 10400)
q = b.add_qty(p, 10400, max_loss=5000, budget=1_000_000)
print("추가 수량", q, "(처음 10주의 절반까지)")
assert q == 5
b.add(p, q, 10400, "시험 돌파")
print(logs[-1])
assert p.qty == 15 and 10000 < p.entry < 10400
# 추가 뒤 평균 단가가 손절선보다 높아도 '손절 시 손실' 이 한도 이하인지
loss = (p.entry - p.sl) * p.qty
print(f"추가 뒤 손절되면 손실 약 {loss:,.0f}원 (한도 5,000원)")
assert loss <= 5000
# 3) 손실 한도가 빡빡하면 추가 수량이 줄어듦
p2 = Position("T2", "시험2", 10, 10000, 11500, 10020, "시험", "x", state="open", peak=10400, extra={"R": 300, "q0": 10})
q2 = b.add_qty(p2, 10600, max_loss=1500, budget=1_000_000)
print("손실 한도 1,500원일 때 추가 수량", q2)
assert q2 < 5
# 4) 최대 2번까지
p.extra["adds"] = 2
assert not b.can_add(p, 10800)

# 5) 스윙: 들고 가기 기준이 낮고(+0.5%), 밤사이 손절선이 넓음(−7% 또는 지지선 아래)
s = Position("SW", "스윙주", 10, 10000, 14000, 9700, "시험", "x", state="open", peak=10100,
             extra={"R": 300, "swing": True, "support": 9500})
d = Position("DT", "단타주", 10, 10000, 11500, 9700, "시험", "x", state="open", peak=10100, extra={"R": 300})
assert b.carry_check(s, 10080, 10050) is None, "스윙인데 +0.8% 로 못 들고 감"
assert b.carry_check(d, 10080, 10050) is None, "손절선 살아 있고 평균 단가 위인데 정리함"
assert b.carry_stop(d, 10080) == 9900, "작은 이익으로 밤을 넘길 땐 손절선을 산 값 −1% 로"
assert b.carry_check(d, 9980, 9900) is not None, "손실 중인데 밤을 넘김"
assert b.carry_check(d, 10080, 10300) is not None, "평균 단가 아래로 밀렸는데 들고 감"
cfg["carry"]["hold_if_intact"] = False
assert b.carry_check(d, 10080, 10050) is not None, "옛 규칙(+3%)에서 +0.8% 로 들고 감"
cfg["carry"]["hold_if_intact"] = True
ss = b.carry_stop(s, 10080)
print("스윙 밤사이 손절선", ss, "(지금가 −7% 와 지지선 9500 아래 중 높은 값, 원래 손절 9700 보다 낮아지진 않음)")
assert ss == 9700
s2 = Position("SW2", "스윙주2", 10, 10000, 14000, 9700, "시험", "x", state="open", peak=10600, extra={"R": 300, "swing": True})
assert b.carry_stop(s2, 10500) >= 10000, "수익 5% 인데 본전을 안 지킴"
# 6) 스윙 추적 손절은 고점에서 6% (단타는 3%)
s3 = Position("SW3", "스윙3", 10, 10000, 14000, 10020, "시험", "x", state="open", peak=10000, extra={"R": 300, "swing": True, "atr": 5})
d3 = Position("DT3", "단타3", 10, 10000, 11500, 10020, "시험", "x", state="open", peak=10000, extra={"R": 300, "atr": 5})
ns, _, _ = b.manage(s3, 11000)
nd, _, _ = b.manage(d3, 11000)
print("고점 11,000 에서 추적 손절선 — 스윙", ns, "/ 단타", nd)
assert ns < nd
print("\n✅ 추가 매수·스윙 시험 통과")
# 7) 처음 1주만 산 종목도 추가 매수 1주는 가능
one = Position("ONE", "1주종목", 1, 10000, 11500, 10020, "시험", "x", state="open", peak=10400, extra={"R": 300, "q0": 1})
assert b.add_qty(one, 10400, max_loss=5000, budget=1_000_000) == 1
print("✅ 1주 종목 추가 매수 확인")
