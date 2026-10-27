"""계좌 낙폭 한도·성적 나쁜 방식 끄기 시험 (python -m tests.test_guard)."""
import datetime as dt
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import bot.main as M
from bot.risk import Risk

root = Path(tempfile.mkdtemp())
M.ROOT = root
cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
cfg["market"] = "KR"
cfg["risk"]["KR"]["daily_capital"] = 1_000_000
d = root / "logs" / "live" / "KR"
d.mkdir(parents=True)
today = dt.date.today().isoformat()


def write(rows):
    (d / f"trades-{today}.jsonl").write_text("\n".join(json.dumps(
        {"t": (dt.datetime.now() - dt.timedelta(hours=1, minutes=60 - i)).isoformat(timespec="seconds"), "kind": "exit", "exit_price": 1, "pnl": p, "reason": r, "extra": {}},
        ensure_ascii=False) for i, (p, r) in enumerate(rows)), encoding="utf-8")


logs, sent, flat = [], [], []
b = SimpleNamespace(cfg=cfg, m="KR", mode="live", risk=Risk(cfg, root / "STOP"), log=logs.append, notify=sent.append,
                    syms={}, broker=SimpleNamespace(pos={"A": 1}, flatten=lambda s, w, l: flat.append(s)))
# 1) 고점 +5만 → −4만: 낙폭 9만(9%) → 아직 괜찮음
write([(50000, "시초 15분 고점 돌파")] + [(-15000, "시초 15분 고점 돌파")] * 6)
M.Bot.dd_check(b)
assert not b.risk.halted, b.risk.halted
# 2) 낙폭 11만 → 멈춤 + 전부 정리
write([(50000, "시초")] + [(-15000, "시초 15분 고점 돌파")] * 7 + [(-5000, "시초")])
M.Bot.dd_check(b)
assert b.risk.halted and "낙폭" in b.risk.halted and flat == ["A"], (b.risk.halted, flat)
print(sent[-1])
# 3) 재개하면 풀림
import bot.app as A
A.STATE["bots"] = {}
print(A.resume_dd())
b.risk.halted = None
M.Bot.dd_check(b)
assert not b.risk.halted, "재개 후에도 멈춤"

# 4) 성적 나쁜 방식 끄기: 시초 돌파 9건 대부분 손실 → 끔, 평균단가 반등은 그대로
write([(-5000, "시초 15분 고점 돌파")] * 7 + [(3000, "시초 15분 고점 돌파")] * 2 + [(4000, "VWAP 반등")] * 3)
b.cfg["strategy"]["orb_enabled"] = True
M.Bot.setup_gate(b)
assert b.cfg["strategy"]["orb_enabled"] is False and b.cfg["strategy"]["setups"]["vwap"]["enabled"]
print(sent[-1])
print("✅ 낙폭 한도·방식 끄기 시험 통과")
