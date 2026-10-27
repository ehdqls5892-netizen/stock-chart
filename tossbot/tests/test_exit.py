"""나눠 익절·손절 판단 시험 (python -m tests.test_exit)."""
import json
import tempfile
import time
from pathlib import Path

import bot.broker as B
from bot.broker import PaperBroker, Position

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
cfg["stop_judge"]["enabled"] = True     # 기본은 꺼짐(손절선에서 바로) — 켰을 때 동작 시험
logs = []
b = PaperBroker("KR", cfg, Path(tempfile.mkdtemp()), logs.append, lambda m: None)
info = {}
b.judge = lambda sym: dict(info)


def pos(qty=9, sl=9700):
    p = Position("T", "시험주", qty, 10000, 11500, sl, "시험", "x", state="open", peak=10000, extra={"R": 300, "atr": 10})
    b.pos["T"] = p
    return p


# 1) 1R(+3%) → 1/3 익절, 손절선은 산 값 −0.4R(9880) — 바로 본전에 걸지 않음
p = pos()
b.on_price("T", 10300, None)
assert p.qty == 6 and b.closed[-1].exit_reason == "1차 익절", (p.qty, b.closed[-1].exit_reason)
assert p.sl == 9880, p.sl
print("1R:", logs[-1])
# 1.5R → 본전(수수료 포함) 위
b.on_price("T", 10450, None)
assert 10000 < p.sl <= 10030, p.sl
# 2R → 남은 것의 절반 2차 익절
b.on_price("T", 10600, None)
assert p.qty == 3 and b.closed[-1].exit_reason == "2차 익절"
print("2R:", logs[-1])
# 이익 구간에선 손절선 = 서버 손절선 (판단 없이 정확히)
assert b.hard_level(p) == p.sl

# 2) 손실 구간: 서버(최후) 손절선은 판단선보다 손절 폭의 절반 아래
q = pos(10, 9700)
assert b.hard_level(q) == 9550, b.hard_level(q)
# 거래량 적고 시장 괜찮음 → 판단선 아래여도 기다림
info.update(vwap=9720, vol_x=0.7, market=0.1)
b.on_price("T", 9690, None)
assert q.state == "open", "살짝 밀렸는데 바로 팜"
print("기다림:", logs[-1])
# 회복하면 그대로 보유
b.on_price("T", 9760, None)
assert q.state == "open" and "soft_since" not in q.extra
# 다시 밀리는데 파는 거래량 많음 → 바로 팜
info.update(vol_x=2.3)
b.on_price("T", 9680, None)
assert q.state == "closed" and "거래량" in q.exit_reason, q.exit_reason
print("거래량 손절:", q.exit_reason)

# 3) 시장 전체가 약하면 바로 팜
q = pos(10, 9700)
info.update(vol_x=0.5, market=-0.9, vwap=9700)
b.on_price("T", 9690, None)
assert q.state == "closed" and "시장" in q.exit_reason
# 4) 악재(실적 실망)면 바로 팜
q = pos(10, 9700)
info.update(market=0.2, bad="실적 실망 종목")
b.on_price("T", 9690, None)
assert q.state == "closed" and "실적" in q.exit_reason
info.pop("bad")
# 5) 10분 안에 회복 못 하면 팜
q = pos(10, 9700)
b.on_price("T", 9690, None)
q.extra["soft_since"] = time.time() - 11 * 60
b.on_price("T", 9690, None)
assert q.state == "closed" and "10분" in q.exit_reason
# 6) 최후 손절선 닿으면 무조건 팜
q = pos(10, 9700)
b.on_price("T", 9540, None)
assert q.state == "closed" and "최후" in q.exit_reason
# 7) 최후 손절선은 −6% 보다 깊어지지 않음
r = pos(10, 9550)
assert b.hard_level(r) >= 9400
# 8) 끄면 예전처럼 판단선에서 바로
cfg["stop_judge"]["enabled"] = False
q = pos(10, 9700)
assert b.hard_level(q) == 9700
b.on_price("T", 9690, None)
assert q.state == "closed"
cfg["stop_judge"]["enabled"] = True
# 9) 2주면 1주만 1차 익절
q = pos(2, 9700)
b.on_price("T", 10300, None)
assert q.qty == 1
print("\n✅ 나눠 익절·손절 판단 시험 통과")
