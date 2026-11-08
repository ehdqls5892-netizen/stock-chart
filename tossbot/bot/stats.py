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

SETUP_NAMES = {"gapdown": "갭하락 반등", "open": "장초반 출발", "orb": "시초 돌파", "hod": "고점 돌파", "vwap": "평균단가 반등", "plan": "데스크 계획",
               "support": "지지선 반등", "r2g": "갭하락 회복", "sw_ep": "🧭스윙·재료 갭", "sw_hi52": "🧭스윙·52주 신고가", "sw_dip": "🧭스윙·하락장 저점", "sw_fs": "스윙·현재·미래 점수", "?": "기타"}
_KEYS = [("갭하락 반등", "gapdown"), ("장초반", "open"), ("시초", "orb"), ("오늘 고점", "hod"), ("VWAP", "vwap"), ("데스크", "plan"),
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


def load(root: Path, market: str, days: int, mode: str = "live"):
    d = root / "logs" / mode / market
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


def merge(recs: list) -> list:
    """나눠 익절(partial)과 마지막 매도(exit)를 한 매매로 합친다 — 따로 세면 작은 익절이 '이긴 매매'로 여러 번 잡혀 승률이 부풀려짐."""
    out: dict = {}
    for r in recs:
        k = (r.get("symbol"), r.get("opened_at")) if r.get("symbol") and r.get("opened_at") else id(r)
        if k not in out:
            out[k] = {**r, "pnl": 0.0, "_parts": 0, "_qty": 0}
        m = out[k]
        m["pnl"] += float(r.get("pnl") or 0)
        m["peak"] = max(float(m.get("peak") or 0), float(r.get("peak") or 0))
        m["_qty"] += int(r.get("qty") or 0)
        m["_parts"] += 1
        m["_day"] = r["_day"]
        if r.get("kind") == "exit":
            m["exit_reason"], m["exit_price"], m["kind"] = r.get("exit_reason"), r.get("exit_price"), "exit"
            m["extra"] = {**(m.get("extra") or {}), **(r.get("extra") or {})}
    return list(out.values())


def summarize(recs: list, market: str, capital: float | None = None) -> str:
    recs = merge(recs)
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
    byday: dict = {}
    for r, x in zip(recs, pnl):
        byday[r["_day"]] = byday.get(r["_day"], 0) + x
    dv = list(byday.values())
    sharpe = None
    if len(dv) >= 5:
        mu = sum(dv) / len(dv)
        sd = (sum((x - mu) ** 2 for x in dv) / (len(dv) - 1)) ** 0.5
        sharpe = mu / sd * 252 ** 0.5 if sd else None
    lines = [f"매매 {len(recs)}건({days}일) · 승률 {len(wins) / len(recs) * 100:.0f}% · 합계 {fmt(tot)} (하루 평균 {fmt(tot / days)})",
             f"평균 이익 {fmt(aw)} / 평균 손실 {fmt(al)}" + (f" → 손익비 {aw / al:.1f}" if al else "")
             + f" · 1회 기대값 {fmt(tot / len(recs))}",
             f"최대 낙폭 {fmt(mdd)}" + (f" (투자금의 {mdd / capital * 100:.1f}%)" if capital else "")
             + (f" · 샤프 {sharpe:+.2f}" + (" (마이너스 → 고칠 필요)" if sharpe < 0 else "") if sharpe is not None else "")]
    by: dict[str, list] = {}
    for r, x in zip(recs, pnl):
        by.setdefault(setup_of(r), []).append(x)
    rows = sorted(by.items(), key=lambda kv: -sum(kv[1]))
    def tag(v):
        w, l = sum(x for x in v if x > 0), -sum(x for x in v if x <= 0)
        pf = w / l if l else 9.9
        return "✅" if pf >= 1.2 else "⚠️" if pf >= 0.8 else "❌"
    lines.append("방식별: " + " · ".join(
        f"{tag(v)}{SETUP_NAMES.get(k, k)} {len(v)}건 {sum(1 for x in v if x > 0) / len(v) * 100:.0f}% {fmt(sum(v))}" for k, v in rows))
    bg: dict[str, list] = {}
    for r, x in zip(recs, pnl):
        bg.setdefault((r.get("extra") or {}).get("grade") or "?", []).append(x)
    if len(bg) > 1 or "?" not in bg:
        order = ["A+", "A", "B", "C", "?"]
        lines.append("확신도별: " + " · ".join(
            f"{tag(bg[g])}{g if g != '?' else '기록없음'} {len(bg[g])}건 {fmt(sum(bg[g]))}" for g in order if g in bg))
    return "\n".join(lines)


def verdict(recs: list, capital: float | None) -> str:
    """투자금을 늘려도 되는지 — 기준을 모두 넘을 때만 '늘려도 됨'."""
    recs = merge(recs)
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


def diagnose(root: Path, days: int = 7) -> str:
    """매매 하나하나: 날짜·종목·방식·확신도·수익률·손익·판 이유 (원인 찾기용)."""
    out = [f"🔍 최근 {days}일 매매 진단 (나눠 익절은 한 매매로 합침)"]
    for mk, nm in (("KR", "🇰🇷 국장"), ("US", "🇺🇸 미장")):
        tr = merge(load(root, mk, days, "live"))
        if not tr:
            continue
        fmt = (lambda v: f"${v:,.2f}") if mk == "US" else (lambda v: f"{v:,.0f}원")
        out.append(f"\n{nm} {len(tr)}건")
        for r in tr:
            e, x = float(r.get("entry") or 0), float(r.get("exit_price") or 0)
            ex = r.get("extra") or {}
            R = ex.get("R")
            rm = f" {((x - e) / R):+.1f}R" if R and e and x else ""
            sl_pct = f" 손절폭{(R / e * 100):.1f}%" if R and e else ""
            nights = len(ex.get("days_held", []))
            pk = float(r.get("peak") or 0)
            best = f" 최고{(pk / e - 1) * 100:+.1f}%" if pk > e > 0 else ""
            out.append(f"{'🟢' if r['pnl'] > 0 else '🔴'} {r['_day'][5:]} {r.get('name')} {SETUP_NAMES.get(setup_of(r), '?')}"
                       f"/{ex.get('grade') or '-'} {(x / e - 1) * 100 if e and x else 0:+.1f}%{best}{rm} {fmt(r['pnl'])}{sl_pct}"
                       + (f" 밤{nights}" if nights else "") + f" · {(r.get('exit_reason') or '')[:22]}")
    return "\n".join(out) if len(out) > 1 else "최근 실전 매매 기록이 없어요"


def buys(root: Path, days: int = 14, key: str = "") -> str:
    """매수 주문 수량(계획) vs 실제 체결 수량 — '왜 이것밖에 안 샀지?' 확인용."""
    out = [f"🧾 최근 {days}일 매수 기록 (계획 → 체결)" + (f" · '{key}'" if key else "")]
    since = dt.date.today() - dt.timedelta(days=days)
    for mk, nm in (("KR", "🇰🇷"), ("US", "🇺🇸")):
        d = root / "logs" / "live" / mk
        sent, armed = {}, {}
        for f in sorted(d.glob("trades-*.jsonl")) if d.exists() else []:
            try:
                if dt.date.fromisoformat(f.stem[7:]) < since:
                    continue
            except ValueError:
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                k = (r.get("symbol"), r.get("opened_at"))
                if r.get("kind") == "buy_sent":
                    sent[k] = r
                elif r.get("kind") == "armed":
                    armed[k] = r
        for k, r in sent.items():
            if key and key not in (r.get("name") or "") and key.upper() != str(r.get("symbol")).upper():
                continue
            a = armed.get(k)
            got = a.get("qty") if a else "체결 안 됨"
            mark = "" if a and a.get("qty") == r.get("qty") else " ⚠️"
            out.append(f"{nm} {str(r.get('opened_at'))[5:16]} {r.get('name')} 계획 {r.get('qty')}주 → 체결 {got}{'주' if a else ''}{mark}"
                       f" · {(r.get('reason') or '')[:30]}")
    return "\n".join(out) if len(out) > 1 else "매수 기록이 없어요"


def shadow_report(root: Path, days: int = 30) -> str:
    """단타 그림자 장부(실제 매매 아님) 성적 — 단타를 다시 켤지 판단용."""
    out = [f"👻 단타 그림자 최근 {days}일 (실제 주문 없음 · 단타 신호대로 샀다면)"]
    for mk, nm in (("KR", "🇰🇷 국장"), ("US", "🇺🇸 미장")):
        recs = load(root, mk, days, "shadow")
        out.append(f"{nm}\n" + summarize(recs, mk))
    out.append("판단 기준: 거래일 10일·30건 넘게 쌓였을 때 합계 플러스 + 이익합/손실합 1.3 이상이면 단타를 다시 켜 볼 만해요")
    return "\n".join(out)


def recent_pnl(root: Path, market: str, days: int) -> tuple:
    tr = merge(load(root, market, days, "live"))
    return len(tr), sum(float(r.get("pnl") or 0) for r in tr)


def report(root: Path, days: int = 14, capitals: dict | None = None) -> str:
    capitals = capitals or {}
    out = []
    for mode, title in (("live", "실전"), ("paper", "연습")):
        if mode == "paper" and not any(load(root, mk, days, "paper") for mk in ("KR", "US")):
            continue
        out.append(f"📈 최근 {days}일 성적 ({title})")
        for mk, nm in (("KR", "🇰🇷 국장"), ("US", "🇺🇸 미장")):
            recs = load(root, mk, days, mode)
            out.append(f"{nm}\n" + summarize(recs, mk, capitals.get(mk)))
            if recs:
                out.append(verdict(load(root, mk, 60, mode), capitals.get(mk)))
        out.append("")
    return "\n".join(out).strip()
