"""장중 알림 — AI 없이 시세만 계산해서 텔레그램으로 보낸다 (Claude 사용량 0).

감시 대상
  1) 전날 COF 스크리너 결과(docs/data/latest.json)의 단타·스윙 후보
  2) config/watch.json 에 직접 적어 둔 종목 (폰의 GitHub 앱에서 수정 가능)

알림 규칙
  - 장 시작 후 첫 실행: 감시 종목 전체 시초 요약 1건
  - 이후: 등락률이 3% · 5% · 10% · 15% · 20% 구간을 새로 넘을 때만 종목별 알림 (같은 구간 중복 알림 없음)
  - 거래량이 20일 평균의 3배를 넘으면 표시

시세: Yahoo Finance(yfinance). 한국 종목은 약 15~20분 지연될 수 있음.
실행: python screener/alert.py kr|us   (환경 변수 TG_TOKEN, TG_CHAT, 선택 DASH_URL)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
LEVELS = [3, 5, 10, 15, 20]
VOL_X = 3.0

MARKETS = {
    "kr": dict(tz=timezone(timedelta(hours=9)), open=(9, 0), close=(15, 30),
               index={"^KS11": "코스피", "^KQ11": "코스닥"}),
    "us": dict(tz=None, open=(9, 30), close=(16, 0),
               index={"^GSPC": "S&P500", "^IXIC": "나스닥", "^SOX": "반도체"}),
}


def now_local(mk: str) -> datetime:
    if mk == "us":
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    return datetime.now(MARKETS["kr"]["tz"])


def is_open(mk: str, t: datetime) -> bool:
    if t.weekday() >= 5:
        return False
    o, c = MARKETS[mk]["open"], MARKETS[mk]["close"]
    m = t.hour * 60 + t.minute
    return o[0] * 60 + o[1] <= m <= c[0] * 60 + c[1]


def yahoo_symbol(code: str, market: str) -> str:
    if market in ("코스피", "KOSPI"):
        return f"{code}.KS"
    if market in ("코스닥", "KOSDAQ"):
        return f"{code}.KQ"
    return code


def load_targets(mk: str) -> dict[str, dict]:
    """symbol -> {name, ref}"""
    targets: dict[str, dict] = {}
    if mk == "kr":
        latest = ROOT / "docs" / "data" / "latest.json"
        if latest.exists():
            d = json.loads(latest.read_text(encoding="utf-8"))
            for kind, ref in (("danta", "단타"), ("swing", "스윙")):
                for r in d.get(kind, []):
                    targets[yahoo_symbol(r["ticker"], r["market"])] = {"name": r["name"], "ref": ref}
    cfg = ROOT / "config" / "watch.json"
    if cfg.exists():
        w = json.loads(cfg.read_text(encoding="utf-8")).get(mk, [])
        for x in w:
            sym = yahoo_symbol(x["code"], x.get("market", ""))
            targets.setdefault(sym, {"name": x.get("name", x["code"]), "ref": "관심"})
    return targets


def quotes(symbols: list[str]) -> dict[str, dict]:
    """symbol -> {last, prev, pct, vol_x}. 오늘 일봉(장중 부분 봉)과 전일 종가로 계산."""
    out: dict[str, dict] = {}
    if not symbols:
        return out
    df = yf.download(symbols, period="1mo", interval="1d", group_by="ticker",
                     auto_adjust=False, progress=False, threads=True)
    for s in symbols:
        try:
            h = df[s] if len(symbols) > 1 else df
            h = h.dropna(subset=["Close"])
            if len(h) < 2:
                continue
            last, prev = float(h["Close"].iloc[-1]), float(h["Close"].iloc[-2])
            vol = float(h["Volume"].iloc[-1])
            avg = float(h["Volume"].iloc[-21:-1].mean()) if len(h) > 5 else 0
            out[s] = {"last": last, "prev": prev, "pct": (last / prev - 1) * 100,
                      "vol_x": vol / avg if avg else None}
        except Exception as e:
            print("skip", s, e, file=sys.stderr)
    return out


def level_of(p: float) -> int:
    a = abs(p)
    lv = 0
    for L in LEVELS:
        if a >= L:
            lv = L
    return lv if p >= 0 else -lv


def fmt_price(v: float, mk: str) -> str:
    return f"${v:,.2f}" if mk == "us" else f"{v:,.0f}원"


def send(text: str):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not token or not chat:
        print("[텔레그램 미설정] 보낼 메시지:\n" + text)
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"}, timeout=20)
    r.raise_for_status()


def line(name: str, ref: str, q: dict, mk: str) -> str:
    arrow = "🔺" if q["pct"] >= 0 else "🔻"
    vx = f" · 거래량 {q['vol_x']:.1f}배" if q.get("vol_x") and q["vol_x"] >= VOL_X else ""
    return f"{arrow} <b>{name}</b> {q['pct']:+.2f}% {fmt_price(q['last'], mk)} <i>[{ref}]</i>{vx}"


def main():
    mk = (sys.argv[1] if len(sys.argv) > 1 else "kr").lower()
    force = "--force" in sys.argv
    t = now_local(mk)
    if not force and not is_open(mk, t):
        print(f"{mk}: 장 운영 시간 아님 ({t:%H:%M})")
        return
    today = t.strftime("%Y-%m-%d")
    STATE_DIR.mkdir(exist_ok=True)
    sp = STATE_DIR / f"{mk}-{today}.json"
    state = json.loads(sp.read_text()) if sp.exists() else {"opened": False, "levels": {}}

    targets = load_targets(mk)
    idx = MARKETS[mk]["index"]
    q = quotes(list(idx) + list(targets))
    dash = os.environ.get("DASH_URL", "")
    flag = "🇰🇷" if mk == "kr" else "🇺🇸"
    idx_line = " · ".join(f"{n} {q[s]['pct']:+.2f}%" for s, n in idx.items() if s in q)

    msgs = []
    if not state["opened"]:
        movers = sorted(((s, v) for s, v in targets.items() if s in q), key=lambda x: -q[x[0]]["pct"])
        body = "\n".join(line(v["name"], v["ref"], q[s], mk) for s, v in movers[:15])
        msgs.append(f"{flag} <b>장 시작 점검 {t:%H:%M}</b>\n{idx_line}\n\n{body or '감시 종목 시세를 받지 못했습니다'}")
        state["opened"] = True
        for s in targets:
            if s in q:
                state["levels"][s] = level_of(q[s]["pct"])
    else:
        hits = []
        for s, v in targets.items():
            if s not in q:
                continue
            lv, old = level_of(q[s]["pct"]), state["levels"].get(s, 0)
            if abs(lv) > abs(old) or (lv and (lv > 0) != (old > 0)):
                hits.append(line(v["name"], v["ref"], q[s], mk) + f"  ← {abs(lv)}% 돌파")
                state["levels"][s] = lv
        if hits:
            msgs.append(f"{flag} <b>{t:%H:%M} 변동 알림</b>\n{idx_line}\n\n" + "\n".join(hits))

    for m in msgs:
        send(m + (f"\n\n<a href=\"{dash}\">대시보드 열기</a>" if dash else ""))
    sp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    print(f"{mk} {t:%H:%M} 감시 {len(targets)} · 알림 {len(msgs)}")


if __name__ == "__main__":
    main()
