"""번 돈 지키기·성적표 시험 (python -m tests.test_review)."""
import json
import tempfile
from pathlib import Path

from bot import stats
from bot.risk import Risk

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
cfg["market"] = "KR"
cfg["risk"]["KR"]["daily_capital"] = 1_000_000
r = Risk(cfg, Path(tempfile.mkdtemp()) / "STOP")
q0 = r.qty_for(10000, 9700, None)
r.record_exit(25000)                       # 오늘 +2.5% 벌었음 → 새 매매 절반 크기
assert r.size_mult() == 0.5 and r.qty_for(10000, 9700, None) < q0
assert r.can_enter(0) is None
r.record_exit(-14000)                      # 최고 25,000 → 11,000 (절반 넘게 반납) → 새 매수 멈춤
assert r.can_enter(0) and "지키기" in r.can_enter(0), r.can_enter(0)
print("번 돈 지키기:", r.halted)

root = Path(tempfile.mkdtemp())
d = root / "logs" / "live" / "KR"
d.mkdir(parents=True)
import datetime as dt
lines = []
for i, (pnl, reason) in enumerate([(5000, "장초반 출발(1순위)"), (-3000, "시초 돌파 · …"), (8000, "VWAP 반등"), (-2000, "VWAP 반등")]):
    lines.append(json.dumps({"kind": "exit", "exit_price": 1, "pnl": pnl, "reason": reason, "extra": {}}, ensure_ascii=False))
(d / f"trades-{dt.date.today().isoformat()}.jsonl").write_text("\n".join(lines), encoding="utf-8")
txt = stats.report(root, 14, {"KR": 1_000_000})
print(txt)
assert "승률 50%" in txt and "평균단가 반등 2건" in txt and "아직 판단 이르다" in txt
print("✅ 번 돈 지키기·성적표 시험 통과")
