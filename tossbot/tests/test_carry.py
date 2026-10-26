"""이틀 시험: 1일차에 이익 난 종목을 들고 가고, 2일차에 이어받아 추적 손절로 파는지."""
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
from bot.util import kr_tick

D1, D2 = dt.date(2026, 10, 26), dt.date(2026, 10, 27)
O1 = dt.datetime.combine(D1, dt.time(9, 0), U.TZ["KR"])
O2 = dt.datetime.combine(D2, dt.time(9, 0), U.TZ["KR"])


def strong(t):
    """1일차: 돌파 후 장 끝까지 꾸준히 상승 (강하게 마감). 2일차: 더 오르다가 오후에 꺾임."""
    if t.date() == D1:
        m = (t - O1).total_seconds() / 60
        if m < 15:
            return 10000 + (60 if int(m) % 2 else -40), 1000
        if m < 25:
            return 10030, 900
        if m < 26:
            return 10160, 6000
        return 10160 + (m - 26) * 2.2, 1500          # 15:15 쯤 약 +7%
    m = (t - O2).total_seconds() / 60
    if m < 120:
        return 10950 + m * 6, 1500                   # 11:00 까지 약 +18%
    return max(11670 - (m - 120) * 15, 10000), 1300  # 오후에 꺾임


def weak(t):  # 돌파 후 거의 제자리 → 수익 기준 미달이라 들고 가지 않음
    base = 30000
    if t.date() != D1:
        return base, 100
    m = (t - O1).total_seconds() / 60
    if m < 15:
        return base + (100 if int(m) % 2 else -100), 1000
    if m < 20:
        return base + 50, 900
    if m < 21:
        return base + 300, 7000
    return base + 350, 800


def run_day(mode, root, day, open_t):
    S.DAY, S.OPEN = day, open_t
    S.CLK.t = open_t - dt.timedelta(minutes=5)
    U.now = S.CLK.now
    M.now = S.CLK.now
    import time as T
    os_, ot_ = T.sleep, T.time
    b = M.Bot(mode)

    def fs(sec):
        S.CLK.sleep(sec)
        if getattr(b, "universe", None) is not None and hasattr(b, "broker"):
            for sym in set(b.universe) | set(b.broker.pos):
                if sym in S.PATHS:
                    p, v = S.PATHS[sym][1](S.CLK.t)
                    b.on_trade(sym, S.snap(p, sym), 1, S.CLK.t.isoformat())
    T.sleep, T.time = fs, (lambda: S.CLK.t.timestamp())
    try:
        b.run()
    finally:
        T.sleep, T.time = os_, ot_
    return b


def main(mode):
    root = Path(tempfile.mkdtemp())
    src = Path(__file__).resolve().parent.parent
    cfg = json.loads((src / "config.json").read_text())
    cfg["live"]["enabled"] = True
    cfg["github_chart_url"] = ""
    cfg["risk"]["KR"].update(max_positions=2, budget_per_trade=300000, max_loss_per_trade=9000, daily_loss_limit=25000)
    cfg["carry"]["max_positions"] = 2
    cfg["carry"]["hold_if_intact"] = False      # 이 시험은 '수익 기준' 들고 가기만 확인 (손절선 유지 보유는 test_pyramid 에서)
    (root / "config.json").write_text(json.dumps(cfg))
    M.ROOT = root
    M._API = None
    import bot.stream
    bot.stream.Stream = S.FakeStream
    M.TossAPI = S.FakeAPI
    S.PATHS.clear()
    S.PATHS.update({"777777": ("강한주", strong), "888888": ("약한주", weak)})

    # FakeAPI 보강: 보유 조회 · 조건주문 상태 (이틀 공유)
    shared = S.FakeAPI()
    S.FakeAPI.__new__ = lambda cls, *a, **k: shared
    S.FakeAPI.__init__ = lambda self, *a, **k: None

    def holdings(self):
        q = {}
        for o in self.orders.values():
            q[o["sym"]] = q.get(o["sym"], 0) + (o["q"] if o["side"] == "BUY" else -o["q"])
        return {"items": [{"symbol": k, "quantity": str(v)} for k, v in q.items() if v > 0]}
    S.FakeAPI.holdings = holdings

    print("\n========== 1일차 ==========")
    b1 = run_day(mode, root, D1, O1)
    st = json.loads((root / "logs" / ("paper" if mode == "paper" else "live") / "KR" / "state.json").read_text())
    carried = [p for p in st["positions"] if p["extra"].get("carry")]
    print("들고 간 종목:", [(p["name"], p["qty"], p["sl"]) for p in carried])
    assert [p["name"] for p in carried] == ["강한주"], "강한주만 들고 가야 해요"
    assert any(p.name == "약한주" and "기준" in p.exit_reason for p in b1.broker.closed), "약한주는 팔아야 해요"

    print("\n========== 2일차 ==========")
    b2 = run_day(mode, root, D2, O2)
    sold = [p for p in b2.broker.closed if p.name == "강한주"]
    assert sold, "2일차에 강한주를 팔아야 해요"
    print("2일차 강한주:", [(p.qty, p.entry, p.exit_price, round(p.pnl), p.exit_reason) for p in sold])
    if mode == "live":
        print("\n--- 서버 요청 (조건주문 관련) ---")
        for x in shared.log:
            if x[0] in ("stop", "cancel_cond") or (x[0] == "modify" and not x[4].startswith("2026-10-26")):
                print(x)
    print("\n✅ 들고 가기 시험 통과")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "paper")
