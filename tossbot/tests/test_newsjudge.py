"""1시간마다 뉴스 AI 판정: 묶음 호출·같은 기사 재사용·심한 악재 → 매수 금지·앱 표시·보유 알림."""
import datetime as dt
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TOSSBOT_OFFLINE", "1")
from bot import news_judge as NJ  # noqa: E402

calls = []


def fake(model, prompt):
    calls.append(prompt)
    return {"A": {"impact": "악재", "why": "유상증자 발표", "adj": -2, "severe": True},
            "B": {"impact": "호재", "why": "큰 계약", "adj": 5, "severe": True},
            "C": {"impact": "중립", "why": "", "adj": -1}}


news = {"A": ["A 유상증자 결정"], "B": ["B 1조 공급계약"], "C": ["C 시황"], "D": []}
items = [{"code": c, "name": c + "사"} for c in "ABCD"]
r = NJ.judge("KR", items, lambda c, n: news[c], {}, fake)
assert len(calls) == 1, "10종목까지 한 번에 묶어 호출"
assert r["A"]["impact"] == "악재" and r["A"]["severe"] and r["A"]["adj"] == -2
assert r["B"]["adj"] == 1 and not r["B"]["severe"], "호재 adj 상한 +1, severe 는 악재만"
assert r["C"]["adj"] == 0 and "D" not in r, "중립은 0, 기사 없으면 판정 안 함"
r2 = NJ.judge("KR", items, lambda c, n: news[c], r, fake)
assert len(calls) == 1 and r2["A"] == r["A"], "같은 기사면 다시 안 읽음"
news["A"] = ["A 유상증자 철회"]
NJ.judge("KR", items, lambda c, n: news[c], r, fake)
assert len(calls) == 2 and '"A"' in calls[1] and '"B"' not in calls[1], "새 기사 있는 종목만 다시 읽음"
# 오래된 판정은 안 씀
now = dt.datetime(2026, 11, 9, 5, tzinfo=dt.timezone.utc)
d = {"stocks": {"A": {"t": "2026-11-09T02:00+00:00"}, "B": {"t": "2026-11-08T20:00+00:00"}}}
assert list(NJ.fresh(d, 6, now)) == ["A"]

# 로봇 매수 직전 점검: 심한 악재면 안 삼, 일반 악재는 주의 + 점수 반영
from bot import main as M  # noqa: E402
from bot import swing as SW  # noqa: E402
bars = [(dt.date(2026, 10, 1) + dt.timedelta(days=i), 100 + i * 0.3, 101 + i * 0.3, 99 + i * 0.3, 100.5 + i * 0.3, 1000) for i in range(80)]
b = SimpleNamespace(m="KR", cfg={"daily_swing": {"brief_news": False, "min_forward_score": -99}})
M.Bot.est_row = lambda self, c: {}
M.Bot.daily_full = lambda self, c: bars
SW.forward_score = lambda *a, **k: (3, ["＋기본"])
for code, sev, exp in (("A", True, "심한 악재"), ("B", False, None)):
    M.Bot.news_judged = lambda self, code=code, sev=sev: {code: {"impact": "악재", "why": "나쁜 소식", "adj": -2, "severe": sev, "head": "제목"}}
    x = {"code": code, "name": code, "close": 124.0, "stop": 115.0, "ev": {}}
    why, txt, warn = M.Bot.cand_check(b, x, 124.0)
    if exp:
        assert why and exp in why, why
    else:
        assert why is None and any("새 악재" in w for w in warn), (why, warn)
        assert x["_fscore"] == 1 and "새 뉴스 AI 악재" in x["_fwhy"][0], x["_fwhy"]

# 앱 표시: 심한 악재는 무조건 빨강
from bot import mobile as MB  # noqa: E402
ctx = {"sc": {}, "nj": {"A": {"impact": "악재", "why": "유상증자 발표", "severe": True}, "B": {"impact": "호재", "why": "큰 계약"}},
       "ev": {}, "an": {}, "good": {}, "bad": {}, "up": {}, "imp": {}, "pk": {}, "ch": {}}
MB.est = lambda mk, c: {}
ta, tb = MB.tags("KR", "A", ctx), MB.tags("KR", "B", ctx)
assert ta["bias"] == "악재" and "심한 악재" in ta["why"][0], ta
assert tb["bias"] == "호재" and "큰 계약" in tb["why"][0], tb

# 보유 종목: 서버가 1시간마다 판정, 새로 악재가 되면 알림 (한 번만)
from bot import alerts as AL  # noqa: E402
from bot import app as A  # noqa: E402
from bot import push as P  # noqa: E402
pos = SimpleNamespace(symbol="A", name="A사", state="open")
A.STATE["bots"] = {"KR": SimpleNamespace(loop_on=True, broker=SimpleNamespace(pos={"A": pos}))}
pushed = []
P.push_event = lambda kind, title, body, mk=None, code=None, full="": pushed.append((kind, title))
AL.hold_news("KR", news_fn=lambda c, n: ["A 유상증자 결정"], claude=fake)
assert pushed and "심한 악재" in pushed[0][1], pushed
AL._last_nj.clear()
AL.hold_news("KR", news_fn=lambda c, n: ["A 유상증자 결정"], claude=fake)
assert len(pushed) == 1, "같은 악재는 다시 안 알림"
assert MB.NJ_LOCAL["KR"]["A"]["severe"]
print("✅ 뉴스 AI 판정 시험 통과")
