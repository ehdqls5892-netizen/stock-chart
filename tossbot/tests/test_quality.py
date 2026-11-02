"""매수 품질 시험 (python -m tests.test_quality): 확신도 C 안 삼, 나눠 익절 합쳐 세기, 나쁜 방식 빨리 끄기, 성적 나쁘면 크기 줄이기."""
import datetime as dt
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import bot.main as M
import bot.stats as S
from bot.risk import Risk

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))

# 1) 확신도 기준: C 는 안 삼, B 이상만
cfg["market"] = "KR"
r = Risk(cfg, Path(tempfile.mkdtemp()) / "STOP")
assert not r.grade_ok({"grade": "C"}) and r.grade_ok({"grade": "B"}) and r.grade_ok({"grade": "A+"}) and r.grade_ok({})
for sc, ok in ((2.0, False), (3.7, True), (5.5, True)):
    s = {"score": sc}
    r.strength(s)
    assert r.grade_ok(s) == ok, (sc, s)

# 2) 성적 나쁘면 크기 절반
q1 = r.qty_for(10000, 9700, None)
r.perf_mult = 0.5
q2 = r.qty_for(10000, 9700, None)
assert 0 < q2 <= q1 / 2 + 1, (q1, q2)

# 2-1) 현금이 모자라 몇 주만 사지는 경우는 안 삼 (예: 삼성중공업 2주)
r.perf_mult = 1.0
assert r.qty_for(15800, 15300, 33000) == 0 and r.last_skip == "cash", r.qty_for(15800, 15300, 33000)
assert r.qty_for(15800, 15300, None) >= 10                             # 현금 충분하면 정상 수량 (투자금 100만 기준 18주)
import bot.swing as SW
assert SW.qty_for(10_000_000, 0.5, 20, 10000, 9500, 300_000) == 0      # 계획 100주인데 29주만 → 안 삼
assert SW.qty_for(10_000_000, 0.5, 20, 10000, 9500, 700_000) == 67     # 절반 넘게는 삼
r.perf_mult = 0.5

# 3) 나눠 익절 + 마지막 매도 = 한 매매
root = Path(tempfile.mkdtemp())
d = root / "logs" / "live" / "KR"
d.mkdir(parents=True)
today = dt.date.today().isoformat()
rows = [
    # 1차 익절 +10,000 → 마지막 -40,000 : 합치면 진 매매 1건
    {"kind": "partial", "symbol": "A", "name": "가", "opened_at": "t1", "qty": 3, "entry": 10000, "exit_price": 10300, "pnl": 10000,
     "exit_reason": "1차 익절", "extra": {"setup": "open", "grade": "C"}},
    {"kind": "exit", "symbol": "A", "name": "가", "opened_at": "t1", "qty": 6, "entry": 10000, "exit_price": 9400, "pnl": -40000,
     "exit_reason": "손절", "extra": {"setup": "open", "grade": "C", "R": 300}},
]
for i in range(3):   # 데스크 계획 3번 다 손실
    rows.append({"kind": "exit", "symbol": f"P{i}", "name": f"계획{i}", "opened_at": f"p{i}", "qty": 10, "entry": 10000,
                 "exit_price": 9300, "pnl": -65000, "exit_reason": "손절", "extra": {"setup": "plan", "grade": "B", "R": 400}})
