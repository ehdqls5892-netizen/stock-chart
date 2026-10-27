"""가짜 시세·가짜 토스 서버로 하루를 빠르게 돌려 보는 시험.

python -m tests.sim paper   연습 모드 흐름
python -m tests.sim live    실전 흐름 (가짜 서버에 주문·조건주문이 제대로 가는지)
"""
from __future__ import annotations
import os
os.environ["TOSSBOT_NO_BG"] = "1"   # 가짜 시계 시험: 뒤 작업 스레드 끔
os.environ["TOSSBOT_OFFLINE"] = "1"  # 시험 중엔 GitHub 데스크 자료를 안 받음

import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

import bot.main as M
import bot.broker as B
from bot.util import TZ, kr_tick

KST = TZ["KR"]
DAY = dt.date(2026, 10, 26)  # 월요일
OPEN = dt.datetime.combine(DAY, dt.time(9, 0), KST)


def path_up(t):     # 돌파 → 꾸준히 +15% 가까이 상승 → 고점에서 밀림 (추적 손절로 이익 실현)
    m = (t - OPEN).total_seconds() / 60
    base = 10000
    if m < 15:
        return base + (60 if int(m) % 2 else -40), 1000
    if m < 25:
        return base + 30, 900
    if m < 26:
        return base + 160, 6000           # 돌파 + 거래량 6배
    if m < 120:
        return base + 160 + (m - 26) * 14, 1500   # 10160 → 약 11480
    return max(base + 160 + 94 * 14 - (m - 120) * 25, 10500), 1200


def path_wiggle(t):  # 돌파 직후 -2% 흔들렸다가 다시 오름 (예전 -1.5% 손절이면 털렸을 자리)
    m = (t - OPEN).total_seconds() / 60
    base = 8000
    if m < 15:
        return base + (40 if int(m) % 2 else -30), 1200
    if m < 35:
        return base + 20, 1000
    if m < 36:
        return base + 130, 9000
    if m < 45:
        return base + 130 - (m - 36) * 18, 1100     # 8130 → 7968 (-2%)
    return min(base - 32 + (m - 45) * 6, 8700), 1300


def path_fail(t):   # 돌파했다가 무너져 손절
    m = (t - OPEN).total_seconds() / 60
    base = 20000
    if m < 15:
        return base + (150 if int(m) % 2 else -100), 800
    if m < 30:
        return base + 50, 700
    if m < 31:
        return base + 400, 5000
    return max(base + 400 - (m - 31) * 120, 19000), 900


def path_flat(t):   # 신호 없음
    return 5000 + (5 if int((t - OPEN).total_seconds() / 60) % 2 else -5), 500


PATHS = {"111111": ("올라가는주", path_up), "222222": ("무너지는주", path_fail), "333333": ("심심한주", path_flat),
         "666666": ("흔들리는주", path_wiggle)}


class Clock:
    def __init__(self):
        self.t = OPEN - dt.timedelta(minutes=5)

    def now(self, market="KR"):
        return self.t

    def sleep(self, s):
        self.t += dt.timedelta(seconds=max(s, 20))  # 한 번 돌 때마다 20초씩 빨리 감기


CLK = Clock()


