"""공통 도구: 시간대, 호가 단위, 숫자 변환."""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

TZ = {"KR": ZoneInfo("Asia/Seoul"), "US": ZoneInfo("America/New_York")}


def now(market: str) -> dt.datetime:
    return dt.datetime.now(TZ[market])


def f(x, default=None):
    """문자열 숫자를 float 로. 실패하면 default."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def kr_tick(price: float) -> int:
    """국내 주식 호가 단위 (2023년 개편, 코스피·코스닥 공통)."""
    if price < 2000:
        return 1
    if price < 5000:
        return 5
    if price < 20000:
        return 10
    if price < 50000:
        return 50
    if price < 200000:
        return 100
    if price < 500000:
        return 500
    return 1000


def tick(market: str, price: float) -> float:
    return kr_tick(price) if market == "KR" else 0.01


def round_tick(market: str, price: float, mode: str = "near") -> float:
    """호가 단위에 맞춘다. mode: near / up / down."""
    t = tick(market, price)
    q = price / t
    q = math.ceil(q - 1e-9) if mode == "up" else math.floor(q + 1e-9) if mode == "down" else round(q)
    v = q * t
    return float(int(v)) if market == "KR" else round(v, 2)


def add_ticks(market: str, price: float, n: int) -> float:
    p = price
    step = 1 if n > 0 else -1
    for _ in range(abs(n)):
        p = round_tick(market, p + step * tick(market, p), "up" if step > 0 else "down")
    return p


def price_str(market: str, price: float) -> str:
    return str(int(round(price))) if market == "KR" else f"{price:.2f}"


def money(market: str, v: float) -> str:
    return f"{v:,.0f}원" if market == "KR" else f"${v:,.2f}"


def parse_hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def parse_ts(s: str | None, market: str) -> dt.datetime | None:
    """ISO 8601 시각을 그 시장 시간대로."""
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=TZ["KR"])
    return t.astimezone(TZ[market])
