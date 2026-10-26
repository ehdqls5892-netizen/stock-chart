"""안전장치 시험: 장 마감 전 정리 / 조건주문 실패 시 즉시 매도 / 하루 손실 한도 / 긴급 정지."""
from __future__ import annotations

import datetime as dt
import json

import tests.sim as S
from bot.api import TossError


def path_drift(t):  # 돌파 후 옆으로만 → 익절·손절 없이 장 마감 전 정리
    m = (t - S.OPEN).total_seconds() / 60
    if m < 15:
        return 30000 + (100 if int(m) % 2 else -100), 1000
    if m < 20:
        return 30050, 900
    if m < 21:
        return 30300, 7000
    return 30350, 800


def path_fail2(t):
    m = (t - S.OPEN).total_seconds() / 60
    base = 15000
    if m < 15:
        return base + (100 if int(m) % 2 else -60), 900
    if m < 40:
        return base + 40, 700
    if m < 41:
        return base + 250, 6000
    return max(base + 250 - (m - 41) * 100, 14000), 900


def reset(paths, cfg_patch=None):
    S.PATHS.clear()
    S.PATHS.update(paths)
    S.CLK.t = S.OPEN - dt.timedelta(minutes=5)
    if cfg_patch:
        orig = S.main.__globals__["json"].loads
        root = S.Path(S.__file__).resolve().parent.parent
        cfg = json.loads((root / "config.json").read_text())
        cfg_patch(cfg)
        S._cfg_override = cfg


def run(mode, paths, patch=None, api_patch=None):
    reset(paths)
    if api_patch:
        api_patch(S.FakeAPI)
    orig = S.Path.read_text
    if patch:
        root = S.Path(S.__file__).resolve().parent.parent

        def rt(self, *a, **k):
            txt = orig(self, *a, **k)
            if self.name == "config.json":
                c = json.loads(txt)
                patch(c)
                txt = json.dumps(c)
            return txt
        S.Path.read_text = rt
    try:
        return S.main(mode)
    finally:
        S.Path.read_text = orig


if __name__ == "__main__":
    print("\n### 1. 장 마감 전 정리 (실전)")
    def no_hold(c):
        c["carry"]["hold_if_intact"] = False   # 이 시험은 '마감 전 정리' 동작만 확인
        c["github_chart_url"] = ""
    b = run("live", {"444444": ("옆으로주", path_drift)}, no_hold)
    assert any(x[0] == "cancel_cond" for x in b.api.log), "조건주문 취소가 안 됐어요"
    assert any(x[0] == "order" and x[2] == "SELL" for x in b.api.log), "마감 전 매도가 안 됐어요"
    assert b.broker.closed and "마감" in b.broker.closed[0].exit_reason

    print("\n### 2. 조건주문 실패 → 즉시 매도 (실전)")
    orig_oco = S.FakeAPI.create_stop_market

    def bad(self, *a, **k):
        raise TossError(422, "condition-already-met", "테스트용 실패")
    S.FakeAPI.create_stop_market = bad
    try:
        b = run("live", {"444444": ("옆으로주", path_drift)})
    finally:
        S.FakeAPI.create_stop_market = orig_oco
    assert b.broker.closed and "조건주문 실패" in b.broker.closed[0].exit_reason

    print("\n### 3. 하루 손실 한도 (연습)")
    def lim(c):
        c["risk"]["KR"]["daily_loss_limit"] = 1000
        c["risk"]["KR"]["daily_loss_pct"] = 0.2      # 50만 원의 0.2% = 1,000원
        c["github_chart_url"] = ""
    b = run("paper", {"222222": ("무너지는주", S.path_fail), "555555": ("또무너지는주", path_fail2)}, lim)
    assert len(b.broker.closed) == 1, f"한도 뒤에도 매매함: {len(b.broker.closed)}"
    assert b.risk.halted

    print("\n### 4. 토스처럼 정정이 404 로 실패(옛 주문도 사라짐) → 새로 걸고 계속 (실전)")
    orig_mod = S.FakeAPI.modify_stop_market

    def mod404(self, cid, *a, **k):
        self.conds[cid]["status"] = "CANCELED"
        raise TossError(404, "conditional-order-not-found", "존재하지 않는 설정입니다.")
    S.FakeAPI.modify_stop_market = mod404
    try:
        b = run("live", {"111111": ("올라가는주", S.path_up)})
    finally:
        S.FakeAPI.modify_stop_market = orig_mod
    stops = [x for x in b.api.log if x[0] == "stop"]
    assert len(stops) >= 3, f"새로 걸기가 안 됨: {stops}"
    alive = [k for k, c in b.api.conds.items() if c["status"] == "WATCHING"]
    assert len(alive) <= 1, f"살아 있는 손절이 여러 개: {alive}"
    assert b.broker.closed and b.broker.closed[-1].pnl > 0, [(p.exit_reason, p.pnl) for p in b.broker.closed]

    print("\n✅ 안전장치 시험 통과")
