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
c["red_flag_news"] = False          # 시험에선 인터넷 뉴스 안 봄 (아래 12번에서 따로 시험)
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
rp = c["risk_pct"] / 100
assert (rp - 0.001) * 10_000_000 <= (p.entry - p.sl) * p.qty <= (rp + 0.0001) * 10_000_000, (p.qty, p.entry)
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
bot.sw_picks = {"date": "2026-11-02", "regime": False, "picks": [
    {"code": "AAA", "name": "저점주", "setup": "dip", "close": 101, "stop": 94, "atr": 2, "score": -5, "reason": "하락장 저점 매수"},
    {"code": "BBB", "name": "신고가주", "setup": "hi52", "close": 50, "stop": 47, "atr": 1, "score": 3, "reason": "52주 신고가"}]}
M.Bot.swing_enter(bot, None)
assert not b3.pos and "장 마감 15분 전" in sent[-1], sent[-1]        # 저점 매수는 시가에 안 삼
api.candles = lambda sym, iv, n: [{"timestamp": dt.datetime.now(M.TZ["US"]).isoformat(), "closePrice": "99", "highPrice": "102", "lowPrice": "93"}]
M.Bot.swing_enter(bot, None, "close")
assert not b3.pos and "손절선" in sent[-1], sent[-1]                   # 장중 손절선(94) 아래까지 밀렸음 → 안 삼
api.candles = lambda sym, iv, n: [{"timestamp": dt.datetime.now(M.TZ["US"]).isoformat(), "closePrice": "99", "highPrice": "102", "lowPrice": "97"}]
M.Bot.swing_enter(bot, None, "close")
assert list(b3.pos) == ["AAA"], list(b3.pos)
assert b3.pos["AAA"].extra.get("carry_done")
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

# 8) 실적 발표 다루기
ec = c["earnings"]
today = dt.date(2026, 11, 2)                       # 월요일
base = {"code": "X", "name": "x", "setup": "dip", "close": 100, "stop": 90, "atr": 3, "score": 0, "reason": "r"}
# 실적 없음 → 그대로
assert SW.earn_plan(base, today, 100, 90, ec)[0] == "ok"
# 보유 기간(7거래일) 밖 → 그대로
far = {**base, "ev": {"earn": "2026-11-20 16:05", "react": "2026-11-23", "gap_avg": 0.08}}
assert SW.earn_plan(far, today, 100, 90, ec)[0] == "ok"
# 기간 안 + 전망치 하향 → 안 삼
dn = {**base, "ev": {"react": "2026-11-04", "gap_avg": 0.08, "eps0q": 0.95, "eps0q_30d": 1.0, "up30": 0, "down30": 5}}
pl, per, why = SW.earn_plan(dn, today, 100, 90, ec)
assert pl == "skip" and "내려가" in why, (pl, why)
# 기간 안 + 전망치 상향 → 들고 감, 1주 위험 = max(손절폭, 평균 갭×2)
up = {**base, "ev": {"react": "2026-11-04", "gap_avg": 0.08, "eps0q": 1.05, "eps0q_30d": 1.0, "up30": 6, "down30": 0}}
pl, per, why = SW.earn_plan(up, today, 100, 95, ec)
assert pl == "hold" and abs(per - 16) < 1e-9, (pl, per)
assert SW.qty_for(5000, 1.0, 20, 100, 100 - per, None) == 3              # $50 / $16 → 3주 (손절폭 기준이면 10주)
print("실적:", why)
assert SW.biz_days(dt.date(2026, 10, 30), dt.date(2026, 11, 2)) == 1 and SW.next_biz(dt.date(2026, 10, 30)) == dt.date(2026, 11, 2)

# 9) 매수 흐름: 전망치 하향 실적주는 건너뛰고, 업종 3종목 넘게 안 삼, 실적 끼면 수량 줄임
cfg["market"] = "US"
b4 = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
sent.clear()
prices.update({"T1": 100.0, "T2": 100.0, "T3": 100.0, "T4": 100.0, "T5": 100.0, "T6": 100.0})
mk = lambda code, ev: {"code": code, "name": code, "setup": "dip", "close": 100, "stop": 92, "atr": 3, "score": 0, "reason": "저점", "ev": ev}
tech = {"sector": "Technology", "industry": "Software"}
bot.broker, bot.m, bot.stopped = b4, "US", {}
bot.risk = SimpleNamespace(kill_switch=lambda: False, halted=None, capital=5000)
M.now = (lambda f: (lambda m: dt.datetime(2026, 11, 2, 9, 31, tzinfo=M.TZ["US"])))(M.now)
bot.sw_picks = {"date": "2026-11-02", "regime": False, "picks": [
    mk("T1", {**tech, "react": "2026-11-04", "gap_avg": 0.10, "eps0q": 1.0, "eps0q_30d": 1.0, "up30": 3, "down30": 0}),
    mk("T2", {**tech, "react": "2026-11-04", "gap_avg": 0.10, "eps0q": 0.9, "eps0q_30d": 1.0, "up30": 0, "down30": 4}),
    mk("T3", {**tech}), mk("T4", {**tech}), mk("T5", {**tech}),
    mk("T6", {"sector": "Healthcare"})]}
