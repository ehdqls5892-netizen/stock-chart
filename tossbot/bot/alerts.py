"""장중 급등·급락 감시 → 휴대폰 앱 알림 (Claude 사용량 없음, 2분마다).

- 보유 종목: +5% · +10% 오를 때, -5% · -8% 내릴 때 (같은 날 같은 단계는 한 번만)
- 후보·데스크 종목(오늘 살 만한 것): +5% · +10% · +20% 오를 때
- 시장 전체: 토스 '실시간 상승률' 상위에서 처음 보는 +15% 넘는 종목 (시장마다 하루 최대 5개)
로봇이 켜져 있고 그 시장이 장중일 때만 본다.
"""
from __future__ import annotations

import threading
import time

LEVELS_HOLD = (10, 5, -8, -5)
LEVELS_WATCH = (20, 10, 5)
MARKET_MIN = 15.0
MARKET_CAP = 5

_sent: set = set()
_prev: dict = {}
_mkt: dict = {}
_last_rank: dict = {}


def prev_close(sym: str, today: str, bars=None) -> float | None:
    k = (sym, today)
    if k in _prev:
        return _prev[k]
    from . import mobile as MB
    b = (bars or MB.bars)(sym)
    done = [x for x in b if x[0] < today]
    v = done[-1][4] if done else None
    _prev[k] = v
    return v


def crossed(pct: float, levels) -> int | None:
    """지금 등락률이 넘은 가장 큰 단계 (없으면 None)."""
    up = [lv for lv in levels if lv > 0 and pct >= lv]
    dn = [lv for lv in levels if lv < 0 and pct <= lv]
    if up:
        return max(up)
    if dn:
        return min(dn)
    return None


def _fire(key_base, lv, levels, fn):
    """같은 날 같은 단계(또는 더 낮은 단계)는 다시 안 보냄."""
    if (key_base, lv) in _sent:
        return False
    for x in levels:
        if (lv > 0 and 0 < x <= lv) or (lv < 0 and lv <= x < 0):
            _sent.add((key_base, x))
    fn()
    return True


def watch_list(mk: str) -> dict:
    """후보(현재·미래 점수 B 이상) + 데스크 가격 계획 종목 → {code: (name, 설명)}"""
    from . import mobile as MB
    out = {}
    try:
        for x in (MB.picks(mk).get("picks") or [])[:12]:
            if (x.get("score") or 0) >= 2 and not ((x.get("ai") or {}).get("veto")):
                out[x["code"]] = (x["name"], f"후보 종목 · 점수 {x.get('grade')} {int(round(x['score'])):+d}")
    except Exception:
        pass
    try:
        d = MB.desk(mk)
        for p in d["live"]["plans"] + d["report"]["plans"][:6]:
            out.setdefault(p["code"], (p.get("name") or p["code"], f"데스크 {p.get('src') or '계획'} 종목"))
    except Exception:
        pass
    return out


def check(mk: str, quotes=None, bars=None, rankings=None, names=None, now_fn=None):
    from . import app as A
    from . import main as M
    from . import push
    b = A.STATE.get("bots", {}).get(mk)
    if b is None or not getattr(b, "loop_on", False):
        return
    today = (now_fn or M.now)(mk).date().isoformat()
    money = lambda v: M.money(mk, v)
    hold = {p.symbol: p for p in b.broker.pos.values() if p.state == "open"}
    watch = {c: v for c, v in watch_list(mk).items() if c not in hold}
    syms = list(hold) + list(watch)[:30]
    q = (quotes or A.quotes)(syms)
    for sym in syms:
        last, pc = q.get(sym), prev_close(sym, today, bars)
        if not last or not pc:
            continue
        pct = (last / pc - 1) * 100
        if sym in hold:
            p = hold[sym]
            lv = crossed(pct, LEVELS_HOLD)
            if lv is None:
                continue
            pnl = (last - p.entry) * p.qty
            if lv > 0:
                t, body = f"🚀 {p.name} {pct:+.1f}% 오르는 중", f"보유 {p.qty}주 · 평가 {'+' if pnl >= 0 else '-'}{money(abs(pnl))}"
            else:
                t, body = f"📉 {p.name} {pct:+.1f}% 내리는 중", f"보유 {p.qty}주 · 손절선 {money(p.sl)}까지 {(p.sl / last - 1) * 100:+.1f}%"
            _fire((today, mk, sym), lv, LEVELS_HOLD, lambda: push.push_event("move_hold", f"{M.FLAG[mk]} {t}", body, mk, sym))
        else:
            lv = crossed(pct, LEVELS_WATCH)
            if lv is None:
                continue
            nm, why = watch[sym]
            _fire((today, mk, sym), lv, LEVELS_WATCH,
                  lambda: push.push_event("move_watch", f"{M.FLAG[mk]} 🚀 {nm} {pct:+.1f}% 오르는 중", f"{why} · 탭해서 차트 보기", mk, sym))
    # 시장 전체 급등 (10분마다)
    if time.time() - _last_rank.get(mk, 0) < 600:
        return
    _last_rank[mk] = time.time()
    n = sum(1 for k in _mkt if k[0] == today and k[1] == mk)
    if n >= MARKET_CAP:
        return
    try:
        r = (rankings or (lambda: M.shared_api().rankings("TOP_GAINERS", mk, "realtime", 10, True)))()
        cands = [it.get("symbol") for it in (r.get("rankings") or []) if it.get("symbol")]
    except Exception:
        return
    cands = [c for c in cands if c not in hold and c not in watch and (today, mk, c) not in _mkt][:8]
    if not cands:
        return
    q = (quotes or A.quotes)(cands)
    try:
        nmap = names(cands) if names else {x.get("symbol"): x.get("name") for x in (M.shared_api().stocks(cands) or [])}
    except Exception:
        nmap = {}
    for c in cands:
        last, pc = q.get(c), prev_close(c, today, bars)
        if not last or not pc:
            continue
        pct = (last / pc - 1) * 100
        if pct < MARKET_MIN:
            continue
        _mkt[(today, mk, c)] = 1
        push.push_event("move_market", f"{M.FLAG[mk]} 🔥 {nmap.get(c) or c} {pct:+.1f}% 급등 (시장 전체)",
                        "오늘 가장 많이 오른 종목 중 하나예요 · 추격은 위험해요 · 탭해서 점수 확인", mk, c)
        n += 1
        if n >= MARKET_CAP:
            break


