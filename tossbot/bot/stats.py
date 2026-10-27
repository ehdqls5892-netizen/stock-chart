"""성적표: 실전 매매 기록(logs/live/<시장>/trades-*.jsonl)으로 최근 N일 성과를 계산한다.

투자금을 늘릴지 판단할 때 보는 숫자
  - 승률, 평균 이익 / 평균 손실(손익비), 1회 기대값(=한 번 매매할 때 평균 얼마 버는지)
  - 최대 낙폭: 누적 손익이 고점에서 가장 많이 줄었던 폭
  - 매수 방식별 성과: 어떤 방식이 돈을 벌고 어떤 방식이 까먹는지
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

SETUP_NAMES = {"open": "장초반 출발", "orb": "시초 돌파", "hod": "고점 돌파", "vwap": "평균단가 반등", "plan": "데스크 계획",
               "support": "지지선 반등", "r2g": "갭하락 회복", "?": "기타"}
_KEYS = [("장초반", "open"), ("시초", "orb"), ("오늘 고점", "hod"), ("VWAP", "vwap"), ("데스크", "plan"),
         ("일봉", "support"), ("갭하락", "r2g")]


def setup_of(rec: dict) -> str:
    s = (rec.get("extra") or {}).get("setup")
    if s:
        return s
    r = rec.get("reason") or ""
    for k, v in _KEYS:
        if k in r:
            return v
    return "?"


def load(root: Path, market: str, days: int):
    d = root / "logs" / "live" / market
    since = dt.date.today() - dt.timedelta(days=days)
    out = []
    for f in sorted(d.glob("trades-*.jsonl")) if d.exists() else []:
        try:
            day = dt.date.fromisoformat(f.stem[7:])
        except ValueError:
            continue
        if day < since:
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") in ("exit", "partial") and r.get("exit_price"):
                r["_day"] = day.isoformat()
                out.append(r)
    return out


def summarize(recs: list, market: str, capital: float | None = None) -> str:
    fmt = (lambda v: f"${v:,.2f}") if market == "US" else (lambda v: f"{v:,.0f}원")
    if not recs:
        return "아직 기록이 없어요"
    pnl = [float(r.get("pnl") or 0) for r in recs]
    wins = [x for x in pnl if x > 0]
    loss = [x for x in pnl if x <= 0]
    tot = sum(pnl)
    aw = sum(wins) / len(wins) if wins else 0
    al = -sum(loss) / len(loss) if loss else 0
    eq, peak, mdd = 0.0, 0.0, 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    days = len({r["_day"] for r in recs})
    lines = [f"매매 {len(recs)}건({days}일) · 승률 {len(wins) / len(recs) * 100:.0f}% · 합계 {fmt(tot)} (하루 평균 {fmt(tot / days)})",
             f"평균 이익 {fmt(aw)} / 평균 손실 {fmt(al)}" + (f" → 손익비 {aw / al:.1f}" if al else "")
             + f" · 1회 기대값 {fmt(tot / len(recs))}",
             f"최대 낙폭 {fmt(mdd)}" + (f" (투자금의 {mdd / capital * 100:.1f}%)" if capital else "")]
    by: dict[str, list] = {}
    for r, x in zip(recs, pnl):
        by.setdefault(setup_of(r), []).append(x)
    rows = sorted(by.items(), key=lambda kv: -sum(kv[1]))
    lines.append("방식별: " + " · ".join(
        f"{SETUP_NAMES.get(k, k)} {len(v)}건 {sum(1 for x in v if x > 0) / len(v) * 100:.0f}% {fmt(sum(v))}" for k, v in rows))
    return "\n".join(lines)


def verdict(recs: list, capital: float | None) -> str:
    """투자금을 늘려도 되는지 — 기준을 모두 넘을 때만 '늘려도 됨'."""
    if len(recs) < 30 or len({r["_day"] for r in recs}) < 10:
        return f"⏳ 아직 판단 이르다 (기록 {len(recs)}건 · 최소 30건, 거래일 10일 필요)"
    pnl = [float(r.get("pnl") or 0) for r in recs]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    wins = [x for x in pnl if x > 0]
    loss = [-x for x in pnl if x <= 0]
    pf = sum(wins) / sum(loss) if loss and sum(loss) else 9.9
    ok = sum(pnl) > 0 and pf >= 1.3 and (not capital or mdd <= capital * 0.08)
    return ("✅ 늘려도 되는 기준 통과 (수익 + 이익합/손실합 " f"{pf:.2f} ≥ 1.3 + 최대 낙폭 투자금 8% 이내) → 한 번에 50%까지만 늘리기 추천"
            if ok else f"🟡 아직 유지 추천 (이익합/손실합 {pf:.2f}, 기준 1.3 · 최대 낙폭 기준 투자금 8% 이내)")


def report(root: Path, days: int = 14, capitals: dict | None = None) -> str:
    capitals = capitals or {}
    out = [f"📈 최근 {days}일 성적 (실전)"]
    for mk, nm in (("KR", "🇰🇷 국장"), ("US", "🇺🇸 미장")):
        recs = load(root, mk, days)
        out.append(f"\n{nm}\n" + summarize(recs, mk, capitals.get(mk)))
        if recs:
            out.append(verdict(load(root, mk, 60), capitals.get(mk)))
    return "\n".join(out)
