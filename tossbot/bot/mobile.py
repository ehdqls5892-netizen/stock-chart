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
    if path.startswith("/api/m/push/"):
        from . import push as PU
        if path == "/api/m/push/sub":
            return {"msg": PU.subscribe(body.get("sub") or {})}
        if path == "/api/m/push/prefs":
            return {"prefs": PU.set_prefs(body.get("prefs") or {})}
        if path == "/api/m/push/test":
            n = PU.send_push("🔔 토스봇 알림 시험", "이 알림이 보이면 준비 끝이에요. 사고팔 때·급등할 때 이렇게 알려 드려요.")
            return {"msg": f"{n}대 기기로 보냈어요" if n else "보낼 기기가 없거나 실패했어요. '이 휴대폰으로 알림 받기'를 먼저 눌러 주세요."}
    if path == "/api/m/buy":
        return {"msg": buy(mk, sym or str(body.get("code", "")))}
    if path == "/api/m/cmd":
        t = str(body.get("text", ""))[:200]
        if any(k in t for k in ("실전 시작", "전부 팔기", "보유 유지", "재개")):
            return {"msg": "이 명령은 화면의 버튼으로 해 주세요."}
        return {"texts": WC.run(t) if WC else ["쓸 수 없어요"]}
    return {"msg": "모르는 요청"}


# ───────── 데스크 통합: 주식비서 데스크 자료·종목 검색·종목 상세(차트·점수)·봇으로 사기 ─────────
_CACHE: dict = {}
RAW = "https://raw.githubusercontent.com/ehdqls5892-netizen/stock-chart"


def _get_json(url: str, ttl: int = 300, get=None):
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        v = (get or (lambda u: requests.get(u, timeout=15).json()))(url)
    except Exception:
        v = hit[1] if hit else None
    _CACHE[url] = (time.time(), v)
    return v


def desk(mk: str) -> dict:
    """주식비서 데스크 자료(마감 리포트 가격 계획·장중 1순위·실적 일정·차트 분류) — 모든 종목에 이름과 작은 표시(칩)."""
    m = mk.lower()
    base = _cfg().get("desk_feed_url") or (RAW + "/main/docs/data/desk-{m}-{kind}.json")
    rep = _get_json(base.format(m=m, kind="report")) or {}
    live = _get_json(base.format(m=m, kind="live"), 120) or {}
    earn = _get_json(base.format(m=m, kind="earn"), 900) or {}
    ch = _get_json(f"{RAW}/main/docs/data/chart-{m}-latest.json", 900) or {}
    st = ch.get("stocks") or {}
    codes = set((rep.get("plans") or {})) | set((live.get("plans") or {})) | set(live.get("watch") or []) | set(earn.get("upcoming") or {}) \
        | set(earn.get("good") or {}) | set(earn.get("bad") or {}) | {c for v in (ch.get("scan") or {}).values() for c in v[:15]}
    hint = {}
    for src in ((rep.get("info") or {}), (earn.get("events") or {}), (earn.get("analyst") or {})):
        for c, x in src.items():
            if x.get("name"):
                hint.setdefault(c, x["name"])
    for d0 in (rep, live):
        for c, x in (d0.get("plans") or {}).items():
            if x.get("name"):
                hint.setdefault(c, x["name"])
    for c, n in (live.get("names") or {}).items():
        if n:
            hint.setdefault(c, n)
    for c, x in st.items():
        if x.get("name"):
            hint.setdefault(c, x["name"])
    nm_ = name_map(mk, sorted(codes), hint)
    ctx = context(mk, earn, live, st)

    def row(c, **kw):
        return {"code": c, "name": nm_.get(c) or c, **kw, **tags(mk, c, ctx)}

    def plans(d):
        out = []
        for code, p in (d.get("plans") or {}).items():
            out.append(row(code, **{k: p.get(k) for k in ("src", "score", "entryLow", "entryHigh", "stop", "t1", "t2")}))
        out.sort(key=lambda x: (0 if "1순위" in str(x.get("src")) else 1, -(x.get("score") or 0)))
        return out
    scan = {}
    for k, v in (ch.get("scan") or {}).items():
        scan[k] = [row(c, close=(st.get(c) or {}).get("close"), pct=(st.get(c) or {}).get("pct"), label=(st.get(c) or {}).get("label"),
                       signals=(st.get(c) or {}).get("signals", [])[:3], warns=(st.get(c) or {}).get("warns", [])[:3]) for c in v[:15]]
    ev = earn.get("events") or {}
    up = sorted(((c, d) for c, d in (earn.get("upcoming") or {}).items()), key=lambda t: t[1])[:40]
    return {"market": mk.upper(), "report": {"date": rep.get("date"), "plans": plans(rep)},
            "live": {"date": live.get("date"), "updatedAt": live.get("updatedAt"), "plans": plans(live),
                     "watch": [row(c) for c in (live.get("watch") or [])[:20]]},
            "earn": {"date": earn.get("date"),
                     "upcoming": [row(c, date=d, time=(ev.get(c) or {}).get("time", ""), kind=(ev.get(c) or {}).get("kind", "")) for c, d in up],
                     "good": [row(c, text=str((x if isinstance(x, dict) else {}).get("text", ""))[:60]) for c, x in list((earn.get("good") or {}).items())[:15]],
                     "bad": [row(c, text=str((x if isinstance(x, dict) else {}).get("text", ""))[:60]) for c, x in list((earn.get("bad") or {}).items())[:15]]},
            "scan": scan, "scanAsOf": ch.get("asOf")}


