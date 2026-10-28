"""잠금 시험 (python -m tests.test_lock)."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import bot.app as A
from bot.broker import PaperBroker, Position

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
b = PaperBroker("KR", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
for i, nm in enumerate(["이수페타시스", "두산로보틱스", "피앤에스로보틱스", "셀트리온"]):
    b.pos[str(i)] = Position(str(i), nm, 10, 10000, 12000, 9700, "x", "x", state="open", extra={"R": 300})
A.STATE["bots"] = {"KR": SimpleNamespace(broker=b)}
assert b.open_count() == 4
print(A.lock_cmd("잠금 이수"))
assert b.open_count() == 3 and b.invested() == 300000, "잠금 종목이 한도에 셈"
_, part, _ = b.manage(b.pos["0"], 10700)
assert part is None, "잠금 종목을 나눠 팜"
print(A.lock_cmd("잠금"))
print(A.lock_cmd("잠금 해제 이수"))
assert b.open_count() == 4
print("✅ 잠금 시험 통과")
