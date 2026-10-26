"""미장 시험: 달러 가격·뉴욕 시간·0.01 호가로 하루 돌려 보기 (python -m tests.sim_us live)."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

import bot.main as M
import bot.util as U
import tests.sim as S

NY = U.TZ["US"]
DAY = dt.date(2026, 10, 26)
OPEN = dt.datetime.combine(DAY, dt.time(9, 30), NY)


def up(t):
    m = (t - OPEN).total_seconds() / 60
    if m < 15:
        return 20.0 + (0.12 if int(m) % 2 else -0.08), 50000
    if m < 25:
        return 20.06, 40000
    if m < 26:
        return 20.32, 300000
    if m < 150:
        return 20.32 + (m - 26) * 0.012, 60000      # 약 +7%
    return max(21.8 - (m - 150) * 0.02, 20.5), 50000


def fail(t):
    m = (t - OPEN).total_seconds() / 60
    if m < 15:
        return 50.0 + (0.3 if int(m) % 2 else -0.2), 30000
    if m < 30:
        return 50.1, 25000
    if m < 31:
        return 50.8, 200000
    return max(50.8 - (m - 31) * 0.3, 47.0), 30000


def main(mode):
    root = Path(tempfile.mkdtemp())
    src = Path(__file__).resolve().parent.parent
    cfg = json.loads((src / "config.json").read_text())
    cfg["github_chart_url"] = ""
    cfg["live"]["enabled"] = True
    cfg["risk"]["US"].update(max_positions=2, budget_per_trade=500, max_loss_per_trade=15, daily_loss_limit=50)
    (root / "config.json").write_text(json.dumps(cfg))
    M.ROOT, M._API = root, None
    import bot.stream
    bot.stream.Stream = S.FakeStream
    M.TossAPI = S.FakeAPI
    S.PATHS.clear()
    S.PATHS.update({"UPUP": ("올라가는미국주", up), "DOWN": ("무너지는미국주", fail)})
    S.OPEN, S.DAY = OPEN, DAY
    S.snap = lambda px, sym: round(px, 2)
    S.FakeAPI.calendar = lambda self, m: {"today": {"date": DAY.isoformat(), "regularMarket": {
        "startTime": OPEN.isoformat(), "endTime": dt.datetime.combine(DAY, dt.time(16, 0), NY).isoformat()}}}
    S.FakeAPI.orderbook = lambda self, sym: {"asks": [{"price": str(self.price(sym) + 0.01)}], "bids": [{"price": str(self.price(sym))}]}
    def candles(self, sym, interval="1m", count=60):
        if interval == "1d":
            return [{"timestamp": (OPEN - dt.timedelta(days=1)).isoformat(), "closePrice": str(S.PATHS[sym][1](OPEN)[0] * 0.98)}]
        out, t = [], OPEN
        while t <= S.CLK.t:
            p0, v = S.PATHS[sym][1](t)
            p1 = S.PATHS[sym][1](t + dt.timedelta(seconds=59))[0]
            out.append({"timestamp": t.isoformat(), "openPrice": p0, "highPrice": max(p0, p1) + 0.02,
                        "lowPrice": min(p0, p1) - 0.02, "closePrice": p1, "volume": v})
            t += dt.timedelta(minutes=1)
        return out[-count:]
    S.FakeAPI.candles = candles
    S.FakeAPI.price = lambda self, sym: round(S.PATHS[sym][1](S.CLK.t)[0], 2)
    S.CLK.t = OPEN - dt.timedelta(minutes=5)
    U.now = M.now = S.CLK.now
    import time as T
    os_, ot_ = T.sleep, T.time
    b = M.Bot(mode, "US")

    def fs(sec):
        S.CLK.sleep(sec)
        if getattr(b, "universe", None) is not None and hasattr(b, "broker"):
            for sym in set(b.universe) | set(b.broker.pos):
                p, v = S.PATHS[sym][1](S.CLK.t)
                b.on_trade(sym, round(p, 2), 1, S.CLK.t.isoformat())
    T.sleep, T.time = fs, (lambda: S.CLK.t.timestamp())
    try:
        b.run()
    finally:
        T.sleep, T.time = os_, ot_
    print("\n결과:", [(p.name, p.qty, p.entry, p.exit_price, round(p.pnl, 2), p.exit_reason) for p in b.broker.closed])
    print("보유:", [(p.name, p.qty, p.sl, p.extra.get("carry")) for p in b.broker.pos.values()])
    if mode == "live":
        print([x for x in b.api.log if x[0] in ("order", "stop")][:8])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "paper")
