"""데스크 자료 읽기 시험: 가격 계획·관심·실적(발표 전 막기 / 서프라이즈 / 실망) (python -m tests.test_desk)."""
import datetime as dt
import json
import types
from pathlib import Path

import requests

import bot.main as M
from bot.util import TZ

NOW = dt.datetime(2026, 10, 27, 9, 30, tzinfo=TZ["US"])
M.now = lambda m: NOW
FEED = {
    "report": {"date": "2026-10-26", "plans": {"AAA": {"stop": 9, "entryLow": 10, "entryHigh": 11, "t1": 13, "src": "1순위"},
                                              "BAD": {"stop": 9}}, "watch": ["THM"]},
    "live": {"date": "2026-10-27", "plans": {"AAA": {"stop": 9.5, "entryLow": 10.2, "entryHigh": 11, "src": "장중 1순위"}}, "watch": ["HOT"]},
    "earn": {"date": "2026-10-27", "upcoming": {"NVDA": "2026-10-27", "MSFT": "2026-10-28", "LATER": "2026-11-05", "GOOD": "2026-10-27"},
             "good": {"GOOD": {"date": "2026-10-27", "text": "예상보다 좋음"}, "OLD": {"date": "2026-10-10"}}, "bad": {"FAIL": {"date": "2026-10-26"}}},
}


class R:
    def __init__(self, j):
        self.status_code, self._j = (200, j) if j is not None else (404, None)
    def json(self):
        return self._j


def fake_get(url, timeout=0):
    kind = url.rsplit("-", 1)[-1].replace(".json", "")
    return R(FEED.get(kind))


requests.get = fake_get
cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
logs = []
b = types.SimpleNamespace(m="US", cfg=cfg, log=logs.append, ctx={})
M.Bot.load_desk(b)
c = b.ctx
print(logs[-1])
assert set(c["plans"]) == {"AAA"} and c["plans"]["AAA"]["entryLow"] == 10.2, c["plans"]   # 장중 1순위가 더 최신 → 덮어씀
assert {"THM", "HOT", "AAA", "GOOD"} <= c["watched"], c["watched"]
assert c["earn_block"] == {"NVDA", "MSFT", "FAIL"}, c["earn_block"]                      # 발표 전 2개 + 실망 1개 (GOOD 은 결과 좋음이라 안 막음)
assert set(c["earn_good"]) == {"GOOD"}, c["earn_good"]                                  # 오래된 OLD 는 제외
FEED["report"]["date"] = "2026-10-10"; FEED.pop("live"); FEED.pop("earn")              # 오래된 자료·파일 없음
M.Bot.load_desk(b)
assert not b.ctx["plans"] and not b.ctx["earn_block"], b.ctx
print("✅ 데스크 자료 시험 통과")
