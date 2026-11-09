"""휴대폰 앱 화면(홈 화면에 추가해서 쓰는 웹앱)용 API.

텔레그램 버튼과 같은 일을 하되, 결과를 화면에 그려 주도록 JSON 으로 돌려준다.
접속은 서버의 Tailscale(내 기기끼리만 연결되는 사설망)로만 — 인터넷에 그냥 열지 않음.
"""
from __future__ import annotations

import json
import re
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
    try:
        from . import swing as SW
        from .main import now as _now
        today = _now(mk).date()
    except Exception:
        SW, today = None, None
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
                    "news": (ev.get("news") or [])[:5], "rr": x.get("rr"), "no_buy": x.get("no_buy"),
                    "pros": [t.strip() for t in re.findall(r"＋([^＋－·＊]+)", x.get("reason", ""))][:3],
                    "cons": [t.strip() for t in re.findall(r"－([^＋－·＊]+)", x.get("reason", ""))][:3]})
        try:                         # 봇과 같은 실적 점검: 보유 기간 안에 실적이 있으면 뒤로 미루고 수량 줄임/안 삼
            if SW and x.get("close") and x.get("stop"):
                ep, per2, etxt = SW.earn_plan(x, today, float(x["close"]), float(x["stop"]), c.get("earnings") or {}, mk)
                out[-1].update({"eplan": ep, "eper": per2, "etxt": etxt})
        except Exception:
            pass
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


def nobuy_state() -> dict:
    from .main import load_settings, now
    nb = load_settings().get("nobuy") or {}
    return {mk: nb.get(mk) == now(mk).date().isoformat() for mk in ("KR", "US")}


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
    try:
        s["nobuy"] = nobuy_state()
    except Exception:
        s["nobuy"] = {}
    return s


def post(path: str, body: dict) -> dict:
    """화면 버튼 처리. 위험한 것(실전 시작·팔기·끄기)은 화면에서 한 번 더 확인한 뒤에만 이 함수로 옴."""
    from . import app as A
    mk, sym = str(body.get("market", "")).upper(), str(body.get("symbol", ""))
    if path == "/api/m/lock":
        return {"msg": A.set_lock(mk, sym, bool(body.get("on")))}
    if path == "/api/m/lockall":
        return {"msg": A.lock_all()}
    if path == "/api/m/nobuy":            # 오늘 하루 새로 사지 않기 (보유 종목 관리는 그대로)
        from .main import load_settings, save_settings, now
        if mk not in ("KR", "US"):
            return {"error": "시장을 골라 주세요"}
        st = load_settings()
        nb = st.setdefault("nobuy", {})
        today = now(mk).date().isoformat()
        on = bool(body.get("on"))
        if on:
            nb[mk] = today
        else:
            nb.pop(mk, None)
        save_settings(st)
        nm = {"KR": "국장", "US": "미장"}[mk]
        return {"msg": f"🛑 {nm} 오늘({today})은 새로 안 사요 · 보유 종목 손절·관리는 그대로" if on else f"✅ {nm} 오늘 다시 평소처럼 사요"}
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
NJ_LOCAL: dict = {}          # 서버가 1시간마다 직접 AI 로 본 보유 종목 뉴스 판정 {mk: {code: {...}}}
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


STARTED = time.time()


