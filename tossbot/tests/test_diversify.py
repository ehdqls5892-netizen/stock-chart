"""같이 움직이는 종목 몰빵 방지 시험 (python -m tests.test_diversify)."""
import datetime as dt
import math

from bot.strategy import Bar, SymState, corr
from bot.util import TZ

OPEN = dt.datetime(2026, 10, 27, 9, 0, tzinfo=TZ["KR"])


def mk(sym, fn):
    st = SymState(sym, sym)
    for m in range(60):
        c = fn(m)
        t = OPEN + dt.timedelta(minutes=m)
        st.bars[t] = Bar(t, c, c, c, c, 100)
    return st


a = mk("A", lambda m: 10000 * (1 + 0.004 * math.sin(m / 3) + 0.0005 * m))
b = mk("B", lambda m: 5000 * (1 + 0.005 * math.sin(m / 3) + 0.0004 * m))      # A 와 같이 움직임
c = mk("C", lambda m: 8000 * (1 + 0.004 * math.cos(m * 1.7)))                  # 따로 움직임
rab, rac = corr(a, b, OPEN), corr(a, c, OPEN)
print(f"A-B {rab:.2f} · A-C {rac:.2f}")
assert rab > 0.9 and abs(rac) < 0.5
assert corr(a, mk("D", lambda m: 1000), OPEN) is None      # 안 움직이면 판단 안 함
print("✅ 몰빵 방지 시험 통과")
