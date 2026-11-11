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
cfg["daily_swing"]["ai_required_other"] = False      # 시험: AI 키 없이 돌리므로 판독 없으면 안 사기는 따로 시험
cfg["daily_swing"].update(stop_by_market={}, risk_pct=1.0, risk_by_grade={"A": 1.0, "B": 0.6}, strong={"enabled": False}, profit_lock={"enabled": False})   # 시험 숫자는 1% 위험·신호 손절선 기준
cfg["daily_swing"]["extra_slots"] = {}                # 시험: 따로 자리는 마지막에 따로 시험
c = cfg["daily_swing"]
c["need_target"] = False   # 시험 종목엔 목표가가 없음 (목표가 필수 규칙은 아래에서 따로 시험)
c["min_chart_q"] = -99
c["red_flag_news"] = False          # 시험에선 인터넷 뉴스 안 봄 (아래 12번에서 따로 시험)
c["brief_news"] = False
c["min_forward_score"] = -99         # 기존 시험은 점수 문턱 없이 (아래 19번에서 따로 시험)
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
cfg["daily_swing"]["any_regime_by_market"] = {"KR": ["fs"], "US": ["fs"]}      # 이 시험은 '신고가는 시장 좋을 때만' 설정 기준
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
c["max_open_risk_by_market"] = {}                      # 이 시험은 3% 한도 기준
M.Bot.swing_enter(bot, None, "open")
assert "H3" not in b9.pos and any("열린 손실" in m for m in sent), sent           # $144 + 신규 $50 > $150(3%)
print("✅ 열린 손실 한도 시험 통과")

# 19) 현재·미래 점수
bars_up = [(dt.date(2026, 10, 1) + dt.timedelta(days=i), 100, 101, 99, 100, 1000.0) for i in range(24)]
bars_up.append((dt.date(2026, 11, 4), 100, 106, 99.5, 105.5, 2500.0))    # 양봉·고가 근처·거래량 2.5배·20일선 위
good = {"targetMeanPrice": "130", "recommendationMean": "1.7", "forwardEps": "6", "trailingEps": "5",
        "eps_0q_current": "1.03", "eps_0q_30daysAgo": "1.0"}
fs, fw = SW.forward_score("US", bars_up, 105.5, good, {}, 1)
print(fs, fw)
assert fs >= 8 and SW.grade_of(fs) == "A", (fs, fw)
bars_dn = bars_up[:-1] + [(dt.date(2026, 11, 4), 100, 101, 93, 93.5, 2500.0)]  # 음봉·저가 마감·거래량 실린 하락·20일선 아래
bad = {"targetMeanPrice": "90", "recommendationMean": "3.2", "forwardEps": "4", "trailingEps": "5",
       "eps_0q_current": "0.95", "eps_0q_30daysAgo": "1.0"}
fs2, fw2 = SW.forward_score("US", bars_dn, 93.5, bad, {}, -1)
assert fs2 <= -6 and SW.grade_of(fs2) == "C", (fs2, fw2)
ks, kw = SW.forward_score("KR", bars_up, 105.5, {"cs_priceTargetMean": "130", "cs_recommMean": "4.0", "foreignerPureBuyQuant": "5",
                                                  "organPureBuyQuant": "3", "ti_cnsEps": "1200", "ti_eps": "1000"}, {}, 0)
assert ks >= 7 and any("외국인·기관 동반 순매수" in w for w in kw), (ks, kw)
# 문턱: 점수 낮으면 안 삼
cfg["daily_swing"]["min_forward_score"] = 2
cfg["market"] = "US"
cz = SimpleNamespace(cfg=cfg, m="US", api=SimpleNamespace(candles=lambda *a: [
    {"timestamp": dt.datetime.combine(b[0], dt.time(16, 0), M.TZ["US"]).isoformat(), "openPrice": str(b[1]), "highPrice": str(b[2]),
     "lowPrice": str(b[3]), "closePrice": str(b[4]), "volume": str(b[5])} for b in bars_dn]), log=print)
cz._est_day, cz._est = M.now("US").date(), {"ZZ": bad}
why, txt, warn = M.Bot.cand_check(cz, {"code": "ZZ", "name": "Zz", "close": 93.5, "ev": {}}, 93.5)
assert why and "근거 부족" in why, why
cfg["daily_swing"]["min_forward_score"] = -99
print("✅ 현재·미래 점수 시험 통과")

# 20) 현재·미래 점수 방식(fs): 시장 상태와 상관없이 후보, 보유 중 점수가 0 이하로 떨어지면 정리
cfg["market"] = "US"
c["any_regime"] = ["fs"]
bf = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker, bot.m = bf, "US"
bot.risk = SimpleNamespace(kill_switch=lambda: False, halted=None, capital=5000)
prices.update({"F1": 100.0})
api.candles = lambda sym, iv, n: [{"timestamp": dt.datetime.now(M.TZ["US"]).isoformat(), "closePrice": "100", "highPrice": "101", "lowPrice": "99"}]
bot.sw_picks = {"date": "2026-11-06", "regime": False, "index": {"close": 100.0},
                "picks": [{"code": "F1", "name": "F1", "setup": "fs", "close": 100, "stop": 93, "atr": 3, "score": 5, "reason": "현재·미래 점수 +5", "ev": {"sector": "S"}}]}
M.Bot.swing_enter(bot, None, "close")
assert "F1" in bf.pos and bf.pos["F1"].extra["setup"] == "sw_fs", list(bf.pos)            # 시장 나쁨이어도 점수로 삼
pf = bf.pos["F1"]
pf.extra["sw_days"] = 2
hist3 = [{"timestamp": (d0 + dt.timedelta(days=i)).isoformat(), "closePrice": str(100 + i * 0.1), "highPrice": str(101 + i * 0.1), "lowPrice": str(99 + i * 0.1)} for i in range(20)]
api.candles = lambda sym, iv, n: hist3
c["exit_forward_score"] = 0
orig_hs = M.Bot.hold_score
M.Bot.hold_score = lambda self, p, last=None: (-2, ["－최근 봉 음봉", "－이익 전망 하향 중"], "브리핑")
M.Bot.swing_daily(bot, pf, 101.5)
M.Bot.hold_score = orig_hs
assert "F1" not in bf.pos and "근거가 약해짐" in bf.closed[-1].exit_reason, bf.closed[-1].exit_reason
print("✅ 현재·미래 점수 방식 시험 통과")

