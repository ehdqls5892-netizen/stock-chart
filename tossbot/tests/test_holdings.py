"""보유종목 칸 시험 (python -m tests.test_holdings): 종목별 잠금·팔기, 장 밖(저장 기록) 잠금."""
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import bot.app as A
import bot.main as M
from bot.broker import PaperBroker, Position
from bot.telegram_ctl import TelegramControl

cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
tmp = Path(tempfile.mkdtemp())
b = PaperBroker("US", cfg, tmp, print, lambda m: None)
for sym, nm in (("CDNS", "케이던스"), ("COHR", "코히런트"), ("PLTR", "팔란티어")):
    b.pos[sym] = Position(sym, nm, 1, 100, 120, 97, "x", "x", state="open", extra={"R": 3})
bot = SimpleNamespace(broker=b, syms={"CDNS": SimpleNamespace(last=104)}, sell_reqs=[], loop_on=False)
A.STATE["bots"] = {"US": bot}
A.STATE["mode"] = "paper"
A.quotes = lambda syms: {"COHR": 110.0, "A1": 52000.0}          # 시세 조회 대신
A.fx_usdkrw = lambda: 1400.0
UP = [(f"d{i:02d}", 90 + i, 91 + i, 89 + i, 90 + i) for i in range(24)] + [("d24", 113, 118, 112, 117.5)]   # 상승·고가 마감
DOWN = [(f"d{i:02d}", 130 - i, 131 - i, 129 - i, 130 - i) for i in range(24)] + [("d24", 106, 107, 100, 100.5)]  # 하락·저가 마감
A.daily_bars = lambda sym: UP if sym in ("COHR", "A1") else DOWN

# 잠금 평가: 수익 크고 손절선 본전 위·상승 추세 → 유지 / 손실·하락·악재 → 나쁨
g, why = A.rate_hold({"symbol": "X", "pct": 13.2, "last": 5120, "sl": 4860, "entry": 4522}, UP)
print("좋은 예:", g, why)
assert g == "유지"
g, why = A.rate_hold({"symbol": "Y", "pct": -1.5, "last": 98.5, "sl": 97, "entry": 100}, DOWN, {"news_bad": {"Y": {}}})
print("나쁜 예:", g, why)
assert g == "나쁨" and any("악재" in w for w in why)
g, _ = A.rate_hold({"symbol": "Z", "pct": 0.5, "last": 100.5, "sl": 97, "entry": 100}, [])
assert g == "보통", g
g, _ = A.rate_hold({"symbol": "E", "pct": 6, "last": 106, "sl": 101, "entry": 100}, UP, {"earn_block": {"E"}})
assert g != "유지", "실적 발표 임박인데 유지"

hs = A.holdings()
assert len(hs) == 3 and abs(hs[0]["pct"] - 4) < 1e-9, hs
# 장 밖이면 팔기 안 됨
assert "장" in A.sell_one("US", "PLTR") and not bot.sell_reqs
bot.loop_on = True
print(A.sell_one("US", "PLTR"))
assert bot.sell_reqs == ["PLTR"]
# 잠금 → 한도에서 빠짐
print(A.set_lock("US", "CDNS", True))
assert b.pos["CDNS"].extra.get("locked") and b.open_count() == 2
print(A.lock_all())
assert all(p.extra.get("locked") for p in b.pos.values())
print(A.set_lock("US", "COHR", False))
assert not b.pos["COHR"].extra.get("locked")

# 장 밖(로봇 메모리에 없음): 저장된 기록에서 읽고 잠금도 기록에 씀
M.ROOT = tmp
d = tmp / "logs" / "paper" / "KR"
d.mkdir(parents=True)
(d / "state.json").write_text(json.dumps({"date": "2026-10-27", "positions": [
    {"symbol": "A1", "name": "이수페타시스", "qty": 3, "entry": 50000, "sl": 48500, "state": "open", "extra": {"carry": True}}]},
    ensure_ascii=False), encoding="utf-8")
kr = [h for h in A.holdings() if h["market"] == "KR"]
assert kr and kr[0]["name"] == "이수페타시스", kr
print(A.set_lock("KR", "A1", True))
assert json.loads((d / "state.json").read_text(encoding="utf-8"))["positions"][0]["extra"]["locked"] is True
assert "장" in A.sell_one("KR", "A1")

# 텔레그램 버튼
sent = []
tc = TelegramControl(lambda *a: "", lambda how: f"stop:{how}", lambda: {})
tc.token, tc.chat = "x", "1"
tc._api = lambda m, body: sent.append((m, body)) or {}
tc.holdings_fn, tc.set_lock_fn, tc.sell_one_fn, tc.lock_all_fn = A.holdings, A.set_lock, A.sell_one, A.lock_all
tc.handle("📦 보유종목")
text = sent[-1][1]["text"]
print(text)
assert "케이던스" in text and "이수페타시스" in text and "🔒" in text
assert "평가손익 +$4.00 ≈ +5,600원" in text and "+$10.00 ≈ +14,000원" in text and "+6,000원" in text, text
assert "합계 평가손익 ≈ +25,600원" in text, text
assert "🟢유지" in text or "🟡보통" in text, text
kb = sent[-1][1]["reply_markup"]["inline_keyboard"]
assert any(btn["callback_data"] == "S|US|COHR" for row in kb for btn in row)
assert all(len(btn["callback_data"].encode()) <= 64 for row in kb for btn in row)
tc.on_callback("L|US|COHR", 5)
assert b.pos["COHR"].extra.get("locked")
tc.on_callback("S|US|COHR", 5)
yes = sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
assert yes.startswith("SY|US|COHR|") and "COHR" not in bot.sell_reqs
tc.on_callback(yes, 5)
assert "COHR" in bot.sell_reqs
tc.on_callback(f"SY|US|CDNS|{int(time.time()) - 500}", 5)        # 오래된 확인 버튼은 무시
assert "CDNS" not in bot.sell_reqs
tc.on_callback("OFF", 5)
assert "OFFY" in sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
tc.on_callback(f"SAY|{int(time.time())}", 5)
assert "stop:sell" in sent[-1][1]["text"]
print("✅ 보유종목 칸 시험 통과")