_NC: dict = {}


def name_map(mk: str, codes: list, hint: dict | None = None) -> dict:
    """코드 → 종목 이름: 데스크 자료 → 이름 목록 → (그래도 없으면) 토스 종목 정보. 토스로 찾은 이름은 저장해 둠."""
    from . import main as M
    out = dict(hint or {})
    nl = names(mk)
    p = DIR.parent / "logs" / "names_cache.json"
    if not _NC:
        try:
            _NC.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    for c in codes:
        if not out.get(c):
            out[c] = nl.get(c) or _NC.get(f"{mk}:{c}") or ""
    miss = [c for c in codes if not out.get(c)][:50]
    if miss:
        try:
            M.load_env()
            for x in M.shared_api().stocks(miss) or []:
                sym, nm = x.get("symbol"), x.get("name") or x.get("stockName") or x.get("koreanName")
                if sym and nm:
                    out[sym] = nm
                    _NC[f"{mk}:{sym}"] = nm
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(_NC, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return {c: (out.get(c) or c) for c in codes} | {c: v for c, v in out.items() if v}


def context(mk: str, earn: dict | None = None, live: dict | None = None, chart: dict | None = None) -> dict:
    """종목별 작은 표시를 만들 재료 (한 번 모아 두고 여러 종목에 씀)."""
    if earn is None:
        m = mk.lower()
        base = _cfg().get("desk_feed_url") or (RAW + "/main/docs/data/desk-{m}-{kind}.json")
        earn = _get_json(base.format(m=m, kind="earn"), 900) or {}
        live = _get_json(base.format(m=m, kind="live"), 120) or {}
        chart = (_get_json(f"{RAW}/main/docs/data/chart-{m}-latest.json", 900) or {}).get("stocks") or {}
    pk = {}
    try:
        for x in (_get_json(_cfg()["daily_swing"]["picks_url"].format(m=mk.lower())) or {}).get("picks", []):
            pk.setdefault(x.get("code"), x)
    except Exception:
        pass
    return {"ev": earn.get("events") or {}, "an": earn.get("analyst") or {}, "good": earn.get("good") or {}, "bad": earn.get("bad") or {},
            "up": earn.get("upcoming") or {}, "imp": (live or {}).get("impact") or {}, "pk": pk, "ch": chart or {}}


def tags(mk: str, code: str, ctx: dict) -> dict:
    """종목 옆 작은 표시: 호재/악재/엇갈림 · 목표가 여력 · 점수 등급 · 실적일 · 차트 관심/주의 (최대 4개)"""
    chips = []
    pk = ctx["pk"].get(code) or {}
    ai = ((pk.get("ev") or {}).get("ai")) or {}
    if code in ctx["good"]:
        chips.append({"t": "실적 좋음", "c": "pos"})
    elif code in ctx["bad"]:
        chips.append({"t": "실적 나쁨", "c": "neg"})
    elif (ctx["imp"].get(code) or {}).get("impact") in ("호재", "악재", "중립"):
        im = ctx["imp"][code]["impact"]
        chips.append({"t": im if im != "중립" else "엇갈림", "c": {"호재": "pos", "악재": "neg"}.get(im, "mid")})
    elif ai.get("view") in ("긍정", "부정", "중립"):
        chips.append({"t": {"긍정": "호재", "부정": "악재", "중립": "엇갈림"}[ai["view"]], "c": {"긍정": "pos", "부정": "neg"}.get(ai["view"], "mid")})
    if ai.get("veto"):
        chips.append({"t": "⛔ 매수 금지", "c": "neg"})
    b = (ctx["ev"].get(code) or {}).get("bias", "")
    if b and code in ctx["up"]:
        chips.append({"t": "실적 " + ("기대" if "기대" in b else "우려" if "우려" in b else "엇갈림"),
                      "c": "pos" if "기대" in b else "neg" if "우려" in b else "mid"})
    if pk.get("score") is not None:
        sc = pk["score"]
        chips.append({"t": f"{'A' if sc >= 4 else 'B' if sc >= 2 else 'C'} {sc:+d}", "c": "A" if sc >= 4 else "B" if sc >= 2 else "C"})
    up = None
    an = ctx["an"].get(code) or {}
    if an.get("up") is not None:
        up = float(an["up"]) / 100
    else:
        e = est(mk, code)
        tg = SWnum(e.get("targetMeanPrice") if mk == "US" else e.get("cs_priceTargetMean"))
        px = SWnum((ctx["ch"].get(code) or {}).get("close")) or SWnum(e.get("closePrice")) or SWnum(e.get("currentPrice")) or SWnum(pk.get("close"))
        if tg and px:
            up = tg / px - 1
        elif (pk.get("ev") or {}).get("tgt_up") is not None:
            up = pk["ev"]["tgt_up"]
    if up is not None and -0.6 <= up <= 3.0:                 # 자료가 어긋나 보이는 값(예: 단위 차이)은 표시 안 함
        chips.append({"t": f"목표가 {up * 100:+.0f}%", "c": "pos" if up >= 0.15 else "neg" if up < 0 else "mid"})
    lb = (ctx["ch"].get(code) or {}).get("label")
    if lb in ("관심", "주의") and len(chips) < 4:
        chips.append({"t": f"차트 {lb}", "c": "pos" if lb == "관심" else "neg"})
    d = ctx["up"].get(code)
    if d and len(chips) < 4:
        chips.append({"t": f"실적 {d[5:].replace('-', '/')}", "c": "C"})
    return {"chips": chips[:4]}


def SWnum(v):
    from .swing import _num
    return _num(v)


def names(mk: str) -> dict:
    """종목 이름 목록 (밤마다 후보 계산 때 저장: 국장 약 700·미장 약 500) + 데스크 관심 종목."""
    m = mk.lower()
    d = dict(_get_json(f"{RAW}/swingpicks-{m}/names.json", 6 * 3600) or {})
    for x in (_get_json(f"{RAW}/main/config/universe_{m}.json", 6 * 3600) or []):
        if isinstance(x, dict) and x.get("code"):
            d.setdefault(str(x["code"]), str(x.get("name") or x["code"]))
    return d


def search(q: str) -> list:
    q = (q or "").strip()
    if not q:
        return []
    ql = q.lower().replace(" ", "")
    out = []
    for mk in ("KR", "US"):
        for code, nm in names(mk).items():
            n2 = str(nm).lower().replace(" ", "")
            if ql == code.lower() or ql in n2 or (mk == "US" and code.lower().startswith(ql)):
                rank = 0 if ql in (code.lower(), n2) else 1 if n2.startswith(ql) or code.lower().startswith(ql) else 2
                out.append((rank, len(n2), {"market": mk, "code": code, "name": nm}))
    out.sort(key=lambda t: (t[0], t[1]))
    res = [t[2] for t in out[:25]]
    if not res and (q.isdigit() and len(q) == 6):
        res = [{"market": "KR", "code": q, "name": name_map("KR", [q]).get(q, q)}]
    elif not res and q.replace(".", "").isalpha() and len(q) <= 6:
        res = [{"market": "US", "code": q.upper(), "name": q.upper()}]
    return res


_BARS: dict = {}


def bars(sym: str, n: int = 260) -> list:
    """토스 일봉 [(날짜, 시가, 고가, 저가, 종가, 거래량)] 오래된 것부터. 1분 재사용."""
    hit = _BARS.get(sym)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    from . import main as M
    from .util import f
    out = []
    try:
        M.load_env()
        for c in M.shared_api().candles(sym, "1d", n) or []:
            cl = f(c.get("closePrice"))
            if cl:
                out.append((str(c.get("timestamp") or "")[:10], f(c.get("openPrice")) or cl, f(c.get("highPrice")) or cl,
                            f(c.get("lowPrice")) or cl, cl, f(c.get("volume"), 0.0) or 0.0))
        out.sort(key=lambda b: b[0])
    except Exception:
        out = []
    _BARS[sym] = (time.time(), out)
    return out


_EST: dict = {}


def est(mk: str, code: str) -> dict:
    from . import swing as SW
    from . import main as M
    c = _cfg().get("daily_swing", {})
    today = M.now(mk).date()
    hit = _EST.get(mk)
    if not hit or hit[0] != today:
        try:
            rows = SW.load_estimates(c.get("estimates_url", ""), mk, today, lambda *a: None) if c.get("estimates_url") else {}
        except Exception:
            rows = {}
        _EST[mk] = hit = (today, rows or {})
    return hit[1].get(code) or {}


def stock(mk: str, code: str, get_bars=None) -> dict:
    """종목 상세: 일봉 차트·현재·미래 점수와 근거·AI 판독(후보면)·데스크 계획·차트 신호·보유 정보·최근 기사."""
    from . import swing as SW
    from . import main as M
    from . import app as A
    mk = mk.upper()
    b = (get_bars or bars)(code)
    pk = next((x for x in (_get_json(_cfg()["daily_swing"]["picks_url"].format(m=mk.lower())) or {}).get("picks", [])
               if x.get("code") == code), None) or {}
    ev = dict(pk.get("ev") or {})
    e = est(mk, code)
    if mk == "US" and not e.get("targetMeanPrice") and ev.get("tgt_up") is not None and pk.get("close"):
        e = {**e, "targetMeanPrice": pk["close"] * (1 + ev["tgt_up"]), "recommendationMean": ev.get("rec")}
    nm = pk.get("name") or name_map(mk, [code]).get(code) or code
    news = []
    try:
        from .news import NewsWatch, naver_stock_news, recent_news
        news = (naver_stock_news(code, 4) if mk == "KR" else []) + recent_news(nm, mk, days=3, n=4)
        news = list(dict.fromkeys(news))[:6]
        nw = NewsWatch.__new__(NewsWatch)
        nw.m = mk
        tone = sum(NewsWatch.tone(nw, t) for t in news)
    except Exception:
        tone = 0
    price = A.quotes([code]).get(code) or (b[-1][4] if b else None)
    fs, why = (SW.forward_score(mk, b, price, e, ev, tone) if b else (None, []))
    today = M.now(mk).date().isoformat()
    done = [x for x in b if x[0] < today] if b else []
    prev = done[-1][4] if done else (b[-1][4] if b else None)
    atr = None
    if len(b) >= 15:
        trs = [max(b[i][2] - b[i][3], abs(b[i][2] - b[i - 1][4]), abs(b[i][3] - b[i - 1][4])) for i in range(len(b) - 14, len(b))]
        atr = sum(trs) / len(trs)
    stop = round(prev - 2 * atr, 4) if prev and atr else None
    dsk = desk(mk)
    plan = next((p for p in dsk["report"]["plans"] + dsk["live"]["plans"] if p["code"] == code), None)
    ch = ((_get_json(f"{RAW}/main/docs/data/chart-{mk.lower()}-latest.json", 900) or {}).get("stocks") or {}).get(code) or {}
    hold = next((h for h in A.holdings() if h["market"] == mk and h["symbol"] == code), None) if A.STATE.get("bots") else None
    return {"market": mk, "code": code, "name": nm, "price": price, "prev": prev,
            "pct": (price / prev - 1) * 100 if price and prev else None,
            "bars": [list(x) for x in b[-160:]], "score": fs, "grade": SW.grade_of(fs) if fs is not None else None, "why": why,
            "min_score": _cfg()["daily_swing"].get("min_forward_score", 3),
            "target": SW.cons_target(mk, e) or (pk["close"] * (1 + ev["tgt_up"]) if ev.get("tgt_up") is not None and pk.get("close") else None),
            "ai": ev.get("ai"), "dart": ev.get("dart_flags"), "sector": ev.get("sector"), "earn": ev.get("earn"),
            "atr": atr, "stop": stop, "is_pick": bool(pk), "plan": plan,
            "chart": {k: ch.get(k) for k in ("label", "signals", "warns", "support", "resistance")} if ch else None,
            "hold": hold, "news": news,
            "_x": {"code": code, "name": nm, "setup": "fs", "close": prev, "stop": stop, "atr": atr, "score": fs,
                   "reason": f"휴대폰 앱에서 직접 고름 · 현재·미래 점수 {fs:+d}: " + " ".join(why[:8]) if fs is not None else "휴대폰 앱에서 직접 고름",
                   "ev": ev}}


def buy(mk: str, code: str) -> str:
    """'봇으로 사기': 장중에만. 실제 매수는 로봇 루프가 스윙 규칙 그대로 점검한 뒤 함."""
    from . import app as A
    mk = mk.upper()
    b = A.STATE.get("bots", {}).get(mk)
    if b is None or not getattr(b, "loop_on", False) or "장중 매매" not in str(getattr(b, "phase", "")):
        return "지금은 이 시장의 매수 시간이 아니에요 (장중에만 봇이 살 수 있어요)."
    d = stock(mk, code)
    x = d["_x"]
    if not x.get("stop") or not x.get("close"):
        return "차트 자료가 부족해서 손절선을 정할 수 없어요."
    if any(r.get("code") == code for r in b.buy_reqs):
        return "이미 요청했어요. 잠시 뒤 결과 알림이 와요."
    b.buy_reqs.append(x)
    from .main import money
    return (f"🤖 {x['name']} 매수 요청을 봇에게 넘겼어요. 봇이 손실 한도·손절선({money(mk, x['stop'])})·업종·점수(지금 {x['score']:+d})·"
            f"AI·공시·뉴스 점검을 똑같이 하고, 통과하면 사고 아니면 이유를 알려 드려요 (몇 초~1분)." if x.get("score") is not None
            else f"🤖 {x['name']} 매수 요청을 봇에게 넘겼어요.")
