"""감시 범위 넓히기·ETF 규칙 시험 (python -m tests.test_wide)."""
import json
import tempfile
from pathlib import Path

import bot.stream as ST
from bot.broker import PaperBroker, Position
from bot.strategy import SymState, exit_levels


class FS:
    def __init__(self, *a, **k):
        self.codes, self.connected = [], True
    def start(self): pass
    def set_codes(self, c): self.codes = c
    def stop(self): pass


ST.Stream = FS
ms = ST.MultiStream(None, "US", None, n=2)
ms.start()
ms.set_codes([f"S{i}" for i in range(180)])
print("연결별 구독 수:", [len(p.codes) for p in ms.parts])
assert [len(p.codes) for p in ms.parts] == [95, 85]
ms.parts[1].connected = False                     # 2번째 연결이 안 되면 첫 연결만 사용
ms.set_codes([f"S{i}" for i in range(180)])
assert [len(p.codes) for p in ms.parts] == [95, 0]

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
cfg["market"] = "US"
b = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), lambda m: None, lambda m: None)
etf = Position("SOXL", "SOXL", 10, 40, 46, 39, "시험", "x", state="open", peak=42, extra={"R": 1, "etf": True})
assert b.carry_check(etf, 43, 41) and "ETF" in b.carry_check(etf, 43, 41)
etf.sl = 40.1
assert not b.can_add(etf, 43), "ETF 인데 불타기 함"
st = SymState("SOXL"); st.etf = True
tp, sl, R, a = exit_levels("US", 40.0, {"or_high": 36.0, "vwap": 36.0, "level": 36.0}, cfg, st, None)
print(f"ETF 손절선 {sl} (최대 7%까지 넓게, 주식은 4.5%)")
assert 37.1 <= sl <= 37.3
st2 = SymState("AAPL")
tp2, sl2, *_ = exit_levels("US", 40.0, {"or_high": 36.0, "vwap": 36.0, "level": 36.0}, cfg, st2, None)
assert sl2 >= 38.19
print("✅ 넓히기·ETF 시험 통과")
