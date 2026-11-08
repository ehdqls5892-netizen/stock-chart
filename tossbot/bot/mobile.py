"""휴대폰 앱 화면(홈 화면에 추가해서 쓰는 웹앱)용 API.

텔레그램 버튼과 같은 일을 하되, 결과를 화면에 그려 주도록 JSON 으로 돌려준다.
접속은 서버의 Tailscale(내 기기끼리만 연결되는 사설망)로만 — 인터넷에 그냥 열지 않음.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from .telegram_ctl import TelegramControl

DIR = Path(__file__).resolve().parent
_PICKS: dict = {}
WC = None          # 텔레그램 명령을 화면용으로 받아 두는 객체 (app.main 에서 만듦)


class Capture(TelegramControl):
    """텔레그램으로 보내는 대신 글을 모아 두는 버전 (같은 명령 처리 코드를 그대로 씀)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.out: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    def send(self, text, kb=True):
        self.out.append(str(text))

    def _api(self, method, body):
        if body.get("text"):
            self.out.append(str(body["text"]))
        return {}

    def run(self, text: str) -> list:
        self.out = []
        self.handle(text)
        return [t for t in self.out if not t.startswith("📰 보유 종목 지금 상황을 모으는 중")]


def make(tc: TelegramControl) -> Capture:
    """텔레그램 조작 객체와 같은 기능을 연결한 화면용 객체."""
    global WC
    w = Capture(tc.start_fn, tc.stop_fn, tc.status_fn, tc.capital_fn)
    for k in ("resume_fn", "lock_fn", "holdings_fn", "set_lock_fn", "sell_one_fn", "lock_all_fn", "etf_fn"):
        if hasattr(tc, k):
            setattr(w, k, getattr(tc, k))
    WC = w
    return w


def _cfg() -> dict:
    return json.loads((DIR.parent / "config.json").read_text(encoding="utf-8"))


def picks(mk: str, get=None) -> dict:
    """밤에 계산된 스윙 후보(현재·미래 점수·AI 판독 포함). 5분 동안 재사용."""
    mk = mk.upper()
    hit = _PICKS.get(mk)
    if hit and time.time() - hit[0] < 300 and get is None:
        return hit[1]
    c = _cfg().get("daily_swing", {})
    get = get or (lambda u: requests.get(u, timeout=15).json())
    try:
        d = get(c["picks_url"].format(m=mk.lower()))
    except Exception as e:
        return {"market": mk, "error": f"후보 자료를 못 받았어요: {type(e).__name__}", "picks": []}
    on = set((c.get("markets") or {}).get(mk, []))
    out = []
    for x in d.get("picks", []):
        if on and x.get("setup") not in on:
            continue
        ev = x.get("ev") or {}
        ai = ev.get("ai") or {}
        sc = x.get("score")
        out.append({"code": x.get("code"), "name": x.get("name"), "close": x.get("close"), "stop": x.get("stop"),
                    "score": sc, "grade": ("A" if sc >= 4 else "B" if sc >= 2 else "C") if isinstance(sc, (int, float)) else None,
                    "reason": x.get("reason", ""), "sector": ev.get("sector"),
                    "ai": {k: ai.get(k) for k in ("view", "adj", "catalyst", "risk", "veto", "final", "model")} if ai else None,
                    "dart": ev.get("dart_flags"), "earn": ev.get("earn"), "tgt_up": ev.get("tgt_up"),
                    "news": (ev.get("news") or [])[:5]})
    res = {"market": mk, "date": d.get("date"), "regime": d.get("regime"), "index": d.get("index"), "picks": out[:20]}
    _PICKS[mk] = (time.time(), res)
    return res


def table(mk: str) -> str:
    """오늘 매수 판단표 (로봇이 켜져 있고 후보를 받아 둔 경우)."""
    from . import app as A
    b = A.STATE.get("bots", {}).get(mk.upper())
    if b is None or not getattr(b, "sw_picks", None):
        return "로봇이 아직 이 시장 후보를 받지 않았어요 (장 시작 전 미리 보기 때 받아요). 아래 '후보' 목록을 참고하세요."
    try:
        return b.swing_table_text()
    except Exception as e:
        return f"판단표를 만들지 못했어요: {e}"


def home() -> dict:
    from . import app as A
    s = A.status()
    try:
        hs = A.holdings()
    except Exception as e:
        hs = []
        s["holdings_error"] = str(e)
    s["holdings"] = hs
    s["capitals"] = A.capitals()
    s["log"] = s.get("log", [])[-120:]
    return s


def post(path: str, body: dict) -> dict:
    """화면 버튼 처리. 위험한 것(실전 시작·팔기·끄기)은 화면에서 한 번 더 확인한 뒤에만 이 함수로 옴."""
    from . import app as A
    mk, sym = str(body.get("market", "")).upper(), str(body.get("symbol", ""))
    if path == "/api/m/lock":
        return {"msg": A.set_lock(mk, sym, bool(body.get("on")))}
    if path == "/api/m/lockall":
        return {"msg": A.lock_all()}
    if path == "/api/m/sell":
        return {"msg": A.sell_one(mk, sym)}
    if path == "/api/m/stop":
        return {"msg": A.stop("sell" if body.get("how") == "sell" else "keep")}
    if path == "/api/m/start":
        return {"msg": A.start("live" if body.get("mode") == "live" else "paper", True)}
    if path == "/api/m/capital":
        return {"msg": A.set_capital(mk, body.get("amount"))}
    if path == "/api/m/resume":
        return {"msg": A.resume_dd()}
    if path == "/api/m/update":
        return {"msg": WC.update_now() if WC else "쓸 수 없어요"}
    if path == "/api/m/cmd":
        t = str(body.get("text", ""))[:200]
        if any(k in t for k in ("실전 시작", "전부 팔기", "보유 유지", "재개")):
            return {"msg": "이 명령은 화면의 버튼으로 해 주세요."}
        return {"texts": WC.run(t) if WC else ["쓸 수 없어요"]}
    return {"msg": "모르는 요청"}