# 21) 장기 하락 종목: 최근 실적·전망 상향·호재 기사 같은 새 근거 포함 2개 이상이면 살 수 있음
assert SW.turnaround(["＋이익 성장 예상 +34%", "＋외국인·기관 동반 순매수"])[0] is False          # 오래된 전망·수급만: 부족
assert SW.turnaround(["＋직전 실적 예상치 +12% 상회", "＋외국인·기관 동반 순매수"])[0] is True
fs3, fw3 = SW.forward_score("US", [], 100, {"last_surprise": 12}, {"eps_yoy": 0.3}, 0)
assert any("상회" in w for w in fw3) and any("1년 전보다" in w for w in fw3), fw3
print("✅ 장기 하락 반전 시험 통과")

# 22) 애널리스트 관점 보강: 5일 수급·컨센서스 4주 변화·밸류에이션·공시 영업이익·AI 판독·상관·등급별 크기·목표가 익절
fs4, fw4 = SW.forward_score("KR", [], 100, {"foreigner5d": 1000, "organ5d": 50, "foreignerPureBuyQuant": -1, "organPureBuyQuant": -1,
                                           "ti_cnsPer": 70}, {"tgt_chg": 0.08, "eps_chg": -0.05, "op_yoy": 0.45, "op_period": "2026 3분기",
                                                               "ai": {"adj": 1, "view": "긍정", "catalyst": "수주"}}, 0)
assert any("최근 5일 외국인·기관 동반 순매수" in w for w in fw4), fw4             # 1일이 아니라 5일 누적으로 봄
assert any("목표가 4주간 +8%" in w for w in fw4) and any("이익 전망 4주간 -5% 하향" in w for w in fw4), fw4
assert any("비쌈(PER 70" in w for w in fw4) and any("공시 영업이익" in w for w in fw4) and any("AI 판독" in w for w in fw4), fw4
fs5, fw5 = SW.forward_score("US", [], 100, {"forwardPE": 12, "forwardEps": 6, "trailingEps": 5}, {}, 0)
assert any("이익 대비 쌈" in w for w in fw5), fw5
assert SW.turnaround(["＋목표가 4주간 +8% 상향", "＋이익 성장 예상 +40%"])[0] is True
assert abs(SW.corr([1, 2, 3, 4], [2, 4, 6, 8]) - 1) < 1e-9 and SW.corr([1, 1], [1, 2]) is None
assert SW.cons_target("KR", {"cs_priceTargetMean": "52000"}) == 52000 and SW.cons_target("US", {}) is None
# 목표가 도달 → 절반 익절 + 나머지 본전 이상 손절
bt = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker = bt
pt = bt.enter("T1", "T1", 100, 10, 200, 92, "시험")
pt.extra.update(sw=True, carry=True, sw_days=3, tgt=110.0)
c["target_take"] = {"enabled": True, "frac": 0.5, "near_pct": 0}
_pt0 = c.get("profit_take")
c["profit_take"] = {"enabled": False}
c["exit_forward_score"] = None
c["exit_ma"] = 0
c["time_stop_days"] = 99
hist4 = [{"timestamp": (d0 + dt.timedelta(days=i)).isoformat(), "closePrice": str(100 + i * 0.6), "highPrice": str(101 + i * 0.6), "lowPrice": str(99 + i * 0.6)} for i in range(20)]
api.candles = lambda sym, iv, n: hist4
M.Bot.hold_score = lambda self, p, last=None: (3, ["＋최근 봉 양봉"], "")
M.Bot.swing_daily(bot, pt, 111.4)
M.Bot.hold_score = orig_hs
assert pt.qty == 5 and pt.extra.get("tgt_done") and pt.sl >= 100, (pt.qty, pt.sl, pt.extra)
assert bt.closed[-1].exit_reason.startswith("애널리스트 목표가"), bt.closed[-1].exit_reason
# +4% 넘으면 1/3 익절 + 나머지 손절선 본전 위로 (목표가 전이라도), 한 번만
c["profit_take"] = {"enabled": True, "pct": 4, "frac": 0.34}
M.Bot.hold_score = lambda self, p, last=None: (3, ["＋최근 봉 양봉"], "")
api.candles = lambda sym, iv, n: [{"timestamp": (d0 + dt.timedelta(days=i)).isoformat(), "closePrice": str(95 + i * 0.5), "highPrice": str(96 + i * 0.5), "lowPrice": str(94 + i * 0.5)} for i in range(20)]
pp = bt.enter("T2", "T2", 100, 9, 200, 92, "시험")
pp.extra.update(sw=True, carry=True, sw_days=3, tgt=130.0)
M.Bot.swing_daily(bot, pp, 104.6)
assert pp.qty == 6 and pp.extra.get("pt_done") and pp.sl >= 100, (pp.qty, pp.sl)
assert bt.closed[-1].exit_reason.startswith("+4.6% 수익 — 일부 익절"), bt.closed[-1].exit_reason
M.Bot.swing_daily(bot, pp, 105.0)
assert pp.qty == 6, "한 번만"
p1 = bt.enter("T3", "T3", 100, 1, 200, 92, "시험")
p1.extra.update(sw=True, carry=True, sw_days=3)
M.Bot.swing_daily(bot, p1, 104.6)
assert p1.qty == 1 and p1.sl >= 100 and p1.extra.get("pt_done"), (p1.qty, p1.sl)
M.Bot.hold_score = orig_hs
c["profit_take"] = _pt0
c["exit_forward_score"] = 0
_, fw6 = SW.forward_score("KR", [], 100, {}, {"op_yoy": 0.5, "op_period": "2026 반기", "op_stale": True, "prov_date": "20261027"}, 0)
assert not any(w.startswith("＋공시 영업이익") for w in fw6) and any("지난 분기 숫자" in w for w in fw6), fw6
print("✅ 애널리스트 관점 보강 시험 통과")

