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
c["brief_news"] = False
c["estimates_url"] = ""
KS = c["earnings"].pop("kr_season")  # 국장 실적 시즌 규칙은 날짜에 따라 달라져서 14번에서 따로 시험
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
assert set(b4.pos) == {"T3", "T4", "T6"}, list(b4.pos)                # 실적 없는 것 먼저 → 업종 2개 차서 T5·T1 은 업종 한도
assert any("T2" in m and "내려가" in m for m in sent) or "T2" not in b4.pos
assert b4.pos["T3"].qty == 3 and b4.pos["T3"].extra["sector"] == "Technology"   # 저점 매수 0.5%: $25 / $8
# 업종 한도 없을 때 실적 낀 T1 은 작게
b5 = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker = b5
bot.sw_picks["picks"] = [bot.sw_picks["picks"][0]]
M.Bot.swing_enter(bot, None, "close")
assert b5.pos["T1"].qty == 1 and b5.pos["T1"].extra["earn_hold"], b5.pos["T1"].qty   # $25 / $20(갭 10%×2)
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
assert list(b6.pos) == ["T6"] and b6.pos["T6"].qty == 3, list(b6.pos)        # 금요일 후보: 시가 매수, 0.5%
# 12) 치명적 악재 뉴스 / 시장보다 크게 뛴 갭
import bot.news as N
xml = """<rss><channel>
<item><title>CrowdStrike upgraded to Buy at Deutsche Bank - Investing.com</title><source>Investing.com</source><pubDate>Mon, 02 Nov 2026 12:00:00 GMT</pubDate></item>
<item><title>Acme Corp announces $500 million public offering - Reuters</title><source>Reuters</source><pubDate>Mon, 02 Nov 2026 11:00:00 GMT</pubDate></item>
<item><title>Hewlett Packard Enterprise closes Juniper deal after DOJ settlement - Reuters</title><source>Reuters</source><pubDate>Mon, 02 Nov 2026 11:00:00 GMT</pubDate></item>
<item><title>CrowdStrike fraud detection and accounting tools - Forbes</title><source>Forbes</source><pubDate>Mon, 02 Nov 2026 11:00:00 GMT</pubDate></item>
<item><title>Other Co faces SEC probe - WSJ</title><source>WSJ</source><pubDate>Mon, 02 Nov 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""
fake_get = lambda u: SimpleNamespace(status_code=200, text=xml)
assert N.red_flags("CrowdStrike Holdings, Inc.", "US", get=fake_get) == []           # 'fraud detection'·'accounting' 은 악재 아님
assert N.red_flags("Hewlett Packard Enterprise", "US", get=fake_get) == []            # 법무부(DOJ) 합의 기사도 악재 아님
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
# 13) 국장 재료 갭도 11/2 신호부터 마감 때 삼
cfg["market"] = "KR"
b8 = PaperBroker("KR", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker, bot.m = b8, "KR"
bot.risk = SimpleNamespace(kill_switch=lambda: False, halted=None, capital=10_000_000)
prices["111111"] = 10000
bot.sw_picks = {"date": "2026-11-03", "regime": True, "picks": [{"code": "111111", "name": "가재료", "setup": "ep", "close": 9900, "stop": 9300, "atr": 300, "score": 5, "reason": "큰 재료 갭", "ev": {"sector": "Industrials"}}]}
M.Bot.swing_enter(bot, None, "open")
assert not b8.pos and "마감 15분 전" in sent[-1], sent[-1]
api.candles = lambda sym, iv, n: [{"timestamp": dt.datetime.now(M.TZ["KR"]).isoformat(), "closePrice": "10000", "highPrice": "10100", "lowPrice": "9800"}]
M.Bot.swing_enter(bot, None, "close")
assert list(b8.pos) == ["111111"] and b8.pos["111111"].extra["sector"] == "Industrials", list(b8.pos)
# 14) SEC 공시 악재 / 국장 실적 시즌 / 판단표 미리 보기
subs = {"filings": {"recent": {"form": ["8-K", "4", "8-K", "NT 10-Q"], "filingDate": ["2026-11-02", "2026-11-01", "2026-10-31", "2026-10-01"],
                               "items": ["4.02,9.01", "", "5.02", ""]}}}
def sec_get(u):
    js = {"0": {"cik_str": 1234, "ticker": "ACME"}} if "company_tickers" in u else subs
    return SimpleNamespace(status_code=200, json=lambda: js)
N._CIK.update(t=0, map={})
fl = N.sec_flags("ACME", get=sec_get, today=dt.date(2026, 11, 2))
assert fl == ["SEC 공시 2026-11-02 8-K 4.02: 과거 재무제표 신뢰 불가(회계 문제)"], fl          # 5.02(임원 변경)·오래된 NT 10-Q 는 안 걸림
assert N.sec_flags("NOPE", get=sec_get) == []
ec["kr_season"] = KS
kr = {"code": "1", "name": "x", "setup": "ep", "close": 100, "stop": 95, "ev": {}}
pl, per, why = SW.earn_plan(kr, dt.date(2026, 11, 3), 100, 95, ec, "KR")
assert pl == "hold" and abs(per - 16) < 1e-9 and "시즌" in why, (pl, per, why)
assert SW.earn_plan(kr, dt.date(2026, 12, 3), 100, 95, ec, "KR")[0] == "ok"
assert SW.earn_plan(kr, dt.date(2026, 11, 3), 100, 95, ec, "US")[0] == "ok"
# 미리 보기: 새 후보가 나오면 한 번만, 지난 장 후보면 안 보냄
root = Path(tempfile.mkdtemp())
sent.clear()
pv = SimpleNamespace(cfg=cfg, m="US", api=api, risk=SimpleNamespace(kill_switch=lambda: False, halted=None, capital=5000),
                     stopped={}, syms={}, stream=None, universe=[], log=print, notify=sent.append, sw_picks=None, broker=None,
                     dir=root, mode="live", open_t=dt.datetime(2026, 11, 3, 9, 30, tzinfo=M.TZ["US"]))
pv.swing_on = lambda: ["hi52", "dip"]
pv.load_stopped = lambda: setattr(pv, "stopped", {})
pv.swing_table_text = lambda: M.Bot.swing_table_text(pv)
pk = {"date": "2026-10-30", "regime": False, "index": {"close": 100, "ma50": 110}, "picks": [mk("T6", {"sector": "Healthcare"})]}
orig_lp = SW.load_picks
SW.load_picks = lambda *a, **k: json.loads(json.dumps(pk))
M.Bot.swing_preview(pv)
assert not sent, sent                                     # 금요일 후보 → 화요일 장엔 지난 것
pk["date"] = "2026-11-02"
M.Bot.swing_preview(pv)
assert len(sent) == 1 and "미리 보기" in sent[0] and "T6" in sent[0] and pv.broker is None, sent
M.Bot.swing_preview(pv)
assert len(sent) == 1                                     # 한 번만
SW.load_picks = orig_lp
print(sent[0])
print("✅ 실적 다루기 시험 통과")

# 15) 계좌 보유 이어받기 + 추가 매수(불타기)
cfg["market"] = "KR"
bk = PaperBroker("KR", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bk.api = object()
armed = []
bk._arm_exit = lambda p: armed.append(p.symbol)
sent.clear()
kd0 = dt.date.today() - dt.timedelta(days=40)
up_bars = [{"timestamp": dt.datetime.combine(kd0 + dt.timedelta(days=i), dt.time(15, 30), M.TZ["KR"]).isoformat(),
            "closePrice": str(4000 + i * 40), "highPrice": str(4050 + i * 40), "lowPrice": str(3950 + i * 40)} for i in range(38)]
hold_items = [{"symbol": "000001", "name": "피앤에스로보틱스", "marketCountry": "KR", "quantity": "122", "averagePurchasePrice": "4522", "lastPrice": "5510"},
              {"symbol": "069500", "name": "KODEX 200", "marketCountry": "KR", "quantity": "5", "averagePurchasePrice": "50000", "lastPrice": "51000"},
              {"symbol": "AAPL", "name": "Apple", "marketCountry": "US", "quantity": "1", "averagePurchasePrice": "200", "lastPrice": "210"}]
ka = SimpleNamespace(holdings=lambda: {"items": hold_items}, stocks=lambda s: [{"name": "피앤에스로보틱스", "securityType": "STOCK"}],
                     candles=lambda sym, iv, n: up_bars, prices=lambda s: [], warnings=lambda s: [])
kb = SimpleNamespace(cfg=cfg, m="KR", broker=bk, api=ka, risk=SimpleNamespace(capital=10_000_000), syms={}, log=print, notify=sent.append)
kb.swing_bars = lambda sym, last: M.Bot.swing_bars(kb, sym, last)
M.Bot.adopt_holdings(kb)
assert list(bk.pos) == ["000001"] and armed == ["000001"], list(bk.pos)            # ETF·미장 종목은 안 이어받음
pa = bk.pos["000001"]
assert pa.qty == 122 and pa.entry == 4522 and pa.extra["sw"] and pa.sl >= 4522, (pa.sl,)  # 수익 중 → 손절선 최소 본전
print(sent[-1])
M.Bot.adopt_holdings(kb)
assert len(sent) == 1                                                                # 두 번 이어받지 않음
# 추가 매수: 시장 나쁨이면 안 함 / 좋으면 절반, 한 번만
kb.sw_regime = False
bars_now = M.Bot.swing_bars(kb, "000001", 5600)
assert M.Bot.swing_add_on(kb, pa, bars_now, 5600) is False
kb.sw_regime = True
assert M.Bot.swing_add_on(kb, pa, bars_now, 5600) is True and pa.qty == 183, pa.qty  # 122 + 61
assert M.Bot.swing_add_on(kb, pa, bars_now, 5700) is False                          # 한 번만
print(sent[-1])
# 잠근 종목은 다음 날 다시 켜도 이어받음
from bot.broker import Position
bl = PaperBroker("KR", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
lp = Position("000002", "잠금주", 10, 1000, 3000, 900, "x", "x", state="open", extra={"locked": True})
(bl.dir / "state.json").write_text(json.dumps({"date": "2026-10-01", "positions": [lp.__dict__]}), encoding="utf-8")
bl.restore()
assert "000002" in bl.pos, "잠근 종목이 빠짐"
print("✅ 계좌 보유 이어받기·추가 매수 시험 통과")

# 16) 보유 종목 브리핑: 오늘 봉·거래량·20일선·애널리스트·실적일·뉴스
import bot.swing as SW3
bd = [(dt.date(2026, 10, 1) + dt.timedelta(days=i), 100, 102, 98, 100, 1000.0) for i in range(25)]
bd.append((dt.date(2026, 11, 4), 96, 105, 95, 104, 3000.0))           # 오늘: 양봉 +4%, 거래량 3배
txt = SW3.brief_text("US", bd, 104, {"targetMeanPrice": "130", "recommendationMean": "1.8", "numberOfAnalystOpinions": "29",
                                     "eps_0q_current": "1.05", "eps_0q_30daysAgo": "1.0"},
                     "2026-11-25", ["Dell rebounds on AI backlog", "Intel warns on PCs"], dt.date(2026, 11, 4), lambda v: f"${v:,.2f}")
print(txt)
assert "양봉 +4.0%" in txt and "거래량 평소 3.0배" in txt and "목표가 평균 $130.00 (+25%)" in txt and "+5.0%" in txt
assert "11/25" in txt and "Dell rebounds" in txt and "20일선 위" in txt
kt = SW3.brief_text("KR", bd, 104, {"cs_priceTargetMean": "120", "cs_recommMean": "4.0", "foreignerPureBuyQuant": "-1000",
                                    "organPureBuyQuant": "2500"}, None, [], dt.date(2026, 11, 4), lambda v: f"{v:,.0f}원")
assert "증권사 목표가 평균 120원 (+15%)" in kt and "외국인 -1,000주" in kt and "기관 +2,500주" in kt, kt
assert SW3.brief_text("US", [], 0, None, None, [], dt.date(2026, 11, 4)) == ""
xmlb = """<rss><channel><item><title>Dell rebounds on AI backlog - Reuters</title><source>Reuters</source><pubDate>Tue, 03 Nov 2026 12:00:00 GMT</pubDate></item>
<item><title>Intel warns on PCs - WSJ</title><source>WSJ</source><pubDate>Tue, 03 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
assert N.recent_news("Dell Technologies Inc.", "US", get=lambda u: SimpleNamespace(status_code=200, text=xmlb)) == ["Dell rebounds on AI backlog"]
print("✅ 보유 종목 브리핑 시험 통과")

