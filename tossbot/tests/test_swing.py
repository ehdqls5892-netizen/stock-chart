"""일봉 스윙 시험 (python -m tests.test_swing): 후보 받기·매수 조건·수량·일봉 점검(본전·추적·10일선·기간)·장중 손절선 고정."""
import datetime as dt
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import bot.main as M
import bot.swing as SW
from bot.broker import PaperBroker

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
c = cfg["daily_swing"]
assert cfg["strategy"]["intraday"] is False and c["enabled"]

# 1) 수량: 손실 한도 0.5%·한 종목 20%·현금 안
assert SW.qty_for(10_000_000, 0.5, 20, 10000, 9500, None) == 100          # 5만 / 500
assert SW.qty_for(10_000_000, 0.5, 20, 10000, 9900, None) == 200          # 20% 한도(200만)에 걸림
assert SW.qty_for(10_000_000, 0.5, 20, 10000, 9500, 300_000) == 0         # 현금 30만 → 계획의 절반도 안 돼 안 삼
assert SW.qty_for(10_000_000, 0.5, 20, 10000, 9500, 600_000) == 58        # 절반 넘으면 현금만큼
assert SW.qty_for(10_000_000, 0.5, 20, 10000, 10100, None) == 0

# 2) 일봉 점검
def bars(closes):
    d0 = dt.date(2026, 10, 1)
    return [(d0 + dt.timedelta(days=i), x * 1.01, x * 0.99, x) for i, x in enumerate(closes)]

up = [100 + i for i in range(30)]                       # 꾸준히 상승
new, hi, why, note = SW.daily_check(110, 104, 104, 110, 3, bars([88 + i for i in range(30)]), c)
assert why is None and new >= 110 * 1.003 - 1e-9, (new, note)            # +1R(6) 넘음 → 본전
new, hi, why, note = SW.daily_check(110, 104, 104, 110, 5, bars([96 + i for i in range(30)]), c)
assert why is None and new > 110.4 and "추적" in note, (new, note)        # +2R 넘음 → 추적 손절
dn = [130 - i * 0.2 for i in range(29)] + [120]
new, hi, why, note = SW.daily_check(125, 118, 118, 126, 4, bars(dn), c)
assert why and "10일선" in why, why                                       # 10일선 아래 마감 → 정리
flat = [100 + (i % 3) * 0.3 for i in range(30)]
new, hi, why, note = SW.daily_check(100, 95, 95, 101, 10, bars(flat), c)
assert why and "거래일" in why, why                                       # 10일 지나도 +0.5R 못 감
new, hi, why, note = SW.daily_check(100, 95, 95, 101, 1, bars(flat), c)
assert why is None                                                         # 산 다음 날은 10일선으로 안 팜

# 3) 후보 날짜 확인
import bot.swing as S2
class R:
    status_code = 200
    def __init__(self, d): self.d = d
    def json(self): return self.d
import requests
orig = requests.get
requests.get = lambda url, timeout=0: R({"date": "2026-10-30", "regime": True, "picks": []})
assert S2.load_picks("x{m}", "KR", dt.date(2026, 11, 2), print) is not None      # 금요일 신호 → 월요일 장
assert S2.load_picks("x{m}", "KR", dt.date(2026, 11, 9), print) is None          # 너무 오래됨
assert S2.load_picks("x{m}", "KR", dt.date(2026, 10, 30), print) is None         # 같은 날(장 중 만든 것)은 안 씀
requests.get = orig