_upd_done: set = set()
UPDATE_AT = {"KR": ("Asia/Seoul", 7, 30), "US": ("America/New_York", 8, 30)}   # 장 시작 약 1시간 반 전


def auto_update(now_utc=None, run=None) -> str | None:
    """매일 국장·미장 시작 전에 GitHub 새 버전이 있으면 시험 후 적용(없으면 그대로). 사람이 버튼 안 눌러도 됨.
    적용되면 로봇이 다시 켜지고, 실전이었으면 실전으로 이어서 시작(APP_RESUME) → 장 시작 때 새 후보를 다시 불러옴."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    for mk, (tz, h, m) in UPDATE_AT.items():
        t = now_utc.astimezone(ZoneInfo(tz))
        key = (mk, t.date().isoformat())
        if t.weekday() >= 5 or key in _upd_done:
            continue
        mins = t.hour * 60 + t.minute - (h * 60 + m)
        if 0 <= mins < 20:
            _upd_done.add(key)
            if run:
                return run(mk)
            from .telegram_ctl import TelegramControl
            msg = TelegramControl.update_now(None)
            print("자동 업데이트 확인", mk, msg)
            return msg
    return None


_last_nj: dict = {}


def hold_news(mk: str, news_fn=None, claude=None):
    """1시간마다 보유 종목 최신 기사를 서버에서 바로 AI 판정 (.env 에 ANTHROPIC_API_KEY 가 있을 때만).
    새로 '악재'가 되면 알림, '심한 악재'면 더 강하게 알림 (자동으로 팔지는 않음 — 손절선이 지켜 줌)."""
    import os
    from . import app as A
    from . import main as M
    from . import mobile as MB
    from . import news as N
    from . import news_judge as NJ
    from . import push
    if not os.getenv("ANTHROPIC_API_KEY", "").strip() and not claude:
        return
    if time.time() - _last_nj.get(mk, 0) < 3300:
        return
    b = A.STATE.get("bots", {}).get(mk)
    if b is None or not getattr(b, "loop_on", False):
        return
    hold = [p for p in b.broker.pos.values() if p.state == "open"]
    if not hold:
        return
    _last_nj[mk] = time.time()

    def f(code, name):
        h = N.naver_stock_news(code, 8) if mk == "KR" and code.isdigit() else []
        return list(dict.fromkeys(h + N.recent_news(name, mk, days=2, n=6)))[:8]
    prev = MB.NJ_LOCAL.get(mk) or {}
    res = NJ.judge(mk, [{"code": p.symbol, "name": p.name} for p in hold], news_fn or f, prev, claude)
    for c, v in res.items():
        old = prev.get(c) or {}
        if v["impact"] == "악재" and (old.get("impact") != "악재" or (v["severe"] and not old.get("severe"))):
            t = f"{M.FLAG[mk]} {'⛔ 심한 ' if v['severe'] else '⚠️ '}악재 뉴스: {v.get('name') or c}"
            push.push_event("warn", t, f"{v['why']} · 보유 중 · 손절선은 그대로 지켜요", mk, c)
    MB.NJ_LOCAL[mk] = {**prev, **res}


def _loop():
    while True:
        for mk in ("KR", "US"):
            try:
                check(mk)
            except Exception:
                pass
            try:
                hold_news(mk)
            except Exception:
                pass
        try:
            auto_update()
        except Exception as e:
            print("자동 업데이트 오류", e)
        time.sleep(120)


def start():
    threading.Thread(target=_loop, daemon=True).start()
