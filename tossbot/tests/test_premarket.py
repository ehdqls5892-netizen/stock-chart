"""프리마켓 갭 후보 찾기 시험 (python -m tests.test_premarket)."""
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import bot.main as M
from bot.strategy import SymState
from bot.util import TZ

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
OPEN = dt.datetime(2026, 10, 27, 9, 30, tzinfo=TZ["US"])
M.now = lambda m="US": OPEN - dt.timedelta(minutes=8)
PX = {"AAA": 105.0, "BBB": 100.5, "CCC": 130.0, "DDD": 97.0, "EEE": 103.0}


class API:
    def prices(self, syms):
        return [{"symbol": s, "lastPrice": str(PX[s])} for s in syms if s in PX]

    def candles(self, sym, interval, n):
        return [{"timestamp": "2026-10-26T16:00:00-04:00", "closePrice": "100"}]


logs = []
me = SimpleNamespace(cfg=cfg, m="US", open_t=OPEN, api=API(), syms={s: SymState(s, s) for s in PX},
                     ctx={"pre": {}}, log=logs.append)
me.prev_close = lambda s: M.Bot.prev_close(me, s)
M.Bot.premarket_scan(me, list(PX))
print(logs[-1])
assert list(me.ctx["pre"]) == ["AAA", "EEE"], me.ctx["pre"]     # +5%, +3% (0.5%·−3%·+30% 는 제외)
M.now = lambda m="US": OPEN + dt.timedelta(minutes=1)          # 장 열린 뒤엔 다시 안 찾음(후보 유지)
M.Bot.premarket_scan(me, list(PX))
assert list(me.ctx["pre"]) == ["AAA", "EEE"]
print("✅ 프리마켓 후보 시험 통과")
