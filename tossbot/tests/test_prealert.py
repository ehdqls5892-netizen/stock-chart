"""장 마감 전 정리 예정 알림 시험 (python -m tests.test_prealert)."""
import datetime as dt
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import bot.main as M
from bot.broker import PaperBroker, Position
from bot.strategy import Bar, SymState
from bot.util import TZ

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
cfg["market"] = "KR"
OPEN = dt.datetime(2026, 10, 28, 9, 0, tzinfo=TZ["KR"])
msgs = []
br = PaperBroker("KR", cfg, Path(tempfile.mkdtemp()), lambda m: None, msgs.append)
st = SymState("007660", "이수페타시스", prev_close=89000)
for i in range(60):
    t = OPEN + dt.timedelta(minutes=i)
    st.bars[t] = Bar(t, 90000 + i * 10, 90100 + i * 10, 89900 + i * 10, 90000 + i * 10, 1000)
st.last = 89100            # 고점(90690)에서 밀려 산 값 근처
br.pos["007660"] = Position("007660", "이수페타시스", 46, 89100, 100000, 86500, "x", "x", state="open", extra={"R": 2600})
b = SimpleNamespace(m="KR", broker=br, syms={"007660": st}, open_t=OPEN, ctx={"daily": {}, "earn_block": set()},
                    market_move=lambda: 0.2, log=lambda m: None, notify=msgs.append)
M.Bot.pre_close_alert(b, OPEN.replace(hour=15, minute=15))
print(msgs[-1])
assert "정리 예정" in msgs[-1] and "잠금 이수페타시스" in msgs[-1] and "애매" in msgs[-1]
br.pos["007660"].extra["locked"] = True
n = len(msgs)
M.Bot.pre_close_alert(b, OPEN.replace(hour=15, minute=15))
assert len(msgs) == n, "잠금 종목에 알림"
print("✅ 장 마감 전 알림 시험 통과")
