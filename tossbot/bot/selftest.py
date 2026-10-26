"""실전 주문 시험 (장중에만): 진짜 주문 흐름이 토스에서 그대로 통하는지 1주로 확인.

순서
  1) 싼 종목 1주 시장가 매수 → 체결 확인
  2) 시장가 손절 조건주문 등록 (현재가 −5%) → 조회
  3) 손절선 올리기(수정) → 조회
  4) 조건주문 취소 → 1주 시장가 매도 → 체결 확인
비용: 수수료·세금·호가 차이로 보통 수십~수백 원.
"""
from __future__ import annotations

import json
import os
import sys
import time

from .api import TossAPI, TossError
from .main import ROOT, load_env
from .util import f, now, price_str, round_tick


def wait_fill(api, oid, sec=20):
    t = time.time()
    while time.time() - t < sec:
        o = api.get_order(oid)
        if o.get("status") == "FILLED":
            return o
        if o.get("status") in ("CANCELED", "REJECTED"):
            raise RuntimeError(f"주문 {o.get('status')}")
        time.sleep(1)
    raise RuntimeError("20초 안에 체결되지 않았어요")


def main():
    load_env()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    from .main import markets_of
    m = (sys.argv[1].upper() if len(sys.argv) > 1 else None)
    if m not in ("KR", "US"):
        from .util import now as _now
        import datetime as _dt
        # 지금 열려 있는 시장으로: 한국 09~15:30 이면 KR, 아니면 US
        k = _now("KR")
        m = "KR" if (k.weekday() < 5 and _dt.time(9, 0) <= k.time() <= _dt.time(15, 20)) else "US"
    sym = cfg.get("selftest_symbol", {}).get(m) or ("010140" if m == "KR" else "F")
    api = TossAPI(os.getenv("TOSS_CLIENT_ID", ""), os.getenv("TOSS_CLIENT_SECRET", ""), ROOT / ".token.json")
    accs = api.accounts() or []
    api.account_seq = int(next(a for a in accs if a.get("accountType") == "BROKERAGE")["accountSeq"])
    print(f"시험 종목 {sym} · {now(m):%m/%d %H:%M} ({m})")
    price = f((api.prices([sym]) or [{}])[0].get("lastPrice"))
    print(f"현재가 {price}")
    ok = []
    cond = None
    try:
        print("1) 1주 시장가 매수 …", end=" ", flush=True)
        o = wait_fill(api, api.create_order(sym, "BUY", 1, "MARKET", None, None)["orderId"])
        buy = f((o.get("execution") or {}).get("averageFilledPrice"), price)
        print(f"체결 {buy}")
        ok.append("매수")
        print("2) 시장가 손절 조건주문 등록 (−5%) …", end=" ", flush=True)
        sl = round_tick(m, buy * 0.95, "down")
        cond = api.create_stop_market(sym, 1, price_str(m, sl), now(m).date().isoformat())["conditionalOrderId"]
        st = api.get_conditional(cond).get("status")
        print(f"등록됨 ({st})")
        ok.append("손절 등록")
        print("3) 손절선 올리기 (−4%) …", end=" ", flush=True)
        sl2 = round_tick(m, buy * 0.96, "down")
        cond, how = api.replace_stop_market(cond, sym, 1, price_str(m, sl2), now(m).date().isoformat())
        c = api.get_conditional(cond)
        print(f"바뀜 · 방법: {how} · 새 손절선 {(c.get('first') or {}).get('triggerPrice')} ({c.get('status')})")
        ok.append("손절 수정")
    except (TossError, RuntimeError, StopIteration) as e:
        print(f"\n❌ 실패: {e}")
        if isinstance(e, TossError) and e.code == "order-hours-closed":
            print("   → 장이 열려 있을 때(정규장) 다시 실행하세요.")
        if isinstance(e, TossError) and e.code == "investor-exchange-not-integrated":
            print("   → 토스 앱 설정에서 주문 거래소를 '통합(SOR)'으로 바꿔 주세요.")
        if isinstance(e, TossError) and e.code == "prerequisite-required":
            print("   → 토스 앱에서 해당 주문의 약관 동의·위험 고지 등을 먼저 완료해야 해요.")
    finally:
        if cond:
            try:
                api.cancel_conditional(cond)
                print("4) 조건주문 취소 … OK")
                ok.append("손절 취소")
            except TossError as e:
                print(f"4) 조건주문 취소 실패: {e}")
        # 남아 있는 이 종목 조건주문 전부 정리 (시험 찌꺼기 방지)
        left = api.open_stops_for(sym)
        for c in left:
            try:
                api.cancel_conditional(c.get("conditionalOrderId"))
                print(f"   남아 있던 조건주문 {c.get('conditionalOrderId')} 취소")
            except TossError as e:
                print(f"   남은 조건주문 취소 실패: {e} — 토스 앱 조건주문 목록에서 직접 취소하세요")
        if cond and "손절 취소" not in ok and not api.open_stops_for(sym):
            ok.append("손절 취소")
        if "매수" in ok:
            try:
                time.sleep(1)
                o = wait_fill(api, api.create_order(sym, "SELL", 1, "MARKET", None, None)["orderId"])
                print(f"5) 1주 시장가 매도 … 체결 {(o.get('execution') or {}).get('averageFilledPrice')}")
                ok.append("매도")
            except (TossError, RuntimeError) as e:
                print(f"5) 매도 실패: {e} — 토스 앱에서 {sym} 1주를 직접 파세요")
    need = ["매수", "손절 등록", "손절 수정", "손절 취소", "매도"]
    print("\n" + ("✅ 실전 주문 흐름 모두 정상" if all(x in ok for x in need) else f"⚠️ 확인된 단계: {', '.join(ok) or '없음'} — 위 오류 화면을 캡처해서 보내 주세요"))


if __name__ == "__main__":
    main()
