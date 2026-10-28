"""직접 판 종목 반영 시험 (python -m tests.test_reconcile)."""
import json
import tempfile
from pathlib import Path

from bot.broker import LiveBroker, Position

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))


class API:
    def __init__(self):
        self.hold, self.canceled, self.mod = {"A": 0, "B": 3}, [], []

    def holdings(self):
        return {"items": [{"symbol": k, "quantity": str(v)} for k, v in self.hold.items() if v]}

    def cancel_conditional(self, cid):
        self.canceled.append(cid)

    def replace_stop_market(self, cid, sym, q, trig, exp):
        self.mod.append((sym, q))
        return cid, "정정"

    def is_alive(self, cid):
        return True

    def get_conditional(self, cid):
        return {"status": "WATCHING"}


api, msgs = API(), []
b = LiveBroker(api, "KR", cfg, Path(tempfile.mkdtemp()), msgs.append, msgs.append)
b.pos["A"] = Position("A", "셀트리온", 1, 190000, 220000, 185500, "x", "x", state="open", cond_id="c1")
b.pos["B"] = Position("B", "피앤에스", 5, 4522, 6000, 4850, "x", "x", state="open", cond_id="c2")
b.reconcile()
assert "A" not in b.pos and api.canceled == ["c1"], "직접 판 종목이 남아 있음"
assert b.pos["B"].qty == 3 and api.mod == [("B", 3)], "일부 판 수량 반영 안 됨"
print("\n".join(msgs[-2:]))
print("✅ 직접 판 종목 반영 시험 통과")
