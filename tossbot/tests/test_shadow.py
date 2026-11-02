"""단타 그림자 장부 시험 (python -m tests.test_shadow): 단타가 꺼져 있어도 신호를 '샀다고 치고' 기록, 실제 주문은 없음."""
import os
os.environ["TOSSBOT_NO_BG"] = "1"
os.environ["TOSSBOT_OFFLINE"] = "1"
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import bot.main as M
import bot.stats as S
from bot.broker import PaperBroker
from bot.risk import Risk

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
assert cfg["strategy"]["intraday"] is False and cfg["shadow"]["enabled"]
root = Path(tempfile.mkdtemp())
M.ROOT = root
cfg["market"] = "KR"
sd = root / "logs" / "shadow" / "KR"
sd.mkdir(parents=True)
real = PaperBroker("KR", cfg, root, print, lambda m: None)
sh = PaperBroker("KR", cfg, sd, lambda m: None, lambda m: None)
rk = Risk(cfg, root / "X")
rk.capital = 10_000_000
sent = []
import datetime as dt
open_t = dt.datetime(2026, 11, 2, 9, 0, tzinfo=M.TZ["KR"])
st = SimpleNamespace(name="가종목", last=10000)
bot = SimpleNamespace(cfg=cfg, m="KR", shadow=sh, shadow_risk=rk, broker=real, universe=["A"], syms={"A": st}, ctx={},
                      open_t=open_t, _sh_t=0.0, market_move=lambda: 0.1, log=print, notify=sent.append)
# 신호를 흉내: entry_signal / exit_levels 를 바꿔 끼움
M.entry_signal = lambda st, n, o, c, x: {"score": 3.0, "setup": "orb", "reason": "시초 돌파", "grade": "C"}
M.exit_levels = lambda m, est, sig, cfg, st, o: (est * 1.2, est * 0.97, est * 0.03, est * 0.01)
n = open_t + dt.timedelta(minutes=30)
M.Bot.shadow_tick(bot, n, open_t + dt.timedelta(hours=5), open_t + dt.timedelta(hours=6), "2026-11-02")
assert "A" in sh.pos and not real.pos, "그림자인데 실제로 삼"
p = sh.pos["A"]
assert p.extra["grade"] == "C"                       # 확신도 C 도 기록
sh.on_price("A", 9600, n)                            # 손절
assert "A" not in sh.pos
M.Bot.shadow_tick(bot, open_t + dt.timedelta(hours=6), open_t + dt.timedelta(hours=5), open_t + dt.timedelta(hours=6), "2026-11-02")
assert sent and "그림자" in sent[-1], sent
print(sent[-1])
txt = S.shadow_report(root, 30)
print(txt)
assert "매매 1건" in txt and "확신도별" in txt
print("✅ 단타 그림자 시험 통과")