def version(get=None) -> dict:
    """설정 화면 '업데이트' 칸: 지금 쓰는 버전·적용 시각·로봇 다시 켜진 시각·GitHub 최신 코드 시각 → 최신인지."""
    import datetime as dt
    root = DIR.parent
    out = {"started": dt.datetime.fromtimestamp(STARTED, dt.timezone.utc).isoformat(timespec="seconds")}
    f = root / ".last_sha"
    if f.exists():
        out["sha"] = f.read_text().strip()[:7]
        out["applied"] = dt.datetime.fromtimestamp(f.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds")
    bad = root / ".last_fail"
    if bad.exists():
        out["fail"] = dt.datetime.fromtimestamp(bad.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds")
    js = _get_json("https://api.github.com/repos/ehdqls5892-netizen/stock-chart/commits?path=tossbot&per_page=1", 300, get)
    try:
        c = js[0]
        out["latest_sha"] = c["sha"][:7]
        out["latest"] = c["commit"]["committer"]["date"].replace("Z", "+00:00")
        out["latest_msg"] = c["commit"]["message"].split("\n")[0][:80]
    except Exception:
        pass
    if out.get("latest"):
        mine = out.get("applied") or out["started"]
        out["up_to_date"] = dt.datetime.fromisoformat(mine) >= dt.datetime.fromisoformat(out["latest"])
    return out


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
            if x.get("setup") == "fs":                       # 현재·미래 점수 방식 후보만 (옛 방식 점수는 단위가 달라 안 씀)
                pk.setdefault(x.get("code"), x)
    except Exception:
        pass
    sc = _get_json(f"{RAW}/swingpicks-{mk.lower()}/scores.json", 3 * 3600) or {}
    nj = {}
    try:
        from . import news_judge as NJ
        ds = _cfg()["daily_swing"]
        if ds.get("news_judge_url"):
            nj = NJ.fresh(_get_json(ds["news_judge_url"].format(m=mk.lower()), 600), ds.get("news_judge_hours", 6))
        nj.update(NJ.fresh(NJ_LOCAL.get(mk), ds.get("news_judge_hours", 6)))   # 서버가 직접 본 보유 종목 판정
    except Exception:
        pass
    return {"sc": sc, "nj": nj, "ev": earn.get("events") or {}, "an": earn.get("analyst") or {}, "good": earn.get("good") or {}, "bad": earn.get("bad") or {},
            "up": earn.get("upcoming") or {}, "imp": (live or {}).get("impact") or {}, "pk": pk, "ch": chart or {}}


def tags(mk: str, code: str, ctx: dict) -> dict:
    """종목 한 줄 판단 (중학생도 바로 알게): 호재(초록)·엇갈림(노랑)·악재(빨강) + 목표가 여력 + 짧은 이유 2~3개.
    재료: 실적 결과·이슈 기사·AI 판독·실적 분위기·현재·미래 점수·애널리스트 목표가·차트 신호."""
    good, bad = [], []
    pk = ctx["pk"].get(code) or {}
    ai = ((pk.get("ev") or {}).get("ai")) or {}
    if code in ctx["good"]:
        good.append("실적이 예상보다 좋았어요")
    if code in ctx["bad"]:
        bad.append("실적이 예상보다 나빴어요")
    im = (ctx["imp"].get(code) or {})
    if im.get("impact") == "호재":
        good.append("좋은 뉴스: " + im.get("title", "")[:18])
    elif im.get("impact") == "악재":
        bad.append("나쁜 뉴스: " + im.get("title", "")[:18])
    nj = (ctx.get("nj") or {}).get(code) or {}
    if nj.get("impact") == "호재":
        good.insert(0, "새 뉴스: " + (nj.get("why") or "")[:22])
    elif nj.get("impact") == "악재":
        bad.insert(0, ("⛔ 심한 악재: " if nj.get("severe") else "새 악재: ") + (nj.get("why") or "")[:22])
    if ai.get("view") == "긍정":
        good.append("AI가 좋게 봐요")
    elif ai.get("view") == "부정":
        bad.append("AI가 나쁘게 봐요")
    if ai.get("veto"):
        bad.append("AI 매수 금지")
    bias_e = (ctx["ev"].get(code) or {}).get("bias", "")
    if code in ctx["up"] and "기대" in bias_e:
        good.append("실적 발표 기대가 커요")
    elif code in ctx["up"] and "우려" in bias_e:
        bad.append("실적 발표 걱정이 커요")
    sc = pk.get("score")
    if sc is not None:
        sc = int(round(sc))
        (good if sc >= 4 else bad if sc <= 1 else []).append(f"봇 점수 {sc:+d}")
    stt = dict((ctx.get("sc") or {}).get(code) or {})
    lv = LIVE.get((mk, code))
    if lv and time.time() - lv["t"] < 3600:              # 서버가 20분마다 토스 일봉으로 다시 본 오늘 상태가 우선
        stt.update({k: lv[k] for k in ("c", "ld", "dd", "d20", "close") if lv.get(k) is not None})
    ld = bool(stt.get("ld"))
    if ld:
        bad.append(f"1년 고점보다 {stt['dd'] * 100:.0f}%, 오래 내리는 중")
    elif stt.get("c") is not None:
        if stt["c"] >= 3:
            good.append("요즘 차트 흐름이 좋아요")
        elif stt["c"] <= -2:
            bad.append("요즘 차트 흐름이 나빠요")
    if (stt.get("d20") or 0) >= 0.15:
        bad.append("짧은 기간에 너무 많이 올랐어요")
    up = None
    an = ctx["an"].get(code) or {}
    if an.get("up") is not None:
        up = float(an["up"]) / 100
    elif stt.get("tgt") and stt.get("close"):
        up = float(stt["tgt"]) / float(stt["close"]) - 1
    else:
        e = est(mk, code)
        tg = SWnum(e.get("targetMeanPrice") if mk == "US" else e.get("cs_priceTargetMean"))
        px = SWnum(stt.get("close")) or SWnum((ctx["ch"].get(code) or {}).get("close")) or SWnum(e.get("closePrice")) or SWnum(e.get("currentPrice")) or SWnum(pk.get("close"))
        if tg and px:
            up = tg / px - 1
        elif (pk.get("ev") or {}).get("tgt_up") is not None:
            up = pk["ev"]["tgt_up"]
    if up is not None and not (-0.6 <= up <= 3.0):
        up = None                                            # 자료가 어긋나 보이는 값은 안 씀
    note = None
    if up is not None:
        if ld and up >= 0.15:
            note = "목표가는 높지만 주가가 많이 빠진 뒤라 믿기 어려워요"
        elif up > 1.0:
            note = "목표가가 너무 멀어요(오래된 값일 수 있어요)"
        elif up >= 0.15:
            good.append("목표가까지 많이 남았어요")
        elif up < 0:
            bad.append("이미 목표가보다 비싸요")
        elif up < 0.08:
            bad.append("목표가까지 남은 게 적어요")
    rec = stt.get("rec")
    if rec is not None and not pk:
        if (mk == "US" and rec <= 2.0) or (mk == "KR" and rec >= 4.0):
            good.append("증권사 의견 '매수'가 많아요")
        elif (mk == "US" and rec >= 3.0) or (mk == "KR" and rec <= 3.0):
            bad.append("증권사 의견이 '중립' 이하예요")
    lb = (ctx["ch"].get(code) or {}).get("label")
    if lb == "관심" and not stt:
        good.append("차트 모양이 좋아요")
    elif lb == "주의" and not stt:
        bad.append("차트 모양이 나빠요")
    n = len(good) - len(bad)
    if ai.get("veto") or code in ctx["bad"] or nj.get("severe"):
        n = min(n, -1)
    if not good and not bad and (stt.get("c") is not None or up is not None):
        bad, good = [], []
        note = (note + " · " if note else "") + "뚜렷한 호재·악재 없이 보통이에요"
        bias = "엇갈림"
    else:
        bias = "정보 적음" if not good and not bad else "호재" if n > 0 else "악재" if n < 0 else "엇갈림"
    why = (good + bad) if n >= 0 else (bad + good)
    if note:
        why.append(note)
    d = ctx["up"].get(code)
    return {"bias": bias, "tgt": f"{up * 100:+.0f}%" if up is not None else None, "why": why[:4],
            "earn": d[5:].replace("-", "/") if d else None}


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
        from .util import parse_ts
        mk = "KR" if sym[:1].isdigit() or sym[:1] == "A" and sym[1:2].isdigit() else "US"
        for c in M.shared_api().candles_upto(sym, "1d", n) or []:
            cl = f(c.get("closePrice"))
            t = parse_ts(c.get("timestamp"), mk)
            if cl and t:
                out.append((t.date().isoformat(), f(c.get("openPrice")) or cl, f(c.get("highPrice")) or cl,
                            f(c.get("lowPrice")) or cl, cl, f(c.get("volume"), 0.0) or 0.0))
        out.sort(key=lambda b: b[0])
        dd = {}
        for x in out:                       # 같은 날짜가 두 번 오면 마지막 것만
            dd[x[0]] = x
        out = list(dd.values())
    except Exception as e:
        print("일봉 받기 실패", sym, type(e).__name__, e)
        out = []
    _BARS[sym] = (time.time(), out) if out else (time.time() - 50, out)
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
    tg0 = SW.cons_target(mk, e) or (pk["close"] * (1 + ev["tgt_up"]) if ev.get("tgt_up") is not None and pk.get("close") else None)
    rr = None
    if fs is not None and tg0 and price and stop and not SW.long_downtrend(b, price):
        adj, rtxt, rr = SW.rr_score(tg0 / price - 1, (price - stop) / price)
        if rtxt:
            fs += adj
            why = [rtxt] + why
    dsk = desk(mk)
    plan = next((p for p in dsk["report"]["plans"] + dsk["live"]["plans"] if p["code"] == code), None)
    ch = ((_get_json(f"{RAW}/main/docs/data/chart-{mk.lower()}-latest.json", 900) or {}).get("stocks") or {}).get(code) or {}
    hold = next((h for h in A.holdings() if h["market"] == mk and h["symbol"] == code), None) if A.STATE.get("bots") else None
    return {"market": mk, "code": code, "name": nm, "price": price, "prev": prev,
            "pct": (price / prev - 1) * 100 if price and prev else None,
            "bars": [list(x) for x in b[-160:]], "rr": round(rr, 2) if rr else None, "score": fs, "grade": SW.grade_of(fs) if fs is not None else None, "why": why,
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


# ───────── 오늘 살 종목 (로봇이 꺼져 있어도 보이는 예상표) ─────────
def next_buy_time(mk: str, now=None):
    """다음 스윙 매수 시각(장 마감 15분 전 = config session.flatten), 주말 건너뜀 → ISO(UTC)."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Seoul" if mk == "KR" else "America/New_York")
    hm = _cfg()["session"][mk]["flatten"]
    n = (now or dt.datetime.now(tz)).astimezone(tz)
    t = n.replace(hour=int(hm[:2]), minute=int(hm[3:]), second=0, microsecond=0)
    if n >= t:
        t += dt.timedelta(days=1)
    while t.weekday() >= 5:
        t += dt.timedelta(days=1)
    return t.astimezone(dt.timezone.utc).isoformat()


def live_or_night(mk: str) -> dict:
    """장중 다시 고른 후보(오늘 것, 5시간 안)가 있으면 그걸, 없으면 밤 후보."""
    import datetime as dt
    from . import main as M
    url = _cfg()["daily_swing"].get("live_picks_url")
    if url:
        d = _get_json(url.format(m=mk.lower()), 300) or {}
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(d.get("made", ""))).total_seconds() / 3600
        except ValueError:
            age = 99
        if d.get("picks") and d.get("date") == M.now(mk).date().isoformat() and age <= 5:
            out = picks(mk, get=lambda u: d)
            _PICKS.pop(mk.upper(), None)
            out["live"] = True
            out["index_today"] = (d.get("index") or {}).get("today_pct")
            return out
    return picks(mk)


def plan(mk: str, hold=None, cap=None, pk=None, now=None) -> dict:
    """오늘(다음 매수 시각에) 살 종목 예상: 봇과 같은 규칙(점수·AI 부정/중립·손익비·업종 2종목·빈자리·손실 한도)으로 미리 계산.
    최종 결정은 그 시각에 그날 봉·뉴스·현재가로 한 번 더 점검해서 바뀔 수 있음."""
    from . import app as A
    from .main import money
    mk = mk.upper()
    cfg = _cfg()
    c = cfg["daily_swing"]
    if hold is None:
        try:
            hold = [h for h in A.holdings() if h["market"] == mk]
        except Exception:
            hold = []
    cap = cap if cap is not None else (A.capitals().get(mk) or 0)
    d = pk if pk is not None else live_or_night(mk)
    held_codes = {h["symbol"] for h in hold}
    slots = max(0, c.get("max_pos", 6) - sum(1 for h in hold if h.get("swing")))
    smax = (c.get("earnings") or {}).get("sector_max") or 99
    sec_n: dict = {}
    rg = c.get("risk_by_grade") or {}
    buy, wait, skip = [], [], []
    # 봇과 같은 순서: 보유 기간 안에 실적 발표가 없는 종목 먼저 (점수 순서는 그 안에서 유지)
    for x in sorted(d.get("picks", []), key=lambda x: x.get("eplan", "ok") not in ("ok", "early")):
        ai = x.get("ai") or {}
        why = None
        sc = x.get("score") or 0
        if x["code"] in held_codes:
            why = "이미 갖고 있어요"
        elif ai.get("veto"):
            why = "AI 매수 금지: " + str(ai["veto"])[:30]
        elif ai.get("view") == "부정":
            why = "AI 판독이 부정적"
        elif x.get("no_buy"):
            why = x["no_buy"]
        elif c.get("min_rr") and (x.get("rr") or {}).get("rr") is not None and x["rr"]["rr"] < c["min_rr"]:
            why = f"손익비 {x['rr']['rr']:.1f} (기준 {c['min_rr']:g} 미만)"
        elif c.get("max_ext20") and any("단기 과열" in t for t in (x.get("cons") or [])):
            why = "20일선보다 너무 높이 떠 있어 단기 과열"
        elif c.get("max_stop_pct") and x.get("close") and x.get("stop") and (x["close"] - x["stop"]) / x["close"] > c["max_stop_pct"]:
            why = f"손절 폭 {(x['close'] - x['stop']) / x['close'] * 100:.0f}% — 너무 넓음"
        elif sc < c.get("min_forward_score", 3):
            why = f"점수 {sc:+.0f} (기준 +{c.get('min_forward_score', 3)} 미만)"
        elif x.get("sector") and sec_n.get(x["sector"], 0) >= smax:
            why = f"같은 업종 {smax}종목까지만"
        elif x.get("eplan") == "skip":
            why = x.get("etxt") or "실적 발표 앞두고 전망치 하향"
        if why:
            skip.append({"code": x["code"], "name": x["name"], "why": why})
            continue
        g = "A" if sc >= 4 else "B"
        rp = rg.get(g, c.get("risk_pct", 1.0))
        small = ai.get("view") == "중립" and rg.get("B") is not None and rp > rg["B"]
        if small:
            rp = rg["B"]
        per = (x.get("close") or 0) - (x.get("stop") or 0)
        per_q = max(per, float(x.get("eper") or 0))          # 실적을 끼고 가면 실적 갭 기준으로 수량 줄임
        q = int(cap * rp / 100 / per_q) if per_q > 0 and cap else 0
        q = min(q, int(cap * c.get("cap_pct", 20) / 100 / x["close"])) if x.get("close") and cap else q
        row = {"code": x["code"], "name": x["name"], "score": sc, "grade": g, "qty": q, "close": x.get("close"), "stop": x.get("stop"),
               "amount": money(mk, q * (x.get("close") or 0)), "loss": money(mk, q * per) if per > 0 else None,
               "small": small, "ai": ai.get("view"), "rr": (x.get("rr") or {}).get("rr"),
               "earn_note": x.get("etxt") if x.get("eplan") in ("hold", "early") else None}
        if q < 1:
            skip.append({"code": x["code"], "name": x["name"],
                         "why": "실적 갭이 커서 1주도 손실 한도를 넘음" if x.get("eplan") in ("hold", "early") else "손실 한도로는 1주도 안 됨(비싼 종목)"})
            continue
        if len(buy) < slots:
            buy.append(row)
            if x.get("sector"):
                sec_n[x["sector"]] = sec_n.get(x["sector"], 0) + 1
        elif len(wait) < 3:
            wait.append(row)
    seen = {r["code"] for r in buy + wait + skip}
    for x in d.get("picks", []):
        if x.get("eplan") == "hold" and x["code"] not in seen and (x.get("score") or 0) >= c.get("min_forward_score", 3):
            skip.append({"code": x["code"], "name": x["name"],
                         "why": "실적 발표가 보유 기간 안이라 뒤로 미룸 ("
                                + (x["etxt"].split(" · ")[0].replace("실적 반응일 ", "발표 반응 ") if str(x.get("etxt", "")).startswith("실적 반응일")
                                   else "실적 시즌인데 발표일을 몰라요") + ")"})
    b = A.STATE.get("bots", {}).get(mk)
    return {"market": mk, "date": d.get("date"), "live": d.get("live", False), "index_today": d.get("index_today"),
            "at": next_buy_time(mk, now), "slots": slots, "buy": buy, "wait": wait,
            "skip": skip[:12], "running": bool(b is not None and getattr(b, "phase", "") not in ("종료됨", "")),
            "robot": bool(A.running()) if hasattr(A, "running") else False}



# ───────── 계속 다시 보기: 20분마다 데스크·후보·보유 종목을 토스 일봉으로 다시 판정, 보유·오늘 살 종목이 악재로 바뀌면 알림 ─────────
LIVE: dict = {}
LAST_BIAS: dict = {}


def live_stats(mk: str, code: str, get_bars=None):
    from . import swing as SW
    b = (get_bars or bars)(code)
    if len(b) < 20:
        return None
    c = b[-1][4]
    sc, _ = SW.forward_score(mk, b, c, {}, {}, 0)
    hi = max(x[2] for x in b[-252:])
    ma20 = sum(x[4] for x in b[-20:]) / 20
    return {"t": time.time(), "c": sc, "ld": bool(SW.long_downtrend(b, c)), "dd": round(c / hi - 1, 3), "d20": round(c / ma20 - 1, 3),
            "close": c}


def watch_codes(mk: str) -> dict:
    """다시 볼 종목: 보유 + 오늘 살 예정 + 후보 + 데스크(가격 계획·장중 관심·실적 일정) → {code: 중요한가(알림 대상)}"""
    from . import app as A
    out = {}
    try:
        for h in A.holdings():
            if h["market"] == mk:
                out[h["symbol"]] = True
    except Exception:
        pass
    try:
        for x in plan(mk)["buy"]:
            out[x["code"]] = True
    except Exception:
        pass
    try:
        for x in live_or_night(mk).get("picks", [])[:20]:
            out.setdefault(x["code"], False)
    except Exception:
        pass
    try:
        d = desk(mk)
        for x in d["live"]["plans"] + d["report"]["plans"] + d["live"]["watch"] + d["earn"]["upcoming"][:30]:
            out.setdefault(x["code"], False)
    except Exception:
        pass
    return out


def refresh_once(mk: str, get_bars=None, notify=None):
    from . import push as PU
    from .main import FLAG
    codes = watch_codes(mk)
    for code in list(codes)[:100]:
        try:
            st = live_stats(mk, code, get_bars)
            if st:
                LIVE[(mk, code)] = st
        except Exception:
            pass
    ctx = context(mk)
    nm = name_map(mk, [c for c, imp in codes.items() if imp])
    for code, imp in codes.items():
        if not imp:
            continue
        t = tags(mk, code, ctx)
        prev = LAST_BIAS.get((mk, code))
        LAST_BIAS[(mk, code)] = t["bias"]
        if prev in ("호재", "엇갈림") and t["bias"] == "악재":
            why = " · ".join(t.get("why") or [])[:80]
            (notify or PU.push_event)("warn", f"{FLAG[mk]} ⚠️ {nm.get(code, code)} {prev}→악재로 바뀌었어요", why or "탭해서 확인", mk, code)


def _refresh_loop():
    import datetime as dt
    while True:
        try:
            if dt.datetime.now(dt.timezone.utc).weekday() < 6:
                for mk in ("KR", "US"):
                    try:
                        refresh_once(mk)
                    except Exception as e:
                        print("다시 보기 오류", mk, e)
        except Exception:
            pass
        time.sleep(1200)


def start_refresh():
    import threading
    threading.Thread(target=_refresh_loop, daemon=True).start()