(d / f"trades-{today}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows), encoding="utf-8")
tr = S.merge(S.load(root, "KR", 7))
assert len(tr) == 4 and tr[0]["pnl"] == -30000, tr
summ = S.summarize(S.load(root, "KR", 7), "KR", 10_000_000)
print(summ)
assert "매매 4건" in summ and "승률 0%" in summ and "확신도별" in summ
print(S.diagnose(root, 7))
assert "계획0" in S.diagnose(root, 7)

# 4) 3번 다 잃은 방식은 8건 안 돼도 바로 끔
M.ROOT = root
c2 = json.loads(json.dumps(cfg))
c2["strategy"]["setups"].setdefault("plan", {})["enabled"] = True
fake = SimpleNamespace(cfg=c2, m="KR", log=print, notify=lambda m: None)
M.Bot.setup_gate(fake)
assert c2["strategy"]["setups"]["plan"]["enabled"] is False, "데스크 계획 안 꺼짐"

# 5) 최근 성적 마이너스 → 실전 크기 절반
fake2 = SimpleNamespace(cfg=cfg, m="KR", mode="live", log=print, notify=lambda m: None, risk=SimpleNamespace(perf_mult=1.0))
cfg["perf_size"]["min_trades"] = 3
M.Bot.perf_size(fake2)
assert fake2.risk.perf_mult == 0.5

# 6) 손실로 판 종목은 며칠 다시 안 삼
fake3 = SimpleNamespace(cfg=cfg, m="KR", mode="live", log=print)
M.Bot.load_stopped(fake3)
assert "P0" in fake3.stopped and "A" in fake3.stopped, fake3.stopped

# 7) 자잘한 손실 3번(합쳐 1R 미만)은 방식 안 끔
c3 = json.loads(json.dumps(cfg))
c3["strategy"]["setups"]["hod"]["enabled"] = True
tiny = [{"kind": "exit", "symbol": f"H{i}", "name": "h", "opened_at": f"h{i}", "qty": 1, "entry": 100, "exit_price": 99.9,
         "pnl": -1000, "exit_reason": "장 마감 전 정리", "extra": {"setup": "hod"}} for i in range(3)]
with open(d / f"trades-{today}.jsonl", "a", encoding="utf-8") as fp:
    fp.write("\n" + "\n".join(json.dumps(x, ensure_ascii=False) for x in tiny))
M.Bot.setup_gate(SimpleNamespace(cfg=c3, m="KR", log=print, notify=lambda m: None))
assert c3["strategy"]["setups"]["hod"]["enabled"] is True, "자잘한 손실로 꺼짐"

# 8) 이번 주 손실이 투자금 3% 넘으면 이번 주 새 매수 멈춤
kday = M.now("KR").date().isoformat()          # 시험 기계 날짜와 한국 날짜가 다를 수 있어 한국 날짜로
if kday != today:
    (d / f"trades-{kday}.jsonl").write_text((d / f"trades-{today}.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
rk = Risk(cfg, Path(tempfile.mkdtemp()) / "STOP")
rk.capital = 3_000_000          # 5% = 15만, 이번 주 손실 ≈ 22.8만
fake4 = SimpleNamespace(cfg=cfg, m="KR", risk=rk, log=print, notify=lambda m: None)
M.Bot.week_guard(fake4)
assert rk.halted and "이번 주" in rk.halted, rk.halted
print("✅ 매수 품질 시험 통과")

# 9) 매수 기록: 계획 수량 vs 체결 수량
from bot import stats as S3
rows2 = [{"kind": "buy_sent", "symbol": "010140", "name": "삼성중공업", "opened_at": "2026-10-30T10:01:00", "qty": 120, "reason": "확신도 B · 시초 돌파"},
         {"kind": "armed", "symbol": "010140", "name": "삼성중공업", "opened_at": "2026-10-30T10:01:00", "qty": 2}]
with open(d / f"trades-{today}.jsonl", "a", encoding="utf-8") as fp:
    fp.write("\n" + "\n".join(json.dumps(x, ensure_ascii=False) for x in rows2))
txt = S3.buys(root, 14, "삼성중공업")
print(txt)
assert "계획 120주 → 체결 2주 ⚠️" in txt
print("✅ 매수 기록 시험 통과")

# 10) 현금은 실제 계좌로 다시 맞춤 (판 돈이 돌아온 것 반영)
import time as _t
calls = []
fb = SimpleNamespace(m="KR", _cash_track=True, _cash_t=0.0,
                     api=SimpleNamespace(buying_power=lambda cur: calls.append(cur) or {"cashBuyingPower": "10132484"}))
assert M.Bot.fresh_cash(fb, 33000) == 10132484 and calls == ["KRW"]
assert M.Bot.fresh_cash(fb, 5000) == 5000 and len(calls) == 1            # 20초 안엔 다시 안 물어봄
assert M.Bot.fresh_cash(fb, 5000, force=True) == 10132484                # 스윙·ETF 매수 직전엔 꼭 확인
fb._cash_track = False
assert M.Bot.fresh_cash(fb, None, force=True) is None                     # 미장 자동 환전 모드는 그대로
print("✅ 현금 다시 맞추기 시험 통과")