# 23) AI 2단계: 1차는 +1 까지만, 최종은 −3~+2·치명적 위험이면 매수 금지 → 봇이 안 삼
import os as _os
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import extra_sources as XS
_os.environ["ANTHROPIC_API_KEY"] = "test"
XS._claude = lambda model, prompt, mt, code=None: {"view": "긍정", "catalyst": "c", "risk": "r", "adj": 5}
assert XS.ai_read("KR", {"code": "1", "ev": {}}, [])["adj"] == 1
XS._claude = lambda model, prompt, mt, code=None: {"view": "부정", "catalyst": "c", "risk": "r", "adj": -9, "veto": True, "veto_reason": "유상증자 발표"}
fin = XS.ai_final("KR", {"code": "1", "ev": {}}, [], {})
assert fin["adj"] == -3 and fin["veto"] == "유상증자 발표" and fin["model"] == "claude-opus-5-5", fin
del _os.environ["ANTHROPIC_API_KEY"]
bv = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker, bot.m = bv, "US"
prices.update({"V1": 100.0})
api.candles = lambda sym, iv, n: [{"timestamp": dt.datetime.now(M.TZ["US"]).isoformat(), "closePrice": "100", "highPrice": "101", "lowPrice": "99"}]
bot.sw_picks = {"date": "2026-11-06", "regime": True, "index": {"close": 100.0},
                "picks": [{"code": "V1", "name": "V1", "setup": "fs", "close": 100, "stop": 93, "atr": 3, "score": 9, "reason": "x",
                           "ev": {"sector": "Z", "ai": {"adj": -3, "veto": "실적 쇼크", "final": True}}}]}
sent.clear()
M.Bot.swing_enter(bot, None, "close")
assert "V1" not in bv.pos and any("AI 최종 판독 매수 금지: 실적 쇼크" in t for t in sent), sent
print("✅ AI 2단계 시험 통과")

# 24) 휴대폰 앱 '봇으로 사기': 스윙 규칙 그대로 점검 — 통과하면 사고, AI 매수 금지면 이유 알림 후 안 삼, 원래 후보는 그대로
bm = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker, bot.m = bm, "US"
prices.update({"M1": 100.0, "M2": 100.0})
api.candles = lambda sym, iv, n: [{"timestamp": dt.datetime.now(M.TZ["US"]).isoformat(), "closePrice": "100", "highPrice": "101", "lowPrice": "99"}]
keep = {"date": "2026-11-06", "regime": True, "index": {"close": 100.0}, "picks": [{"code": "ZZ", "name": "원래후보", "setup": "fs"}]}
bot.sw_picks = keep
sent.clear()
M.Bot.manual_buy(bot, {"code": "M2", "name": "M2", "setup": "fs", "close": 100, "stop": 93, "atr": 3, "score": 6, "reason": "앱",
                       "ev": {"sector": "Y", "ai": {"veto": "희석 발표", "adj": -3}}}, None)
assert "M2" not in bm.pos and any("M2 건너뜀" in t and "희석" in t for t in sent), sent
M.Bot.manual_buy(bot, {"code": "M1", "name": "M1", "setup": "fs", "close": 100, "stop": 93, "atr": 3, "score": 6, "reason": "앱", "ev": {"sector": "X"}}, None)
assert "M1" in bm.pos and bot.sw_picks is keep, (list(bm.pos), bot.sw_picks)
print("✅ 앱 봇으로 사기 시험 통과")

# 25) 손익비·과열·AI 부정/중립: 목표가까지 남은 폭이 손절 폭보다 작으면 감점, AI 부정이면 안 삼, 중립이면 작게
assert SW.rr_score(0.35, 0.10)[0] == 2 and SW.rr_score(0.25, 0.10)[0] == 1 and SW.rr_score(0.17, 0.10)[0] == 0 and SW.rr_score(0.12, 0.10)[0] == -1
# 실적 앞둔 종목: 판정 좋으면(점수 6↑·AI 부정 아님·전망치 하향 아님) 순서 그대로 + 갭 1.5배 기준으로 덜 줄임
_ec = {"enabled": True, "window": {"ep": 10}, "default_gap": 0.07, "gap_mult": 2, "conf_score": 6, "conf_gap_mult": 1.5}
_x = {"setup": "ep", "score": 8, "ev": {"react": "2026-11-12", "gap_avg": 0.08, "ai": {"view": "중립"}}}
_pl, _per, _t = SW.earn_plan(_x, dt.date(2026, 11, 9), 100, 95, _ec, "KR")
assert _pl == "early" and abs(_per - 12) < 1e-9 and "미리" in _t, (_pl, _per, _t)
assert SW.earn_plan({**_x, "score": 5}, dt.date(2026, 11, 9), 100, 95, _ec, "KR")[0] == "hold"
assert SW.earn_plan({**_x, "ev": {**_x["ev"], "ai": {"view": "부정"}}}, dt.date(2026, 11, 9), 100, 95, _ec, "KR")[0] == "hold"
assert SW.earn_plan({**_x, "ev": {**_x["ev"], "eps0q": 0.9, "eps0q_30d": 1.0}}, dt.date(2026, 11, 9), 100, 95, _ec, "KR")[0] == "skip"
assert SW.rr_score(0.05, 0.10)[0] == -2 and SW.rr_score(None, 0.1)[0] == 0
hot = [(f"d{i}", 100, 101, 99, 100, 1000) for i in range(19)] + [("d19", 100, 150, 99, 150, 1000)]
assert any("단기 과열" in w for w in SW.forward_score("US", hot, 150, {}, {}, 0)[1])
ba = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker, bot.m = ba, "US"
prices.update({"N1": 100.0, "N2": 100.0})
api.candles = lambda sym, iv, n: [{"timestamp": dt.datetime.now(M.TZ["US"]).isoformat(), "closePrice": "100", "highPrice": "101", "lowPrice": "99"}]
bot.sw_picks = {"date": "2026-11-06", "regime": True, "index": {"close": 100.0}, "picks": [
    {"code": "N1", "name": "N1", "setup": "fs", "close": 100, "stop": 93, "atr": 3, "score": 9, "reason": "x", "ev": {"sector": "P", "ai": {"view": "부정", "adj": -1}}},
    {"code": "N2", "name": "N2", "setup": "fs", "close": 100, "stop": 93, "atr": 3, "score": 9, "reason": "x", "ev": {"sector": "Q", "ai": {"view": "중립", "adj": 0}}}]}
