"""장기 ETF 모으기 (배당·장기 목적, 🔒 잠금으로 계속 보유 · 손절 없음).

근거 (tools 백테스트, 지수 3년): '근거가 있을 때만 사기'(추세 속 눌림·큰 하락) 는 매달 같은 금액 사기보다
평균 단가가 낫지 않았음 (SPY 643 vs 642, QQQ 579 vs 583, 코스피 3,760 vs 4,284 — 오히려 나쁨, 현금도 놀게 됨).
그래서 가장 근거가 확실한 방법 = 정해진 금액을 매달 첫 거래일에 나눠 사는 것(적립식).
  - 레버리지·인버스 ETF 는 목록에 넣지 않음(오래 들면 녹아내림)
  - 1주 값보다 이번 달 몫이 적으면 다음 달로 이월해서 모아 삼
  - 로봇 손절·장 마감 정리·전부 팔기·긴급 정지 모두에서 빠짐 (팔 때는 텔레그램 💰 팔기로 직접)
"""
from __future__ import annotations


def plan(budget: float, items: list, bank: dict, prices: dict, cash: float | None):
    """이번 달 살 수량. items: [[코드, 이름, 비중], ...] → ([(코드, 이름, 수량, 값)], 새 이월금)"""
    bank = dict(bank)
    tot_w = sum(float(w) for _, _, w in items) or 1
    buys = []
    left = cash
    for code, name, w in items:
        bank[code] = bank.get(code, 0.0) + budget * float(w) / tot_w
        px = prices.get(code)
        if not px:
            continue
        q = int(bank[code] // px)
        if left is not None:
            q = min(q, int(left * 0.98 // px))
        if q >= 1:
            buys.append((code, name, q, px))
            bank[code] -= q * px
            if left is not None:
                left -= q * px
    return buys, bank