# 17) 매수 직전 종합 점검: 목표가가 지금보다 낮고 의견도 나쁘면 안 삼 / 주의는 표시
cfg["market"] = "US"
cb = SimpleNamespace(cfg=cfg, m="US", api=SimpleNamespace(candles=lambda *a: []), log=print)
cb._est_day, cb._est = M.now("US").date(), {"BAD": {"targetMeanPrice": "80", "recommendationMean": "3.4"},
                                              "OK": {"targetMeanPrice": "95", "recommendationMean": "1.9"}}
why, txt, warn = M.Bot.cand_check(cb, {"code": "BAD", "name": "Bad", "close": 100, "ev": {}}, 100)
assert why and "목표가" in why, why
why, txt, warn = M.Bot.cand_check(cb, {"code": "OK", "name": "Ok", "close": 100, "ev": {}}, 100)
assert why is None and any("목표가" in w for w in warn) and "목표가 평균 $95.00 (-5%)" in txt, (why, warn, txt)
cfg["market"] = "KR"
ck = SimpleNamespace(cfg=cfg, m="KR", api=SimpleNamespace(candles=lambda *a: []), log=print)
ck._est_day, ck._est = M.now("KR").date(), {"111111": {"cs_priceTargetMean": "15000", "cs_recommMean": "4.0",
                                                       "foreignerPureBuyQuant": "-10", "organPureBuyQuant": "-5"}}