bot.swing_enter = lambda *a, **k: M.Bot.swing_enter(bot, *a, **k)
bot.swing_timing = lambda *a: M.Bot.swing_timing(bot, *a)
tbl = M.Bot.swing_table_text(bot)
print(tbl)
assert "✅ 매수 예정" in tbl and "T3" in tbl and "T2" in tbl and "같은 업종" in tbl and not b4.pos, tbl   # 판단표는 주문 안 함
plan_names = [r[0]["code"] for r in bot.sw_table if r[1] == "매수"]
M.Bot.swing_enter(bot, None, "close")
assert set(b4.pos) == set(plan_names), (plan_names, list(b4.pos))          # 판단표와 실제 매수가 같음
assert set(b4.pos) == {"T3", "T4", "T5", "T6"}, list(b4.pos)                # 실적 없는 것 먼저 → 업종 3개 차서 T1 은 업종 한도
assert any("T2" in m and "내려가" in m for m in sent) or "T2" not in b4.pos
assert b4.pos["T3"].qty == 9 and b4.pos["T3"].extra["sector"] == "Technology"   # 저점 매수 1.5%: $75 / $8
# 업종 한도 없을 때 실적 낀 T1 은 작게
b5 = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker = b5
bot.sw_picks["picks"] = [bot.sw_picks["picks"][0]]
M.Bot.swing_enter(bot, None, "close")
assert b5.pos["T1"].qty == 3 and b5.pos["T1"].extra["earn_hold"], b5.pos["T1"].qty   # $75 / $20(갭 10%×2)
print("매수(실적):", sent[-1])

# 10) 실적 전날: 줄여 사지 않은 종목이 쿠션 없고 갭 위험 크면 정리 / 줄여 산 종목은 들고 감
p1 = b5.pos["T1"]
p1.extra.update(earn_react="2026-11-03")
assert M.Bot.earn_presell(bot, p1, 100.0) is False                           # 살 때 감안해 작게 샀음
b5.enter("T9", "T9", 100.0, 40, 300.0, 92.0, "x", r=8)
p9 = b5.pos["T9"]
p9.extra.update(sw=True, sw_stop0=92.0, earn_react="2026-11-03", earn_gap=0.10)
assert M.Bot.earn_presell(bot, p9, 101.0) is True and "T9" not in b5.pos       # 40주×$101×20% ≈ $808 > $75
b5.enter("T8", "T8", 100.0, 40, 300.0, 92.0, "x", r=8)
p8 = b5.pos["T8"]
p8.extra.update(sw=True, sw_stop0=92.0, earn_react="2026-11-03", earn_gap=0.10)
assert M.Bot.earn_presell(bot, p8, 110.0) is False                            # +1R 넘게 벌고 있으면 들고 감
# 11) 이미 알린 후보(entry_at_since 이전 신호)는 원래대로 장 시작 때 삼
b6 = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker = b6
bot.sw_picks = {"date": "2026-10-30", "regime": False, "picks": [mk("T6", {"sector": "Healthcare"})]}
M.Bot.swing_enter(bot, None, "open")
assert list(b6.pos) == ["T6"] and b6.pos["T6"].qty == 9, list(b6.pos)        # 금요일 후보: 시가 매수, 1.5%
# 12) 치명적 악재 뉴스 / 시장보다 크게 뛴 갭
import bot.news as N
xml = """<rss><channel>
<item><title>CrowdStrike upgraded to Buy at Deutsche Bank - Investing.com</title><source>Investing.com</source><pubDate>Mon, 02 Nov 2026 12:00:00 GMT</pubDate></item>
<item><title>Acme Corp announces $500 million public offering - Reuters</title><source>Reuters</source><pubDate>Mon, 02 Nov 2026 11:00:00 GMT</pubDate></item>
<item><title>Other Co faces SEC probe - WSJ</title><source>WSJ</source><pubDate>Mon, 02 Nov 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""
fake_get = lambda u: SimpleNamespace(status_code=200, text=xml)
assert N.red_flags("CrowdStrike Holdings, Inc.", "US", get=fake_get) == []
assert N.red_flags("Acme Corp", "US", get=fake_get) == ["Acme Corp announces $500 million public offering"]
assert N.red_flags("Acme", "US", get=lambda u: (_ for _ in ()).throw(OSError("x"))) == []     # 연결 실패 → 매매 그대로
b7 = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker = b7
prices.update({"SPY": 101.0, "H1": 108.0, "H2": 104.0})
bot.sw_picks = {"date": "2026-11-02", "regime": True, "index": {"close": 100.0}, "picks": [
    {"code": "H1", "name": "H1", "setup": "hi52", "close": 100, "stop": 95, "atr": 2, "score": 3, "reason": "신고가", "ev": {"sector": "A"}},
    {"code": "H2", "name": "H2", "setup": "hi52", "close": 100, "stop": 95, "atr": 2, "score": 2, "reason": "신고가", "ev": {"sector": "B"}}]}
M.Bot.swing_enter(bot, None, "open")
assert list(b7.pos) == ["H2"], list(b7.pos)                      # H1: 시장 +1% 인데 +8% → 7%p 더 뜀 → 안 삼
assert any("H1" in m and "시장보다" in m for m in sent)
print("✅ 실적 다루기 시험 통과")