sent.clear()
M.Bot.swing_enter(bot, None, "close")
assert "N1" not in ba.pos and any("AI 판독이 부정적" in t for t in sent), sent
assert "N2" in ba.pos and ba.pos["N2"].extra.get("risk_pct") == c["risk_by_grade"]["B"], ba.pos["N2"].extra.get("risk_pct")
print("✅ 손익비·AI 부정/중립 시험 통과")

# 26) 손익비 1 미만이면 점수가 높아도 안 삼
br2 = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker, bot.m = br2, "US"
prices.update({"R1": 100.0})
api.candles = lambda sym, iv, n: [{"timestamp": dt.datetime.now(M.TZ["US"]).isoformat(), "closePrice": "100", "highPrice": "101", "lowPrice": "99"}]
bot._est_day, bot._est = M.now("US").date(), {"R1": {"targetMeanPrice": "103"}}
c["estimates_url"] = "x"
bot.sw_picks = {"date": "2026-11-06", "regime": True, "index": {"close": 100.0},
                "picks": [{"code": "R1", "name": "R1", "setup": "fs", "close": 100, "stop": 93, "atr": 3, "score": 9, "reason": "x", "ev": {"sector": "R"}}]}
sent.clear()
M.Bot.swing_enter(bot, None, "close")
c["estimates_url"] = ""
assert "R1" not in br2.pos and any("손익비" in t for t in sent), sent
print("✅ 손익비 하한 시험 통과")