# 4) 매수: 국면 좋음 + 손절선 위로 열림 → 삼 / 손절선 아래 열림·급등 갭 → 안 삼 / 국면 나쁨 → 안 삼
cfg["market"] = "KR"
b = PaperBroker("KR", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
sent = []
prices = {"111111": 10000, "222222": 9000, "333333": 13000}
api = SimpleNamespace(prices=lambda syms: [{"symbol": s, "lastPrice": str(prices[s])} for s in syms if s in prices],
                      stocks=lambda syms: [{"name": "가", "securityType": "STOCK"}], warnings=lambda s: [],
                      candles=lambda sym, iv, n: [])
risk = SimpleNamespace(kill_switch=lambda: False, halted=None, capital=10_000_000)
bot = SimpleNamespace(cfg=cfg, m="KR", broker=b, risk=risk, api=api, stopped={}, syms={}, stream=None, universe=[],
                      log=print, notify=sent.append)
picks = [{"code": "111111", "name": "가재료", "setup": "ep", "close": 9900, "stop": 9300, "atr": 300, "score": 5, "reason": "큰 재료 갭"},
         {"code": "222222", "name": "나갭하락", "setup": "ep", "close": 9800, "stop": 9100, "atr": 300, "score": 4, "reason": "x"},
         {"code": "333333", "name": "다급등", "setup": "ep", "close": 11000, "stop": 10000, "atr": 300, "score": 3, "reason": "x"}]
bot.sw_picks = {"regime": False, "picks": picks}
assert M.Bot.swing_enter(bot, None) == 0 and not b.pos
bot.sw_picks = {"regime": True, "picks": picks}
spent = M.Bot.swing_enter(bot, None)
assert list(b.pos) == ["111111"], list(b.pos)
p = b.pos["111111"]
assert p.extra["sw"] and p.extra["carry"] and p.extra["setup"] == "sw_ep" and p.sl == 9300
assert 0.004 * 10_000_000 <= (p.entry - p.sl) * p.qty <= 0.0051 * 10_000_000, (p.qty, p.entry)
print("매수:", sent[-1])
assert b.open_count() == 0, "스윙이 단타 동시 보유 한도에 셈"

# 5) 장중엔 손절선을 안 바꿈(일봉 점검에서만), 손절선 닿으면 서버 손절처럼 팔림
ns, part, _ = b.manage(p, p.entry * 1.08)
assert ns is None and part is None
b.on_price("111111", 9200, None)
assert "111111" not in b.pos and b.closed[-1].exit_reason

# 6) 일봉 점검 → 10일선 깨지면 정리
b2 = PaperBroker("KR", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker = b2
prices["111111"] = 10000
M.Bot.swing_enter(bot, None)
p = b2.pos["111111"]
p.extra["sw_days"] = 3
d0 = dt.datetime(2026, 10, 1, 15, 30, tzinfo=M.TZ["KR"])
hist = [{"timestamp": (d0 + dt.timedelta(days=i)).isoformat(), "closePrice": str(10500 - i * 10), "highPrice": str(10600), "lowPrice": str(10300)}
        for i in range(20)]
api.candles = lambda sym, iv, n: hist
bot.swing_bars = lambda sym, last: M.Bot.swing_bars(bot, sym, last)
M.Bot.swing_daily(bot, p, 9600)
assert "111111" not in b2.pos and "10일선" in b2.closed[-1].exit_reason, b2.closed[-1].exit_reason
print("정리:", sent[-2])

# 7) 하락 국면엔 저점 매수(dip)만 사고, 짧게 관리 (10일선으로 안 팔고, 7거래일 지나면 정리)
b3 = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker, bot.m = b3, "US"
cfg["market"] = "US"
prices.update({"AAA": 100.0, "BBB": 50.0})
bot.sw_picks = {"regime": False, "picks": [
    {"code": "AAA", "name": "저점주", "setup": "dip", "close": 101, "stop": 94, "atr": 2, "score": -5, "reason": "하락장 저점 매수"},
    {"code": "BBB", "name": "신고가주", "setup": "hi52", "close": 50, "stop": 47, "atr": 1, "score": 3, "reason": "52주 신고가"}]}
M.Bot.swing_enter(bot, None)
assert list(b3.pos) == ["AAA"], list(b3.pos)
p = b3.pos["AAA"]
assert p.extra["sw_kind"] == "dip"
p.extra["sw_days"] = 3
hist2 = [{"timestamp": (d0 + dt.timedelta(days=i)).isoformat(), "closePrice": str(110 - i), "highPrice": str(111 - i), "lowPrice": str(109 - i)}
         for i in range(20)]
api.candles = lambda sym, iv, n: hist2
M.Bot.swing_daily(bot, p, 99)
assert "AAA" in b3.pos, "저점 매수가 10일선으로 팔림"
p.extra["sw_days"] = 7
M.Bot.swing_daily(bot, p, 99)
assert "AAA" not in b3.pos and "최대 보유" in b3.closed[-1].exit_reason, b3.closed[-1].exit_reason
print("✅ 일봉 스윙 시험 통과")