def snap(px, sym):
    return int(px // kr_tick(px) * kr_tick(px))


class FakeAPI:
    account_seq = None

    def __init__(self, *a, **k):
        self.orders, self.conds, self.n = {}, {}, 0
        self.log = []

    def token(self):
        return "tok"

    def accounts(self):
        return [{"accountNo": "12345678901", "accountSeq": 7, "accountType": "BROKERAGE"}]

    def calendar(self, m):
        o = dt.datetime.combine(DAY, dt.time(9, 0), KST)
        c = dt.datetime.combine(DAY, dt.time(15, 30), KST)
        return {"today": {"date": DAY.isoformat(), "integrated": {"regularMarket": {
            "startTime": o.isoformat(), "endTime": c.isoformat()}}}}

    def rankings(self, *a, **k):
        return {"rankings": [{"symbol": s} for s in PATHS]}

    def stocks(self, syms):
        return [{"symbol": s, "name": PATHS[s][0], "securityType": "STOCK", "isCommonShare": True} for s in syms if s in PATHS]

    def warnings(self, s):
        return []

    def prices(self, syms):
        return [{"symbol": s, "lastPrice": str(self.price(s))} for s in syms if s in PATHS]

    def price(self, sym):
        return snap(PATHS[sym][1](CLK.t)[0], sym)

    def candles(self, sym, interval="1m", count=60):
        if interval == "1d":
            p = PATHS[sym][1](OPEN)[0]
            return [{"timestamp": (OPEN - dt.timedelta(days=1)).isoformat(), "closePrice": str(p * 0.98)}]
        out = []
        t = OPEN
        while t <= CLK.t:
            p0, v = PATHS[sym][1](t)
            p1 = PATHS[sym][1](t + dt.timedelta(seconds=59))[0]
            out.append({"timestamp": t.isoformat(), "openPrice": p0, "highPrice": max(p0, p1) + 10,
                        "lowPrice": min(p0, p1) - 10, "closePrice": p1, "volume": v})
            t += dt.timedelta(minutes=1)
        return out[-count:]

    def orderbook(self, sym):
        p = self.price(sym)
        return {"asks": [{"price": str(p + kr_tick(p))}], "bids": [{"price": str(p)}]}

    def buying_power(self, c):
        return {"cashBuyingPower": "1000000"}

    def sellable(self, sym):
        return {"sellableQuantity": str(sum(o["q"] for o in self.orders.values() if o["sym"] == sym and o["side"] == "BUY"))}

    def create_order(self, sym, side, q, typ, price, cid):
        self.n += 1
        oid = f"o{self.n}"
        px = float(price) if price else self.price(sym)
        self.orders[oid] = {"sym": sym, "side": side, "q": int(q), "px": px}
        self.log.append(("order", sym, side, q, typ, price))
        return {"orderId": oid}

    def get_order(self, oid):
        o = self.orders[oid]
        return {"status": "FILLED", "execution": {"filledQuantity": str(o["q"]), "averageFilledPrice": str(o["px"])}}

    def cancel_order(self, oid):
        self.log.append(("cancel", oid))

    def create_oco_exit(self, sym, q, tpt, tpp, slt, slp, exp, client_id=None):
        self.n += 1
        cid = f"c{self.n}"
        self.conds[cid] = {"sym": sym, "q": q, "tp": float(tpt), "sl": float(slt), "slp": float(slp), "status": "WATCHING"}
        self.log.append(("oco", sym, q, tpt, tpp, slt, slp, exp))
        return {"conditionalOrderId": cid}

    def modify_oco_exit(self, cid, q, tpt, tpp, slt, slp, exp):
        self.conds[cid].update(sl=float(slt), slp=float(slp))
        self.log.append(("modify", cid, slt, slp))

    def create_stop_market(self, sym, q, trig, exp, client_id=None):
        self.n += 1
        cid = f"c{self.n}"
        self.conds[cid] = {"sym": sym, "q": int(q), "tp": 1e18, "sl": float(trig), "slp": None, "status": "WATCHING"}
        self.log.append(("stop", sym, q, trig, exp))
        return {"conditionalOrderId": cid}

    def modify_stop_market(self, cid, q, trig, exp):
        self.conds[cid].update(q=int(q), sl=float(trig))
        self.log.append(("modify", cid, trig, q, exp))
        return {"conditionalOrderId": cid}

    def replace_stop_market(self, cid, sym, q, trig, exp):
        from bot.api import TossAPI
        return TossAPI.replace_stop_market(self, cid, sym, q, trig, exp)

    def is_alive(self, cid):
        return self.conds.get(cid, {}).get("status") == "WATCHING"

    def cancel_conditional(self, cid):
        self.conds[cid]["status"] = "CANCELED"
        self.log.append(("cancel_cond", cid))

    def get_conditional(self, cid):
        c = self.conds[cid]
        p = self.price(c["sym"])
        if c["status"] == "WATCHING":
            if p >= c["tp"]:
                c["status"], c["px"], c["leg"] = "COMPLETED", c["tp"], 0
            elif p <= c["sl"]:
                c["status"], c["px"], c["leg"] = "COMPLETED", (c["slp"] if c["slp"] is not None else p), 1
        out = {"status": c["status"], "first": {}, "second": {}}
        if c["status"] == "COMPLETED":
            self.n += 1
            oid = f"o{self.n}"
            self.orders[oid] = {"sym": c["sym"], "side": "SELL", "q": c["q"], "px": c["px"]}
            out["first" if (c["leg"] == 0 or c["slp"] is None) else "second"]["triggeredOrderId"] = oid
        return out


class FakeStream:
    def __init__(self, api, market, on_trade, on_order=None, log=print):
        self.on_trade = on_trade

    def start(self):
        pass

    def set_codes(self, c):
        self.codes = c

    def stop(self):
        pass


def main(mode):
    tmp = Path(tempfile.mkdtemp())
    root = Path(__file__).resolve().parent.parent
    shutil.copy(root / "config.json", tmp / "config.json")
    cfg = json.loads((tmp / "config.json").read_text())
    cfg["live"]["enabled"] = True
    cfg["github_chart_url"] = ""
    (tmp / "config.json").write_text(json.dumps(cfg))
    M.ROOT = tmp
    M._API = None
    M.now = CLK.now
    B.dt = dt
    import bot.stream
    bot.stream.Stream = FakeStream
    M.TossAPI = FakeAPI
    import time as T
    orig_sleep = T.sleep
    bot_ref = {}

    def fake_sleep(s):
        CLK.sleep(s)
        b = bot_ref.get("b")
        if b and getattr(b, "universe", None) and hasattr(b, "broker"):
            for sym in b.universe:
                p, v = PATHS[sym][1](CLK.t)
                b.on_trade(sym, snap(p, sym), 1, CLK.t.isoformat())
    M.time.sleep = fake_sleep
    orig_time = T.time
    M.time.time = lambda: CLK.t.timestamp()
    bb = M.Bot(mode)
    bot_ref["b"] = bb
    try:
        bb.run()
    finally:
        M.time.sleep = orig_sleep
        M.time.time = orig_time
    if mode == "live":
        print("\n--- 가짜 서버가 받은 요청 ---")
        for x in bb.api.log:
            print(x)
    print("\n결과:", [(p.name, p.entry, p.exit_price, round(p.pnl), p.exit_reason) for p in bb.broker.closed])
    return bb


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "paper")