# 27) 장중 다시 고르기: 오늘 것이면 후보를 바꾸고, 시장이 -2% 넘게 빠지는 날엔 새로 안 삼
import requests as _rq
_orig_get = _rq.get
c["live_picks_url"] = "https://x/{m}.json"
c["live_guard_index_pct"] = -2.0
bot.m = "US"
base = {"date": "2026-11-06", "regime": True, "index": {"close": 100}, "picks": [{"code": "OLD", "name": "옛후보", "setup": "fs"}]}
live = {"date": M.now("US").date().isoformat(), "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "index": {"today_pct": -0.5}, "picks": [{"code": "NEW", "name": "새후보", "setup": "fs"}], "added": ["NEW"], "dropped": ["OLD"]}
_rq.get = lambda url, timeout=None: SimpleNamespace(json=lambda: live)
bot.sw_picks = dict(base)
sent.clear()
assert M.Bot.live_refresh(bot) and bot.sw_picks["picks"][0]["code"] == "NEW" and any("새로: 새후보" in t and "빠짐: 옛후보" in t for t in sent), sent
live["index"]["today_pct"] = -2.4
bot.sw_picks = dict(base)
assert M.Bot.live_refresh(bot) and bot.sw_picks["picks"] == [] and any("새로 안 사요" in t for t in sent)
live["date"] = "2020-01-01"
bot.sw_picks = dict(base)
assert not M.Bot.live_refresh(bot) and bot.sw_picks["picks"][0]["code"] == "OLD"                 # 오늘 것이 아니면 밤 후보 그대로
_rq.get = _orig_get
c["live_picks_url"] = ""
print("✅ 장중 다시 고르기 시험 통과")

# 확신 종목(점수 8↑·AI 부정 아님·손익비 2↑)은 장 시작 후 바로, 나머지는 마감 무렵
_b = M.Bot.__new__(M.Bot)
_b.cfg = {"daily_swing": {"entry_at_since": "2026-11-02", "entry_at": {"fs": "close"}, "early_conf": {"enabled": True, "score": 8, "min_rr": 2.0}}}
_d = {"date": "2026-11-09"}
assert M.Bot.swing_timing(_b, {"setup": "fs", "score": 9, "rr": {"rr": 3.0}, "ev": {"ai": {"view": "중립"}}}, _d) == "open"
assert M.Bot.swing_timing(_b, {"setup": "fs", "score": 7, "rr": {"rr": 3.0}}, _d) == "close"
assert M.Bot.swing_timing(_b, {"setup": "fs", "score": 9, "rr": {"rr": 1.8}}, _d) == "close"
assert M.Bot.swing_timing(_b, {"setup": "fs", "score": 9, "ev": {"ai": {"view": "부정"}}}, _d) == "close"
print("✅ 확신 종목 아침 매수 시험 통과")

# 급등 직후 추격 금지 · 목표가 없는 종목은 안 삼(need_target)
_b = [(dt.date(2026, 9, 1) + dt.timedelta(days=i), 100, 101, 99, 100, 1000) for i in range(40)] + [(dt.date(2026, 10, 15), 100, 130, 100, 128, 9000)] + \
     [(dt.date(2026, 10, 16) + dt.timedelta(days=i), 128, 130, 126, 128, 2000) for i in range(5)]
assert SW.spike_note(_b, 128) and "급등" in SW.spike_note(_b, 128)
assert SW.spike_note(_b[:40], 100) is None
print("✅ 급등 직후 추격 금지 시험 통과")

# 차트 모양 점수: 건강한 상승 > 0, 하루 급등 뒤 버티기·하락 ≤ -2
import random as _rd  # noqa: E402
_rd.seed(1)
def _mk(path, vol=None):
    out, prev = [], path[0]
    for i, cc in enumerate(path):
        o = prev * (1 + _rd.uniform(-0.005, 0.005))
        out.append((dt.date(2026, 1, 1) + dt.timedelta(days=i), o, max(o, cc) * 1.005, min(o, cc) * 0.995, cc, (vol[i] if vol else 1000)))
        prev = cc
    return out
assert SW.chart_quality(_mk([100 * (1.004 ** i) * (1 + 0.01 * ((-1) ** i)) for i in range(120)]))[0] > 0
_kx = [130 + _rd.uniform(-2, 2) for i in range(110)] + [170] + [174 + _rd.uniform(-2, 1) for i in range(9)]
assert SW.chart_quality(_mk(_kx, [1000] * 110 + [15000] + [1500] * 9))[0] <= -2
assert SW.chart_quality(_mk([100 * (0.996 ** i) for i in range(120)]))[0] <= -2
print("✅ 차트 모양 점수 시험 통과")
# 매수 직전 후보 전체 다시 점검: 지금 가격으로 점수 높은 순, 탈락은 뒤로
_cc = M.Bot.cand_check
def _fake_cc(self, x, price):
    x["_fscore"] = {"A1": 2, "A2": 7, "A3": 9}[x["code"]]
    return ("차트 나쁨" if x["code"] == "A3" else None), "", []
M.Bot.cand_check = _fake_cc
rb = SimpleNamespace(cfg=cfg, m="US", log=print, notify=print)
cs = [{"code": c, "name": c} for c in ("A1", "A2", "A3")]
got = M.Bot.rerank(rb, cs, {"A1": 10, "A2": 10, "A3": 10}, dt.date(2026, 11, 3), {}, False)
M.Bot.cand_check = _cc
assert [x["code"] for x in got] == ["A2", "A1", "A3"] and got[0]["_chk"][0] == 10, [x["code"] for x in got]
print("✅ 매수 직전 후보 다시 줄 세우기 시험 통과")
# 아침 다시 고르기: 장 시작 전에 만든 후보는 안 쓰고, 장 시작 뒤에 만든 후보만 씀
import requests as _rq
_g = _rq.get
o_t = M.now("KR").replace(hour=9, minute=0, second=0, microsecond=0)
def _mk_live(made):
    return lambda *a, **k: SimpleNamespace(json=lambda: {"date": M.now("KR").date().isoformat(), "made": made.astimezone(dt.timezone.utc).isoformat(timespec="minutes"),
                                                         "picks": [{"code": "005930", "name": "삼성전자"}], "added": [], "dropped": []})
lr = SimpleNamespace(cfg={"daily_swing": {"live_picks_url": "x{m}"}}, m="KR", sw_picks={"picks": []}, open_t=o_t, log=print, notify=print)
_rq.get = _mk_live(o_t - dt.timedelta(minutes=30))
assert M.Bot.live_refresh(lr, max_age=99, after_open=True) is False
_rq.get = _mk_live(o_t + dt.timedelta(minutes=50))
ok = M.Bot.live_refresh(lr, max_age=1e6, after_open=True)
_rq.get = _g
assert ok and lr.sw_picks["picks"][0]["code"] == "005930", (ok, lr.sw_picks, M.now("KR"), o_t)
print("✅ 아침 전 종목 다시 고르기 시험 통과")
# 프리장 점검: 후보 + 후보 밖 전 종목의 프리장 가격 → 새 종목 추가, 정규장 전에 끝남 (사지는 않음)
import os
os.environ.pop("TOSSBOT_OFFLINE", None)
_lp, _rget, _cc2, _df = SW.load_picks, _rq.get, M.Bot.cand_check, M.Bot.daily_full
SW.load_picks = lambda *a, **k: {"date": "2026-11-02", "regime": True, "picks": [{"code": "C1", "name": "후보1", "setup": "fs", "close": 100, "stop": 92}]}
_rq.get = lambda url, **k: SimpleNamespace(json=lambda: {"N1": {"c": 6, "close": 50, "ld": False}, "N2": {"c": 1, "close": 20}} if "scores" in url else {"N1": "새종목"})
def _cc3(self, x, price):
    x["_fscore"] = 9 if x["code"] == "N1" else 4
    return None, "", []
M.Bot.cand_check = _cc3
M.Bot.daily_full = lambda self, k: [(dt.date(2026, 10, 1) + dt.timedelta(days=i), 50, 51, 49, 50, 1e6) for i in range(40)]
sent.clear()
pm = SimpleNamespace(cfg=cfg, m="US", dir=Path(tempfile.mkdtemp()), log=print, notify=sent.append,
                     open_t=dt.datetime(2026, 11, 3, 9, 30, tzinfo=M.TZ["US"]),
                     api=SimpleNamespace(prices=lambda cs: [{"symbol": c, "lastPrice": {"C1": 103, "N1": 52, "N2": 21}[c]} for c in cs]))
pm.swing_on = lambda: ["fs"]
M.Bot.swing_premarket(pm)
SW.load_picks, _rq.get, M.Bot.cand_check, M.Bot.daily_full = _lp, _rget, _cc2, _df
assert sent and "프리장 점검" in sent[0] and "새종목" in sent[0] and pm._pm_add[1][0]["code"] == "N1" and pm._pm_add[1][0]["score"] == 9, sent
assert "N2" not in str(pm._pm_add)                       # 차트 점수 낮은 종목은 안 봄
print("✅ 프리장 점검 시험 통과")
# 손실 검토: -3% 넘게 잃고 점수 2 이하면 손절선까지 안 기다리고 정리, 점수가 버티면 보유 (+ 앱에 보여 줄 판정 저장)
os.environ["TOSSBOT_OFFLINE"] = "1"
c["loss_review"] = {"enabled": True, "pct": 3, "max_score": 2}
c["exit_forward_score"] = 0
c["profit_take"] = {"enabled": False}
bl = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker, bot.m = bl, "US"
sent.clear()
bot.notify = sent.append
api.candles = lambda sym, iv, n: [{"timestamp": (d0 + dt.timedelta(days=i)).isoformat(), "closePrice": str(100 - i * 0.1), "highPrice": str(101 - i * 0.1), "lowPrice": str(99 - i * 0.1)} for i in range(20)]
la = bl.enter("L1", "L1", 100, 10, 200, 90, "시험")
la.extra.update(sw=True, carry=True, sw_days=0)
M.Bot.hold_score = lambda self, p, last=None: (5, ["＋20일선 위", "－최근 봉 음봉"], "")
M.Bot.swing_daily(bot, la, 96.5)                                   # -3.5% 지만 점수 +5 → 보유
assert "L1" in bl.pos and la.extra["review"]["grade"] == "주의" and "손실 검토" in sent[-1], (sent[-1], la.extra.get("review"))
M.Bot.hold_score = lambda self, p, last=None: (2, ["－20일선 아래", "－최근 봉 음봉"], "")
M.Bot.swing_daily(bot, la, 96.5)                                   # -3.5% + 점수 +2 → 정리
assert "L1" not in bl.pos and "손절선까지 기다리지 않고 정리" in bl.closed[-1].exit_reason, bl.closed[-1].exit_reason
lb = bl.enter("L2", "L2", 100, 10, 200, 90, "시험")
lb.extra.update(sw=True, carry=True, sw_days=0)
M.Bot.swing_daily(bot, lb, 98.5)                                   # -1.5%: 점수 +2 여도 손실 검토 대상 아님 (점수 0 이하 정리 규칙만)
assert "L2" in bl.pos and lb.extra["review"]["grade"] == "주의", lb.extra.get("review")
# 장중 -3% 를 처음 넘는 순간 한 번만 검토 알림 (팔지는 않음)
bot.sync_bg = True
sent.clear()
M.Bot.loss_watch(bot, lb, 96.9)
M.Bot.loss_watch(bot, lb, 96.0)
assert len(sent) == 1 and "손실 검토: 정리 예정" in sent[0] and "L2" in bl.pos, sent
M.Bot.hold_score = lambda self, p, last=None: (6, ["＋20일선 위"], "")
lb.extra.pop("lr_day")
M.Bot.loss_watch(bot, lb, 96.9)
assert "손실 검토: 주의" in sent[-1] and "버틸 근거" in sent[-1], sent[-1]
M.Bot.loss_watch(bot, lb, 99.0)                                    # -1%: 알림 없음
assert len(sent) == 2
assert M.Bot.review_verdict(bot, 7, -1.0)[0] == "유지" and M.Bot.review_verdict(bot, 0, 2.0)[0] == "정리 예정"
M.Bot.hold_score = orig_hs
print("✅ 손실 검토 시험 통과")

# 국장은 지수가 50일선 아래(시장 나쁨)면 fs 를 새로 안 삼, 미장은 그대로 삼
c["any_regime"] = ["fs"]
c["any_regime_by_market"] = {"KR": [], "US": ["fs"]}
for mk_, n_on in (("KR", 0), ("US", 1)):
    rb2 = SimpleNamespace(cfg=cfg, m=mk_)
    rb2.swing_on = lambda: ["fs"]
    dd = {"regime": False, "picks": [{"code": "X", "name": "X", "setup": "fs"}]}
    M.Bot.split_picks(rb2, dd)
    assert len(dd["picks"]) == n_on and len(dd["_off"]) == 1 - n_on, (mk_, dd)
    dd = {"regime": True, "picks": [{"code": "X", "name": "X", "setup": "fs"}]}
    M.Bot.split_picks(rb2, dd)
    assert len(dd["picks"]) == 1, (mk_, dd)
print("✅ 국장 하락 국면 매수 쉼 시험 통과")
# +10% 1/3 익절, 손절선은 그대로 (be=false): 본전으로 안 올림
c["profit_take"] = {"enabled": True, "pct": 10, "frac": 0.34, "be": False}
bq = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker = bq
q1 = bq.enter("Q1", "Q1", 100, 9, 200, 92, "시험")
q1.extra.update(sw=True, carry=True, sw_days=3)
assert M.Bot.profit_take(bot, q1, 104.6) is False and q1.qty == 9            # +4.6%: 아직 안 팖
assert M.Bot.profit_take(bot, q1, 110.5) is True and q1.qty == 6 and q1.sl == 92, (q1.qty, q1.sl)
assert M.Bot.profit_take(bot, q1, 112.0) is False and q1.qty == 6            # 한 번만
q2 = bq.enter("Q2", "Q2", 100, 1, 200, 92, "시험")
q2.extra.update(sw=True, carry=True, sw_days=3)
M.Bot.profit_take(bot, q2, 111.0)
assert q2.qty == 1 and q2.sl == 92 and q2.extra.get("pt_done")               # 1주: 아무것도 안 함
print("✅ +10% 익절(본전 이동 없음) 시험 통과")
# 여러 방식 함께: 미장 신고가(hi52)는 시장과 상관없이, 국장 재료 갭(ep)은 시장 나쁠 땐 점수 10 이상일 때만
c["any_regime_by_market"] = {"KR": ["fs"], "US": ["fs", "hi52"]}
c["bad_regime_min_score"] = {"KR": {"ep": 10}}
mb = SimpleNamespace(cfg=cfg, m="KR")
mb.swing_on = lambda: ["fs", "ep"]
dd = {"regime": False, "picks": [{"code": "E", "name": "E", "setup": "ep"}, {"code": "F", "name": "F", "setup": "fs"}]}
M.Bot.split_picks(mb, dd)
assert [x["code"] for x in dd["picks"]] == ["E", "F"] and dd["picks"][0]["_bad_min"] == 10 and "_bad_min" not in dd["picks"][1], dd
dd = {"regime": True, "picks": [{"code": "E", "name": "E", "setup": "ep"}]}
M.Bot.split_picks(mb, dd)
assert dd["picks"] and "_bad_min" not in dd["picks"][0]
mb = SimpleNamespace(cfg=cfg, m="US")
mb.swing_on = lambda: ["fs", "hi52"]
dd = {"regime": False, "picks": [{"code": "H", "name": "H", "setup": "hi52"}]}
M.Bot.split_picks(mb, dd)
assert dd["picks"] and "_bad_min" not in dd["picks"][0]
print("✅ 여러 방식 함께 사기 시험 통과")
# 시장별 손절선: 국장 3×ATR, 미장 사는 값 -5% (현재·미래 점수 방식만), 다른 방식은 자기 손절선
c["strict_setups"] = ["fs"]
c["stop_by_market"] = {"KR": {"atr": 3}, "US": {"pct": 5}}
xk = {"code": "K", "setup": "fs", "close": 10000, "stop": 9200, "atr": 400}
assert SW.stop_for(c, "KR", xk, 10100) == 8800 and SW.stop_for(c, "US", xk, 100) == 95.0
assert SW.stop_for(c, "KR", {**xk, "setup": "ep"}, 10100) == 9200 and SW.stop_for({}, "KR", xk, 10100) == 9200
# 급락 뒤 반등(rb): 점수·차트 모양·과열 기준을 안 쓰고, 신호 뒤 3% 넘게 밀리면 안 삼
c["rebound_setups"] = ["rb"]
c["min_forward_score"] = 3
c["min_chart_q"] = -2
c["need_target"] = True
rbb = SimpleNamespace(cfg=cfg, m="US", log=print)
_df2, _sn2, _er2, _nj2 = M.Bot.daily_full, M.Bot.stock_news, M.Bot.est_row, M.Bot.news_judged
M.Bot.daily_full = lambda self, k: [(dt.date(2026, 8, 1) + dt.timedelta(days=i), 100 - i * 0.3, 101 - i * 0.3, 99 - i * 0.3, 100 - i * 0.3, 1e6) for i in range(90)]   # 계속 내리는 차트
M.Bot.stock_news = lambda self, a, b: []
M.Bot.est_row = lambda self, s_: {}
M.Bot.news_judged = lambda self: {}
xr = {"code": "R", "name": "R", "setup": "rb", "close": 74.0, "low": 72.5, "stop": 68.1}
why_r, _, _ = M.Bot.cand_check(rbb, dict(xr), 74.5)
assert why_r is None, why_r                                                        # 내리는 차트여도 반등 방식은 삼
why_f, _, _ = M.Bot.cand_check(rbb, {**xr, "setup": "fs"}, 74.5)
assert why_f, "같은 종목을 현재·미래 점수 방식으로는 안 삼"
why_d, _, _ = M.Bot.cand_check(rbb, dict(xr), 71.0)
assert why_d and "반등 실패" in why_d, why_d
M.Bot.daily_full, M.Bot.stock_news, M.Bot.est_row, M.Bot.news_judged = _df2, _sn2, _er2, _nj2
# 반등 신호 만들기 (연구·후보 생성 공용)
try:
    import numpy  # noqa: F401  (서버 로봇에는 numpy 가 없음 → 후보 생성용 신호 시험은 건너뜀)
    _np = True
except ImportError:
    _np = False
if _np:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import swingbt as BT
    rows = [(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", 100.0, 101.0, 99.0, 100.0, 1e6) for i in range(200)]
    rows += [(f"2026-09-{1 + i:02d}", 100 - i * 4.0, 100 - i * 4.0 + 0.5, 96 - i * 4.0, 96 - i * 4.0, 1e6) for i in range(7)]      # 급락 (100 → 72)
    rows += [(f"2026-09-{8 + i:02d}", 72.0, 72.5, 71.5, 72.0, 1e6) for i in range(3)]                                               # 바닥에서 사흘 쉼
    rows += [("2026-09-11", 72.0, 76.5, 71.5, 76.0, 2e6)]                                                                           # +5.6% 반등, 5일선 위
    Sx = {"X": BT.indicators(BT.prep(rows))}
    sg = BT.signals("rb", Sx, {"include_last": True, "mintv": 1}, {"bars": {"X": rows}}, "us")
    assert sg and sg[-1][0] == "2026-09-11" and abs(sg[-1][3] - 76.0 * 0.92) < 1e-6, sg[-3:]
print("✅ 시장별 손절·급락 뒤 반등 시험 통과")
# 반등 방식 보유 종목: 손실 검토 알림·점수 판정을 안 씀 (손절선·15일 규칙으로만)
c["exit_by_setup"] = {**(c.get("exit_by_setup") or {}), "rb": {"exit_ma": 0, "max_hold": 15, "exit_forward_score": None, "loss_review": {"enabled": False}}}
c["loss_review"] = {"enabled": True, "pct": 3, "max_score": 2}
br = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker, bot.sync_bg = br, True
r1 = br.enter("RB1", "RB1", 100, 10, 200, 92, "시험")
r1.extra.update(sw=True, carry=True, sw_days=1, sw_kind="rb")
sent.clear()
M.Bot.hold_score = lambda self, p, last=None: (-4, ["－20일선 아래"], "")
M.Bot.loss_watch(bot, r1, 96.0)
assert not sent, sent
assert M.Bot.review_verdict(bot, -4, -4.0, r1)[0] == "유지" and M.Bot.review_verdict(bot, -4, -4.0)[0] == "정리 예정"
M.Bot.swing_daily(bot, r1, 96.0)                         # 점수 -4, 손실 -4% 여도 반등 방식은 계속 보유
assert "RB1" in br.pos, br.closed[-1].exit_reason if br.closed else ""
M.Bot.hold_score = orig_hs
print("✅ 반등 방식 보유 관리 시험 통과")
# 신고가·반등 후보: 사기 직전 AI 판독 — 부정·매수 금지면 안 삼, 판독을 못 받으면 안 삼 (차트 신호만으로 안 삼)
from bot import news_judge as _NJ
_call0 = _NJ._call
os.environ["ANTHROPIC_API_KEY"] = "test"
os.environ.pop("TOSSBOT_OFFLINE", None)
ab = SimpleNamespace(cfg=cfg, m="US", log=print)
_er3, _sn3 = M.Bot.est_row, M.Bot.stock_news
M.Bot.est_row = lambda self, s_: {"targetMeanPrice": 120}
M.Bot.stock_news = lambda self, a, b: ["Akamai cuts guidance"]
seen_p = []
_NJ._call = lambda prompt, model, claude=None: (seen_p.append((prompt, model)) or {"view": "부정", "catalyst": "가이던스 하향", "risk": "추가 하락", "adj": -2, "veto": "가이던스 대폭 하향"})
a1 = M.Bot.ai_check(ab, {"code": "AKAM", "name": "Akamai", "setup": "rb", "reason": "급락 뒤 반등", "close": 83.7, "stop": 77}, 84.0)
assert a1["view"] == "부정" and a1["veto"] and "급락 뒤 반등" in seen_p[0][0] and "Akamai cuts guidance" in seen_p[0][0] and seen_p[0][1] == "claude-opus-5-5", (a1, seen_p)
_NJ._call = lambda prompt, model, claude=None: {}
assert M.Bot.ai_check(ab, {"code": "AKAM", "name": "Akamai", "setup": "rb"}, 84.0) == {}
_NJ._call = _call0
M.Bot.est_row, M.Bot.stock_news = _er3, _sn3
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["TOSSBOT_OFFLINE"] = "1"
assert M.Bot.ai_check(ab, {"code": "X", "name": "X", "setup": "rb"}, 1.0) == {}
print("✅ 다른 방식 후보 사기 직전 AI 판독 시험 통과")
# 따로 자리: 미장 신고가·반등은 기본 자리(꽉 참)와 별개로 각 3종목까지 삼
c["extra_slots"] = {"US": {"hi52": 3, "rb": 3}}
c["extra_open_risk_by_market"] = {"US": 4}
c["max_pos_by_market"] = {"US": 1}
c["max_open_risk_by_market"] = {"US": 3}
c["setup_slots"] = {}
c["exit_by_setup"] = {}
bx = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
px0 = bx.enter("F0", "F0", 100, 1, 200, 95, "시험")
px0.extra.update(sw=True, sw_kind="fs")
bot.broker, bot.m = bx, "US"
api.prices = lambda syms: [{"symbol": s_, "lastPrice": "100"} for s_ in syms]
bot.sw_picks = {"date": "2026-11-02", "regime": True, "picks": [
    {"code": "F1", "name": "F1", "setup": "fs", "close": 100, "stop": 95, "atr": 2, "score": 9, "reason": "점수", "ev": {"sector": "A", "ai": {"view": "긍정"}}},
    {"code": "H1", "name": "H1", "setup": "hi52", "close": 100, "stop": 96, "atr": 2, "score": 2, "reason": "신고가", "ev": {"sector": "B", "ai": {"view": "긍정"}}},
    {"code": "R1", "name": "R1", "setup": "rb", "close": 100, "stop": 92, "atr": 2, "score": 1, "reason": "반등", "ev": {"sector": "C", "ai": {"view": "중립"}}}]}
_cc5 = M.Bot.cand_check
M.Bot.cand_check = lambda self, x, price: (setattr(self, "_x", 1) or x.update(_fscore=6) or (None, "", []))
_ss = M.Bot.swing_timing
M.Bot.swing_timing = lambda self, x, d: "close"
M.Bot.swing_enter(bot, 10000, "close")
M.Bot.cand_check, M.Bot.swing_timing = _cc5, _ss
assert "H1" in bx.pos and "R1" in bx.pos and "F1" not in bx.pos, list(bx.pos)
print("✅ 따로 자리(신고가·반등) 시험 통과")
# 사용자 설정: 모든 방식 손절선 -3% 고정 (all) — 신고가·반등에도 적용
assert abs(SW.stop_for({"stop_by_market": {"US": {"pct": 3, "all": True}}, "strict_setups": ["fs"]}, "US", {"stop": 80, "setup": "rb"}, 100) - 97.0) < 1e-9
assert SW.stop_for({"stop_by_market": {"US": {"pct": 3}}, "strict_setups": ["fs"]}, "US", {"stop": 80, "setup": "rb"}, 100) == 80
print("✅ 모든 방식 -3% 손절 시험 통과")
# 손절선 -5% 고정: 보유 종목도 마감 점검 때 맞춤(더 높은 손절선은 그대로), 이미 아래면 정리
c["stop_by_market"] = {"US": {"pct": 5, "all": True}}
c["exit_by_setup"] = {}
c["loss_review"] = {"enabled": False}
c["profit_take"] = {"enabled": False}
c["exit_forward_score"] = None
c["exit_ma"] = 0
c["time_stop_days"] = 99
bf = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker, bot.m = bf, "US"
api.candles = lambda sym, iv, n: [{"timestamp": (d0 + dt.timedelta(days=i)).isoformat(), "closePrice": str(100 + i * 0.1), "highPrice": str(101 + i * 0.1), "lowPrice": str(99 + i * 0.1)} for i in range(20)]
M.Bot.hold_score = lambda self, p, last=None: (6, ["＋20일선 위"], "")
w1 = bf.enter("W1", "W1", 100, 10, 200, 88, "시험")      # 예전에 -12% 로 넓게 잡은 종목
w1.extra.update(sw=True, carry=True, sw_days=2)
M.Bot.swing_daily(bot, w1, 101.0)
assert "W1" in bf.pos and abs(w1.sl - 95.0) < 0.02, (w1.sl, sent[-3:], bot.m, c.get("stop_by_market"))
w2 = bf.enter("W2", "W2", 100, 10, 200, 98, "시험")      # 이미 더 높은 손절선(-2%)은 그대로
w2.extra.update(sw=True, carry=True, sw_days=2)
M.Bot.swing_daily(bot, w2, 101.0)
assert abs(w2.sl - 98.0) < 0.02, w2.sl
w3 = bf.enter("W3", "W3", 100, 10, 200, 85, "시험")
w3.extra.update(sw=True, carry=True, sw_days=2)
M.Bot.swing_daily(bot, w3, 94.0)                         # 이미 -5% 아래로 마감 → 정리
assert "W3" not in bf.pos and "고정 손절" in bf.closed[-1].exit_reason, bf.closed[-1].exit_reason
M.Bot.hold_score = orig_hs
print("✅ 보유 종목 -5% 고정 손절 시험 통과")
# 이익 잠금: 최고가 기준 손절선 올리기(내리지 않음) + +10% 두 번째 익절
c["profit_lock"] = {"enabled": True, "steps": [[4, 0.5], [7, 3], [10, 5]], "trail_from": 12, "trail_pct": 5, "take2_pct": 10, "take2_frac": 0.5}
bl2 = PaperBroker("US", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot.broker = bl2
sent.clear()
k1 = bl2.enter("K1", "K1", 100, 10, 300, 95, "시험")
k1.extra.update(sw=True, carry=True, sw_days=1)
M.Bot.profit_lock(bot, k1, 103.0)
assert k1.sl == 95                                         # +3%: 아직 그대로
M.Bot.profit_lock(bot, k1, 104.2)
assert abs(k1.sl - 100.5) < 0.02, k1.sl                    # +4%: 본전 위
M.Bot.profit_lock(bot, k1, 107.5)
assert abs(k1.sl - 103.0) < 0.02, k1.sl                    # +7%: +3% 잠금
M.Bot.profit_lock(bot, k1, 104.0)
assert abs(k1.sl - 103.0) < 0.02, k1.sl                    # 내려와도 손절선은 안 내림
M.Bot.profit_lock(bot, k1, 110.5)                          # +10%: 절반 더 익절 + 손절선 +5%
assert k1.qty == 5 and k1.extra.get("pl_t2"), (k1.qty, k1.extra)
M.Bot.profit_lock(bot, k1, 110.6)
assert abs(k1.sl - 105.0) < 0.02, k1.sl
M.Bot.profit_lock(bot, k1, 120.0)                          # +20%: 최고가 -5% = 114 따라감
assert abs(k1.sl - 114.0) < 0.02, k1.sl
assert any("이익으로 팔려요" in m_ for m_ in sent), sent
c["profit_lock"] = {"enabled": False}
print("✅ 이익 잠금 시험 통과")
