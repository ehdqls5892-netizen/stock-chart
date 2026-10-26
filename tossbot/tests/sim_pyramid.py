"""실전 모드 가짜 하루: 돌파 매수 → 올라서 본전 확보 → 고점 아래 쉼 → 재돌파에서 추가 매수 (python -m tests.sim_pyramid)."""
import tests.sim as S


def path(t):
    m = (t - S.OPEN).total_seconds() / 60
    if m < 15:
        return 10000 + (40 if int(m) % 2 else -40), 1000
    if m < 16:
        return 10080, 4000                       # 시초 돌파 (거래량 4배)
    if m < 40:
        return 10080 + (m - 16) * 20, 1500       # → 10560 까지 오름 (본전 확보)
    if m < 55:
        return 10500 + (20 if int(m) % 2 else -10), 800   # 고점(10560) 아래에서 좁게 쉼
    if m < 56:
        return 10600, 5000                       # 오늘 고점 재돌파 (거래량 급증) → 추가 매수
    if m < 200:
        return 10600 + (m - 56) * 4, 1500
    return 11150, 1000


if __name__ == "__main__":
    S.PATHS.clear()
    S.PATHS.update({"777777": ("불타기주", path)})
    b = S.main("live")
    adds = [x for x in b.api.log if x[0] == "order" and x[2] == "BUY"]
    print("매수 주문:", adds)
    assert len(adds) >= 2, "추가 매수가 안 됐어요"
    mods = [x for x in b.api.log if x[0] in ("modify", "stop")]
    print("손절 주문 기록(마지막 3개):", mods[-3:])
    print("✅ 실전 모드 추가 매수 시험 통과")