why, txt, warn = M.Bot.cand_check(ck, {"code": "111111", "name": "가", "close": 10000, "ev": {}}, 10000)
assert why is None and "외국인·기관 동반 순매도" in warn and "+50%" in txt, (warn, txt)
print("✅ 매수 직전 종합 점검 시험 통과")

# 18) 스윙 전체 열린 손실 한도 3%: 이미 손절선까지 손실이 큰 종목들을 들고 있으면 새로 안 삼
cfg["market"] = "US"
b9 = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
for code in ("Q1", "Q2"):
    pq = b9.enter(code, code, 100.0, 9, 300.0, 92.0, "x", r=8)      # 손절선까지 9주 × $8 = $72 씩
    pq.state = "open"; pq.extra.update(sw=True)
bot.broker, bot.m = b9, "US"
bot.risk = SimpleNamespace(kill_switch=lambda: False, halted=None, capital=5000)
sent.clear()
prices.update({"H3": 100.0})
bot.sw_picks = {"date": "2026-11-05", "regime": True, "index": {"close": 100.0},
                "picks": [{"code": "H3", "name": "H3", "setup": "hi52", "close": 100, "stop": 95, "atr": 2, "score": 3, "reason": "신고가", "ev": {"sector": "C"}}]}
M.Bot.swing_enter(bot, None, "open")
assert "H3" not in b9.pos and any("열린 손실" in m for m in sent), sent           # $144 + 신규 $50 > $150(3%)
print("✅ 열린 손실 한도 시험 통과")
