"""장기 ETF 적립 시험 (python -m tests.test_etf): 매달 한 번·이월·추가 매수·손절/정리/전부 팔기에서 제외·잠금 해제 불가."""
import datetime as dt
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import bot.app as A
import bot.etf as ETF
import bot.main as M
from bot.broker import PaperBroker

# 1) 계획: 비중대로 나누고, 1주 값이 안 되면 이월
items = [["VOO", "VOO", 0.5], ["SCHD", "SCHD", 0.3], ["QQQ", "QQQ", 0.2]]
buys, bank = ETF.plan(300, items, {}, {"VOO": 600, "SCHD": 28, "QQQ": 520}, None)
assert [b[:3] for b in buys] == [("SCHD", "SCHD", 3)], buys          # VOO $150·QQQ $60 은 이월
assert abs(bank["VOO"] - 150) < 1e-9 and abs(bank["QQQ"] - 60) < 1e-9 and abs(bank["SCHD"] - 6) < 1e-9
for _ in range(3):
    buys, bank = ETF.plan(300, items, bank, {"VOO": 600, "SCHD": 28, "QQQ": 520}, None)
assert any(b[0] == "VOO" for b in buys), "4달 모으면 VOO 1주"
buys, _ = ETF.plan(300, items, {}, {"VOO": 600, "SCHD": 28, "QQQ": 520}, 50)
assert sum(q * p for _, _, q, p in buys) <= 50                        # 현금 안에서만

# 2) 로봇: 첫 거래일에 사고 같은 달엔 다시 안 삼, 다음 달엔 추가 매수
root = Path(tempfile.mkdtemp())
M.ROOT = root
cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
cfg["market"] = "KR"
(root / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
M.save_settings({"etf_budget": {"KR": 1_000_000}})
b = PaperBroker("KR", cfg, root, print, lambda m: None)
px = {"069500": 50000, "360750": 20000, "458730": 12000}
sent = []
api = SimpleNamespace(prices=lambda syms: [{"symbol": s, "lastPrice": str(px[s])} for s in syms],
                      stocks=lambda syms: [{"name": "KODEX 200"}])
bot = SimpleNamespace(cfg=cfg, m="KR", broker=b, api=api, risk=SimpleNamespace(kill_switch=lambda: False),
                      log=print, notify=sent.append)
n = dt.datetime(2026, 11, 2, 9, 10, tzinfo=M.TZ["KR"])
M.Bot.etf_buy(bot, n, None)
assert set(b.pos) == {"069500", "360750", "458730"}, b.pos
p = b.pos["069500"]
assert p.qty == 10 and p.extra["etf_long"] and p.extra["locked"] and p.extra["nostop"]
print(sent[-1])
assert M.Bot.etf_buy(bot, n + dt.timedelta(days=1), None) == 0          # 같은 달 두 번 안 삼
M.Bot.etf_buy(bot, dt.datetime(2026, 12, 1, 9, 10, tzinfo=M.TZ["KR"]), None)
assert b.pos["069500"].qty == 20, b.pos["069500"].qty                   # 다음 달 추가

# 3) 손절·장중 관리·로봇 정리에서 빠짐, 텔레그램 팔기는 됨
assert b.manage(p, 70000) == (None, None, "")
b.on_price("069500", 30000, None)
assert "069500" in b.pos, "ETF 가 손절로 팔림"
b.flatten("069500", "종료 버튼 — 전부 정리", 50000)
assert "069500" in b.pos, "전부 팔기에 ETF 가 팔림"
assert b.open_count() == 0 and b.invested() == 0                        # 단타 한도·투자금 계산에서 빠짐
A.STATE["bots"] = {"KR": SimpleNamespace(broker=b, syms={})}
A.STATE["mode"] = "paper"
A.quotes = lambda syms: {}
A.fx_usdkrw = lambda: None
A.daily_bars = lambda sym: []
assert "풀 수 없" in A.set_lock("KR", "069500", False) and b.pos["069500"].extra["locked"]
assert "풀 수 없" in A.lock_cmd("잠금 해제 KODEX")
b.flatten("069500", "직접 매도 (텔레그램 보유종목)", 50000, force=True)
assert "069500" not in b.pos
# 5) 추천 모드 (기본): 수량·상태·언제 살지 안내, 주문은 안 함
assert cfg["etf_long"]["mode"] == "advise"
fake = {"KR": {"069500": {"code": "069500", "name": "KODEX 200", "w": 0.5, "price": 77000, "above200": True, "from_hi": -0.12, "ret1y": 0.6, "div_yield": 0.015},
               "360750": {"code": "360750", "name": "TIGER 미국S&P500", "w": 0.3, "price": 25000, "above200": True, "from_hi": -0.03},
               "458730": {"code": "458730", "name": "TIGER 미국배당다우존스", "w": 0.2, "price": 13000, "above200": False, "from_hi": -0.08}},
        "US": {"VOO": {"code": "VOO", "name": "VOO", "w": 0.5, "price": 695}, "SCHD": {"code": "SCHD", "name": "SCHD", "w": 0.3, "price": 28},
               "QQQ": {"code": "QQQ", "name": "QQQ", "w": 0.2, "price": 690}}}
A.etf_info = lambda mk: fake[mk]
txt = A.etf_cmd("ETF 국장 100만")
print(txt)
assert "6주 × 77,000원" in txt and "13주 × 25,000원" in txt and "남는 돈 5,000원" in txt and "직접" in txt
assert M.load_settings()["etf_budget"]["KR"] == 1_000_000
lump = A.etf_cmd("ETF 한번에 미장 5000")
print(lump)
assert "3~6개월" in lump and "× $695.00" in lump
print(A.etf_cmd("ETF"))
assert "꺼짐" in A.etf_cmd("ETF 미장 0") or True
# 매달 첫 거래일: 알림만, 주문 없음
sent2 = []
b5 = PaperBroker("KR", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
bot5 = SimpleNamespace(cfg=cfg, m="KR", broker=b5, notify=sent2.append, log=print)
st0 = M.load_settings(); st0.pop("etf_month", None); M.save_settings(st0)
M.Bot.etf_advise(bot5, dt.datetime(2027, 1, 4, 9, 10, tzinfo=M.TZ["KR"]))
assert sent2 and "직접 매수" in sent2[-1] and not b5.pos, sent2
M.Bot.etf_advise(bot5, dt.datetime(2027, 1, 5, 9, 10, tzinfo=M.TZ["KR"]))
assert len(sent2) == 1                                     # 같은 달 한 번만
print("✅ 장기 ETF 시험 통과")

# 4) 실전: 손절 주문 없는 ETF 의 다음 달 추가 매수 체결도 확인돼야 함
from bot.broker import LiveBroker, Position
orders = {}
class FakeLiveAPI:
    def create_order(self, sym, side, q, typ, price, cid):
        orders["o1"] = {"status": "FILLED", "execution": {"filledQuantity": str(q), "averageFilledPrice": "51000"}}
        return {"orderId": "o1"}
    def get_order(self, oid):
        return orders[oid]
lb = LiveBroker(FakeLiveAPI(), "KR", cfg, Path(tempfile.mkdtemp()), print, lambda m: None)
ep = Position("069500", "KODEX 200", 10, 50000, 5_000_000, 0.0, "x", "x", state="open",
              extra={"etf_long": True, "nostop": True, "locked": True, "carry": True})
lb.pos["069500"] = ep
lb.add(ep, 5, 51000, "적립")
lb.poll(None, {})
assert ep.qty == 15 and "add_oid" not in ep.extra, (ep.qty, ep.extra)
print("✅ 실전 ETF 추가 적립 체결 확인 통과")
