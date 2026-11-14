"""실시간 단타 로봇 본체.

실행
  python -m bot.main check          연결 확인만 (주문 없음)
  python -m bot.main paper          연습 모드 (실제 주문 없음) — 처음 2주는 이걸로
  python -m bot.main live           실전 모드 (config.json 의 live.enabled 가 true 일 때만)

하루 흐름 (국내 기준, 한국시간)
  08:55  켜짐 → 오늘 장 열리는 날인지 확인 → 후보 종목 고르기 → 1분봉 불러오기 → 실시간 구독
  09:15~ 시초 15분 고점 돌파 + VWAP 위 + 거래량 급증이면 매수 → 익절·손절 조건주문
  14:30  새 진입 중단
  15:15  남은 물량 전부 매도 → 오늘 결과 정리 → 종료
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from .api import TossAPI, TossError
from .broker import LiveBroker, PaperBroker
from .notify import Notifier
from .risk import Risk
from .strategy import SymState, corr, entry_signal, exit_levels, session_bars, vwap
from .util import TZ, add_ticks, f, money, now, parse_hm, parse_ts, round_tick
from . import etf as ETF
from . import swing as SW

ROOT = Path(__file__).resolve().parent.parent
import re as _re
LEV = _re.compile(r"(\b[23]X\b|Bull|Bear|Leverag|Inverse|UltraPro|UltraShort|Ultra 2x|레버리지|인버스|곱버스|2배|3배)", _re.I)
BAD_WARNINGS = {"LIQUIDATION_TRADING", "INVESTMENT_WARNING", "INVESTMENT_RISK", "OVERHEATED",
                "VI_STATIC_AND_DYNAMIC", "VI_STATIC", "VI_DYNAMIC"}


def load_env():
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Logger:
    def __init__(self, path: Path):
        import collections
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = open(path, "a", encoding="utf-8")
        self.recent = collections.deque(maxlen=300)

    def __call__(self, msg: str):
        line = f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        self.recent.append(line)
        self.fp.write(line + "\n")
        self.fp.flush()


_API = None
_API_LOCK = __import__("threading").Lock()


def shared_api() -> TossAPI:
    """국장·미장 로봇이 토큰 하나를 같이 쓰게 (토스는 클라이언트당 토큰이 1개만 유효)."""
    global _API
    with _API_LOCK:
        if _API is None:
            load_env()
            _API = TossAPI(os.getenv("TOSS_CLIENT_ID", ""), os.getenv("TOSS_CLIENT_SECRET", ""), ROOT / ".token.json")
        return _API


def load_settings() -> dict:
    """화면·텔레그램에서 바꾼 값 (settings.json). 없으면 빈 값."""
    try:
        return json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(d: dict):
    (ROOT / "settings.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def markets_of(cfg: dict) -> list:
    ms = cfg.get("markets") or [cfg.get("market", "KR")]
    return [m for m in ms if m in ("KR", "US")]


FLAG = {"KR": "🇰🇷", "US": "🇺🇸"}


class Bot:
    def __init__(self, mode: str, market: str | None = None):
        load_env()
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.m = market or markets_of(cfg)[0]
        cfg["market"] = self.m          # 전략·안전장치가 이 시장 설정을 쓰게
        cap = load_settings().get("capital", {}).get(self.m)
        if cap:                         # 화면·텔레그램에서 정한 '오늘 투자금'
            cfg["risk"][self.m]["daily_capital"] = float(cap)
        self.cfg = cfg
        self.mode = mode
        day = now(self.m).date().isoformat()
        self.log = Logger(ROOT / "logs" / f"bot-{self.m}-{day}.log")
        self.notify = Notifier(prefix=FLAG[self.m] + (" [연습] " if mode == "paper" else " "))
        self.api = shared_api()
        self.dir = ROOT / "logs" / ("paper" if mode == "paper" else "live") / self.m
        old = ROOT / "logs" / ("paper" if mode == "paper" else "") / "state.json"
        if self.m == "KR" and old.exists() and not (self.dir / "state.json").exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            old.replace(self.dir / "state.json")   # 예전 버전 보유 기록 옮기기
        self.risk = Risk(self.cfg, ROOT / "STOP")
        self.syms: dict[str, SymState] = {}
        self.ctx: dict = {"plans": {}, "daily": {}, "earn_block": set(), "watched": set(), "pre": {}}
        self.dirty: set = set()          # 새 체결이 들어와 다시 점검할 종목
        self.hot: dict = {}              # 크게 움직인 종목 → 분봉을 먼저 보정
        self._bg_stop = True
        self.stream = None
        self.stop_req: str | None = None   # None / "keep"(보유 유지하고 끝) / "sell"(전부 팔고 끝)
        self.sell_reqs: list[str] = []     # 텔레그램 보유종목 칸에서 '팔기' 누른 종목
        self.buy_reqs: list[dict] = []     # 휴대폰 앱에서 '봇으로 사기' 누른 종목 (스윙 규칙 그대로 점검 후 매수)
        self.loop_on = False               # 장중 루프가 돌고 있을 때만 True (이때만 한 종목 팔기 가능)
        self.stopped: dict = {}            # 최근 손실로 판 종목 → 며칠 다시 안 삼
        self.sw_picks = None               # 일봉 스윙 후보 (전날 장 끝난 뒤 만든 것)
        self.sw_done = ""
        self.phase = "준비 중"
        self.open_t: dt.datetime | None = None
        self.close_t: dt.datetime | None = None

    # ───────── 준비 ─────────
    def pick_account(self):
        accs = self.api.accounts() or []
        brok = [a for a in accs if a.get("accountType") == "BROKERAGE"] or accs
        if not brok:
            raise RuntimeError("계좌를 찾지 못했어요.")
        want = self.cfg.get("account_seq")
        a = next((x for x in brok if want and int(x["accountSeq"]) == int(want)), brok[0])
        self.api.account_seq = int(a["accountSeq"])
        no = str(a.get("accountNo", ""))
        self.log(f"계좌: ****{no[-4:]} (accountSeq {self.api.account_seq})")

    def mark_swing(self, p, sig):
        """차트가 좋은 종목은 스윙(며칠~몇 주)으로: 추적 손절 폭을 넓히고, 최대 익절가를 높이고, 들고 가기 기준을 낮춤."""
        if not self.cfg.get("swing", {}).get("enabled") or p.extra.get("etf"):
            p.extra["q0"] = p.qty
            return
        d = self.ctx.get("daily", {}).get(p.symbol) or {}
        pl = self.ctx.get("plans", {}).get(p.symbol) or {}
        why = []
        if d.get("label") == "관심":
            why.append("일봉 차트 '관심'")
        if d.get("scan") in ("돌파", "추세"):
            why.append(f"일봉 {d['scan']}")
        if "주" in str(pl.get("horizon", "")) or pl.get("style") == "스윙":
            why.append("데스크 스윙 후보")
        if p.symbol in self.ctx.get("earn_good", {}):
            why.append("실적 서프라이즈")
        p.extra["q0"] = p.qty
        if d.get("support"):
            p.extra["support"] = d["support"]
        if why:
            p.extra["swing"] = True
            p.tp = max(p.tp, round(p.entry * (1 + self.cfg["swing"].get("hard_pct", 40) / 100), 2))
            self.log(f"📈 {p.name} 스윙 후보로 관리 ({', '.join(why)}) · 추적 손절 폭 넓게, 들고 가기 기준 낮게")
        self.broker.save_state()

    def _bg_loop(self, no_entry_t):
        """뒤에서 따로 도는 일: ① 분봉 거래량 보정(초당 N종목, 보유·크게 움직인 종목 먼저) ② 후보 목록 갱신.
        매매 판단(메인 루프)은 이 일들 때문에 멈추지 않는다."""
        while not self._bg_stop:
            t0 = time.time()
            self._bg_tick(no_entry_t)
            time.sleep(max(0.05, 1.0 - (time.time() - t0)))

    def _bg_tick(self, no_entry_t):
        bs = self.__dict__.setdefault("_bgs", {"rr": 0, "last_uni": time.time(), "warned": False})
        rate = int(self.cfg["live"].get("rest_per_sec", 8))
        try:
            n = now(self.m)
            if (time.time() - bs["last_uni"] > self.cfg["universe"]["refresh_min"] * 60 and n < no_entry_t
                    and not self.stop_req):
                bs["last_uni"] = time.time()
                old = set(self.universe)
                self.build_universe()
                new = [x for x in self.universe if x not in old]
                if new:
                    self.refresh_bars(new)
                    self.log(f"후보 갱신: 새로 {len(new)}개 (총 {len(self.universe)}개)")
                self.stream.set_codes(list(dict.fromkeys(list(self.broker.pos) + self.universe)))
            watch = list(dict.fromkeys(list(self.broker.pos) + self.universe))
            if watch:
                hot = [x for x, t in list(self.hot.items()) if time.time() - t < 90 and x in self.syms]
                pri = list(dict.fromkeys(list(self.broker.pos) + hot))[: max(1, rate // 2)]
                rest = []
                while len(pri) + len(rest) < rate and len(rest) < len(watch):
                    rest.append(watch[bs["rr"] % len(watch)])
                    bs["rr"] += 1
                for sym in dict.fromkeys(pri + rest):
                    try:
                        self.syms[sym].load_candles(self.api.candles(sym, "1m", 5), self.m)
                    except Exception:
                        pass
        except Exception as e:
            if not bs["warned"]:
                self.log(f"뒤 작업 오류(계속 진행): {e}")
                bs["warned"] = True

    def load_desk(self):
        """주식비서 데스크 자료(GitHub docs/data/desk-<m>-*.json)를 읽는다. 없으면 조용히 건너뜀.
        - report: 마감 리포트의 1순위·픽·관심종목 가격 계획, 테마·이슈 종목
        - live:   장중 알림의 '지금 1순위'(더 최신이라 report 보다 우선)
        - earn:   실적 발표 일정과 발표 직후 결과(좋음/나쁨)
        """
        ctx = self.ctx
        ctx["plans"], ctx["earn_block"], ctx["earn_good"], ctx["watched"] = {}, set(), {}, set()
        ctx["earn_bad"] = set()
        base = self.cfg.get("desk_feed_url")
        if not base or os.getenv("TOSSBOT_OFFLINE") == "1":
            return
        import requests
        today = now(self.m).date()
        m = self.m.lower()

        def get(kind):
            try:
                r = requests.get(base.format(m=m, kind=kind), timeout=20)
                return r.json() if r.status_code == 200 else None
            except Exception:
                return None

        def fresh(j, days):
            d = (j or {}).get("date") or (j or {}).get("updatedAt") or ""
            try:
                return (today - dt.date.fromisoformat(d[:10])).days <= days
            except ValueError:
                return False

        rep, live, earn = get("report"), get("live"), get("earn")
        if fresh(rep, 4):
            for code, x in (rep.get("plans") or {}).items():
                if x.get("stop") and x.get("entryLow") and x.get("entryHigh"):
                    ctx["plans"][code] = x
            ctx["watched"].update(rep.get("watch") or [])
        if fresh(live, 0):
            for code, x in (live.get("plans") or {}).items():
                if x.get("stop") and x.get("entryLow") and x.get("entryHigh"):
                    ctx["plans"][code] = {**ctx["plans"].get(code, {}), **x}
            ctx["watched"].update(live.get("watch") or [])
            for code, x in (live.get("earn_good") or {}).items():   # 장중 잠정실적 서프라이즈(한국)
                ctx["earn_good"][code] = x
        if fresh(earn, 3):
            for code, x in (earn.get("good") or {}).items():
                try:
                    if 0 <= (today - dt.date.fromisoformat(str(x.get("date"))[:10])).days <= 3:
                        ctx["earn_good"][code] = x
                except ValueError:
                    pass
            bad = set(earn.get("bad") or {})
            for code, d in (earn.get("upcoming") or {}).items():
                try:
                    ed = dt.date.fromisoformat(str(d)[:10])
                except ValueError:
                    continue
                if 0 <= (ed - today).days <= 1 and code not in ctx["earn_good"]:
                    ctx["earn_block"].add(code)        # 발표 전(오늘·내일) → 결과 모르니 새로 안 삼
            ctx["earn_block"].update(c for c in bad if c not in ctx["earn_good"])  # 실적 실망 → 안 삼
            ctx["earn_bad"].update(c for c in bad if c not in ctx["earn_good"])
        ctx["watched"].update(ctx["plans"])
        ctx["watched"].update(ctx["earn_good"])
        if rep or live or earn:
            self.log(f"데스크 연결: 가격 계획 {len(ctx['plans'])} · 관심 {len(ctx['watched'])} · 실적 서프라이즈 {len(ctx['earn_good'])}"
                     f" · 실적 발표 전이라 안 사는 종목 {len(ctx['earn_block'])}")

    def us_trading_now(self, n) -> bool:
        """SPY 1분봉이 최근 15분 안에 찍혔으면 미국장이 실제로 열려 있는 것."""
        try:
            bars = self.api.candles("SPY", "1m", 5) or []
            ts = [parse_ts(b.get("timestamp"), "US") for b in bars]
            ts = [t for t in ts if t]
            return bool(ts) and (n - max(ts)).total_seconds() < 900
        except Exception:
            return False

    def session_times(self) -> bool:
        """다음으로 매매할 장의 시작·끝 시각을 정한다.
        오늘 장이 열리고 아직 안 끝났으면 오늘, 아니면 토스 장 일정의 '다음 영업일'(주말·공휴일 자동 건너뜀)."""
        s = self.cfg["session"][self.m]
        tz = TZ[self.m]
        n = now(self.m)

        def reg_of(day: dict):
            if not day or not day.get("date"):
                return None
            box = day.get("integrated") if self.m == "KR" else day
            r = (box or {}).get("regularMarket") if box else None
            if not r:
                return None
            st, en = parse_ts(r.get("startTime"), self.m), parse_ts(r.get("endTime"), self.m)
            if not (st and en):
                d = dt.date.fromisoformat(day["date"])
                st = dt.datetime.combine(d, parse_hm(s["open"]), tz)
                en = dt.datetime.combine(d, parse_hm(s["close"]), tz)
            return st, en

        try:
            cal = self.api.calendar(self.m) or {}
            if not getattr(self, "_cal_logged", False):          # 시간외(NXT·프리/애프터) 지원 여부 확인용 한 줄
                self._cal_logged = True
                try:
                    tdy = cal.get("today") or {}
                    box = tdy.get("integrated") if self.m == "KR" else tdy
                    self.log("장 일정 구성(시간외 확인용): " + json.dumps({k: (list(v) if isinstance(v, dict) else v)
                                                                 for k, v in (box or {}).items()}, ensure_ascii=False)[:400])
                except Exception:
                    pass
            pick = None
            # 미장은 토스 일정이 한국 날짜 기준이라, 한국 자정 이후엔 지금 열린 장이 previousBusinessDay 에 있다
            # 마감 정리 시각(예: 15:15·15:45)이 이미 지난 장은 건너뜀 → 하루를 끝낸 뒤 같은 장을 다시 시작하지 않게
            cut = dt.timedelta(minutes=(parse_hm(s["close"]).hour * 60 + parse_hm(s["close"]).minute)
                               - (parse_hm(s["flatten"]).hour * 60 + parse_hm(s["flatten"]).minute))
            for key in ("previousBusinessDay", "today", "nextBusinessDay"):
                r = reg_of(cal.get(key) or {})
                if r and n < r[1] - cut:
                    pick = r
                    break
            # 미장: 토스 일정의 'today' 는 한국 날짜 기준이라, 한국 자정이 지나면 지금 열려 있는
            # 뉴욕 오늘 장이 목록에서 빠진다 → 뉴욕 오늘 장이 진행 중이면 그것을 쓴다(실제 시세로 확인).
            if self.m == "US" and (not pick or pick[0] > n):
                o = dt.datetime.combine(n.date(), parse_hm(s["open"]), tz)
                c = dt.datetime.combine(n.date(), parse_hm(s["close"]), tz)
                if n.weekday() < 5 and o <= n < c - cut and self.us_trading_now(n):
                    self.log("뉴욕 오늘 장이 열려 있어요 → 지금 장으로 시작 (토스 일정은 한국 날짜 기준이라 보정)")
                    pick = (o, c)
            if not pick:
                self.log("토스 장 일정에서 다음 장을 찾지 못했어요.")
                return False
            self.open_t, self.close_t = pick
        except TossError as e:
            self.log(f"장 일정 조회 실패({e.code}) → 설정 시간({s['open']}~{s['close']})으로 진행")
            d = n.date()
            while True:
                o = dt.datetime.combine(d, parse_hm(s["open"]), tz)
                c = dt.datetime.combine(d, parse_hm(s["close"]), tz)
                if d.weekday() < 5 and n < c:
                    break
                d += dt.timedelta(days=1)
            self.open_t, self.close_t = o, c
        if self.open_t.date() != n.date():
            self.log(f"다음 장: {self.open_t.strftime('%m/%d(%a) %H:%M')} ({'한국' if self.m == 'KR' else '뉴욕'} 시간) 에 시작해요.")
        return True

    def at(self, hm: str) -> dt.datetime:
        return dt.datetime.combine(self.open_t.date(), parse_hm(hm), TZ[self.m])

    def build_universe(self):
        u = self.cfg["universe"]
        picks: dict[str, str] = {}
        for t, dur in u["rankings"]:
            try:
                r = self.api.rankings(t, self.m, dur, u["per_ranking"], True)
                for it in r.get("rankings", []):
                    picks.setdefault(it["symbol"], "")
            except TossError as e:
                self.log(f"랭킹 조회 실패 {t}: {e}")
        for s in self.cfg.get("watchlist", {}).get(self.m, []):
            picks.setdefault(s, "")
        self.load_desk()
        self.premarket_scan(list(self.ctx["earn_good"]) + list(self.ctx["plans"]) + list(picks) + list(self.ctx["watched"]))
        pre = sorted(self.ctx["pre"], key=lambda c: -self.ctx["pre"][c])
        front = (list(self.ctx["earn_good"]) + list(self.ctx["plans"]) + pre
                 + [c for c in self.ctx["watched"] if c not in self.ctx["plans"]])
        picks = {**{c: "" for c in front}, **picks}       # 실적 서프라이즈·데스크 종목을 맨 앞에
        gh = self.cfg.get("github_chart_url")
        if gh:
            try:
                import requests
                j = requests.get(gh.format(m=self.m.lower()), timeout=20).json()
                for c, d in (j.get("stocks") or {}).items():
                    vols = [b[5] for b in (d.get("bars") or [])[-21:-1] if len(b) > 5 and b[5]]
                    self.ctx["daily"][c] = {"support": d.get("support"), "resistance": d.get("resistance"), "label": d.get("label"),
                                            "vol_min": (sum(vols) / len(vols) / 390) if vols else None}
                for k in ("breakout", "rebound", "trend"):
                    for c in j.get("scan", {}).get(k, [])[:10]:
                        if k != "trend":
                            picks.setdefault(c, "")
                        self.ctx["watched"].add(c)
                        self.ctx["daily"].setdefault(c, {})["scan"] = {"breakout": "돌파", "rebound": "반등", "trend": "추세"}[k]
            except Exception as e:
                self.log(f"깃허브 차트 후보 불러오기 실패: {e}")
        codes = list(picks)
        # 종목 정보로 걸러내기: 보통주(STOCK)만, ETF·ETN·스팩 등 제외
        keep = []
        try:
            for info in self.api.stocks(codes):
                sym = info.get("symbol")
                stype = str(info.get("securityType") or "")
                is_fund = ("ETF" in stype or "ETN" in stype)
                if stype not in ("STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPT") and not (is_fund and u.get("allow_etf")):
                    continue
                self.syms.setdefault(sym, SymState(sym)).etf = is_fund
                if self.m == "KR" and info.get("isCommonShare") is False:
                    continue
                nm = info.get("name") or sym
                if any(w in nm for w in ("스팩", "SPAC")):
                    continue
                if not u.get("allow_etf") and (is_fund or LEV.search(nm)):
                    continue                            # 레버리지·인버스·ETF 제외
                keep.append(sym)
                self.syms.setdefault(sym, SymState(sym)).name = nm
        except TossError as e:
            self.log(f"종목 정보 조회 실패: {e} → 걸러내기 없이 진행")
            keep = codes
        keep = keep[:u["max_symbols"]]
        # 경고 종목(과열·투자경고·VI 등) 제외
        for sym in keep:
            st = self.syms.setdefault(sym, SymState(sym))
            try:
                ws = self.api.warnings(sym)
                bad = [w.get("warningType") for w in ws if w.get("warningType") in BAD_WARNINGS and not w.get("endDate")]
                st.excluded = ",".join(bad) if bad else None
            except TossError:
                pass
        # 전일 종가: 일봉 2개
        for sym in keep:
            self.prev_close(sym)
        self.universe = [s for s in keep if not self.syms[s].excluded]
        self.load_daily(self.universe)
        ex = [f"{self.syms[s].name}({self.syms[s].excluded})" for s in keep if self.syms[s].excluded]
        self.log(f"후보 {len(self.universe)}개: " + ", ".join(self.syms[s].name or s for s in self.universe[:40])
                 + (f" … 제외 {len(ex)}개" if ex else ""))

    # ───────── 실시간 뉴스 ─────────
    def _news_loop(self, no_entry_t):
        while not getattr(self, "_bg_stop", False):
            try:
                self.news_tick(no_entry_t)
            except Exception as e:
                self.log(f"📰 뉴스 감시 오류: {e}")
            time.sleep(5)

    def news_tick(self, no_entry_t):
        c = self.cfg.get("news", {})
        if not c.get("enabled") or os.getenv("TOSSBOT_OFFLINE") == "1":
            return
        if getattr(self, "news", None) is None:
            from .news import NewsWatch
            names = {s: st.name for s, st in self.syms.items() if st.name}
            try:
                import requests
                raw = self.cfg.get("allstocks_url", "https://raw.githubusercontent.com/ehdqls5892-netizen/stock-chart/data-{m}/index.json")
                for it in (requests.get(raw.format(m=self.m.lower()), timeout=20).json().get("items") or []):
                    names.setdefault(it[0], it[1])
            except Exception as e:
                self.log(f"📰 종목 이름 목록을 못 받아 후보 종목 이름으로만 뉴스를 봐요: {e}")
            self.news = NewsWatch(self.m, names, self.log, self.notify, c.get("queries_per_min", 6), c.get("max_age_min", 90))
            self.ctx["news_good"], self.ctx["news_bad"] = self.news.good, self.news.bad
            self.log(f"📰 실시간 뉴스 감시 시작 (종목 {len(names)}개 이름으로 찾음, 1분마다)")
        for code, tone, title in self.news.poll():
            st = self.syms.get(code)
            nm = (st.name if st and st.name else None) or code
            if tone > 0:
                self.log(f"📰 호재 뉴스 {nm}: {title}")
                if code not in self.universe and now(self.m) < no_entry_t:
                    self.add_symbol(code, "뉴스 호재")
            else:
                self.log(f"📰 악재 뉴스 {nm}: {title}")
                p = self.broker.pos.get(code) if getattr(self, "broker", None) else None
                if p:
                    self.notify(f"⚠️ 보유 중인 {p.name} 악재 뉴스: {title}\n→ 손절 판단선 아래로 가면 기다리지 않고 바로 정리해요")

    def add_symbol(self, sym, why=""):
        """장중에 새 종목을 후보에 바로 추가 (보통주·경고 없음 확인 → 분봉 받기 → 실시간 구독)."""
        try:
            info = (self.api.stocks([sym]) or [{}])[0]
            stype = str(info.get("securityType") or "")
            if stype not in ("STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPT"):
                return
            if LEV.search(info.get("name") or ""):
                return
            if self.m == "KR" and info.get("isCommonShare") is False:
                return
            st = self.syms.setdefault(sym, SymState(sym))
            st.name = info.get("name") or st.name or sym
            ws = self.api.warnings(sym)
            if any(w.get("warningType") in BAD_WARNINGS and not w.get("endDate") for w in ws):
                return
            self.prev_close(sym)
            st.load_candles(self.api.candles(sym, "1m", 120), self.m)
            self.load_daily([sym])
        except Exception as e:
            self.log(f"후보 추가 실패 {sym}: {e}")
            return
        self.universe = [sym] + [x for x in self.universe if x != sym]
        if self.stream:
            self.stream.set_codes(list(dict.fromkeys(list(self.broker.pos) + self.universe)))
        self.log(f"➕ 후보 추가 {st.name}({why}) · 차트 신호가 나오면 우선 매수")

    def fetch_daily(self, code):
        """전 종목 일봉(GitHub data-<m>/s/<code>.json)의 bars. 없으면 []."""
        import requests
        url = self.cfg.get("allstocks_bar_url", "https://raw.githubusercontent.com/ehdqls5892-netizen/stock-chart/data-{m}/s/{code}.json")
        try:
            r = requests.get(url.format(m=self.m.lower(), code=code), timeout=10)
            return (r.json().get("bars") or []) if r.status_code == 200 else []
        except Exception:
            return []

    def load_daily(self, syms):
        """일봉 추세 요약(전날까지만)을 종목별로 채운다. 하루 한 번, 이미 있으면 건너뜀."""
        if not (self.cfg["strategy"].get("filters", {}).get("daily_trend") or self.cfg.get("average_down", {}).get("enabled")):
            return
        today = now(self.m).strftime("%y%m%d")
        for sym in syms:
            st = self.syms.get(sym)
            if not st or st.daily.get("_d") == today:
                continue
            bars = [b for b in self.fetch_daily(sym) if str(b[0]) < today and b[4]]
            st.daily = {"_d": today}
            if len(bars) >= 25:
                cl = [b[4] for b in bars]
                st.daily.update(close=cl[-1], ma5=sum(cl[-5:]) / 5, ma20=sum(cl[-20:]) / 20,
                                ma20_prev=sum(cl[-25:-5]) / 20, ma60=sum(cl[-60:]) / min(60, len(cl)),
                                high20=max(b[2] for b in bars[-20:]))

    def prev_close(self, sym):
        st = self.syms.setdefault(sym, SymState(sym))
        if st.prev_close is None:
            try:
                d = self.api.candles(sym, "1d", 2)
                today = now(self.m).date()
                prev = [(parse_ts(c.get("timestamp"), self.m), c) for c in d]
                prev = [x for x in prev if x[0] and x[0].date() < today]
                if prev:
                    st.prev_close = f(max(prev, key=lambda x: x[0])[1].get("closePrice"))
            except TossError:
                pass
        return st.prev_close

    def premarket_scan(self, pool):
        """장 시작 몇 분 전: 프리마켓(한국은 장전 시간외·동시호가)에서 이미 많이 오른 종목을 미리 후보 맨 앞에 넣는다.
        여기서 사지는 않는다 — 장이 열리면 '장초반 출발'(첫 1~15분)로 정규장 가격·거래량을 확인하고 산다."""
        c = self.cfg.get("premarket", {})
        if not c.get("enabled") or now(self.m) >= self.open_t:
            return
        pool = [x for x in dict.fromkeys(pool) if x][: c.get("max_check", 100)]
        if not pool:
            return
        try:
            px = {x.get("symbol"): f(x.get("lastPrice")) for x in (self.api.prices(pool) or [])}
        except Exception as e:
            self.log(f"프리마켓 시세 조회 실패: {e}")
            return
        found = []
        for sym in pool:
            p, pc = px.get(sym), self.prev_close(sym)
            if not p or not pc:
                continue
            g = (p / pc - 1) * 100
            if c.get("min_gap_pct", 2) <= g <= c.get("max_gap_pct", 15):
                found.append((g, sym))
        same = sum(1 for sym in pool if px.get(sym) and self.syms[sym].prev_close and px[sym] == self.syms[sym].prev_close)
        if pool and same >= len(pool) * 0.9:
            self.log("🌅 프리마켓 시세가 안 들어와요(지금 가격 = 어제 종가). 장 열린 뒤 첫 1~15분 '장초반 출발'로만 잡아요")
        found.sort(reverse=True)
        found = found[: c.get("max_add", 25)]
        self.ctx["pre"] = {sym: round(g, 1) for g, sym in found}
        if found:
            self.log("🌅 프리마켓 갭 상승 후보 " + str(len(found)) + "개 (장 열리면 바로 확인): "
                     + ", ".join(f"{self.syms[s].name or s} {g:+.1f}%" for g, s in found[:10]))

    def refresh_bars(self, syms=None):
        for sym in syms or self.universe:
            try:
                self.syms[sym].load_candles(self.api.candles(sym, "1m", 120), self.m)
            except TossError as e:
                if e.status == 404:
                    self.syms[sym].excluded = "종목 없음"
            except Exception as e:
                self.log(f"분봉 오류 {sym}: {e}")

    # ───────── 실시간 콜백 ─────────
    def on_trade(self, sym, price, vol, ts):
        st = self.syms.get(sym)
        if not st:
            return
        self.dirty.add(sym)
        prev = st.last
        if prev and abs(price / prev - 1) >= 0.002:      # 0.2% 넘게 튀면 '뜨거운 종목' → 분봉 거래량을 먼저 다시 맞춤
            self.hot[sym] = time.time()
        t = parse_ts(ts, self.m) or now(self.m)
        st.on_trade(price, vol, t)
        self.broker.on_price(sym, price, t)
        sh = getattr(self, "shadow", None)
        if sh is not None and sym in sh.pos:
            try:
                sh.on_price(sym, price, t)
            except Exception:
                pass

    def on_closed(self, p):
        self.risk.record_exit(p.pnl)
        try:
            self.dd_check()
        except Exception as e:
            self.log(f"낙폭 확인 오류: {e}")
        st = self.syms.get(p.symbol)
        if p.pnl < 0 and p.state == "closed" and p.exit_price and p.exit_price < p.entry:
            self.stopped[p.symbol] = now(self.m).date().isoformat()     # 손실로 판 종목은 며칠 다시 안 삼
        if p.state == "closed" and p.extra.get("sw"):
            self.recent_sold = getattr(self, "recent_sold", {})
            self.recent_sold[p.symbol] = now(self.m).date().isoformat()  # 판 스윙 종목은 이익이어도 며칠 다시 안 삼 (사고팔기 반복·수수료 방지)
        if st and p.pnl < 0:
            st.cooldown_until = now(self.m) + dt.timedelta(minutes=self.cfg["strategy"]["cooldown_min"])

    # ───────── 계좌 낙폭 한도: 누적 손익이 고점에서 투자금의 N% 넘게 줄면 전부 정리하고 '재개' 전까지 멈춤 ─────────
    def dd_check(self, flatten: bool = True):
        lim = self.cfg["risk"][self.m].get("max_drawdown_pct", 0)
        if not lim or not self.risk.capital:
            return
        st = load_settings()
        lock = (st.get("dd_lock") or {}).get(self.m)
        if not lock:
            from .stats import load
            base = (st.get("dd_base") or {}).get(self.m, "")
            eq = peak = 0.0
            for r in sorted(load(ROOT, self.m, 3650, self.mode), key=lambda r: r.get("t", "")):
                if base and r.get("t", "") < base:
                    continue
                eq += float(r.get("pnl") or 0)
                peak = max(peak, eq)
            dd = peak - eq
            if dd < self.risk.capital * lim / 100:
                return
            lock = f"{now(self.m).strftime('%m/%d %H:%M')} 누적 손익 고점 대비 −{money(self.m, dd)} (한도 {lim}%)"
            st.setdefault("dd_lock", {})[self.m] = lock
            save_settings(st)
            msg = (f"🛑 계좌 낙폭 한도 도달: {lock}\n→ 들고 있는 종목을 전부 정리하고 새 매수를 멈춰요. "
                   f"원인을 확인한 뒤 텔레그램에서 '재개'를 보내면 다시 시작해요.")
            self.log(msg)
            self.notify(msg)
            if flatten and getattr(self, "broker", None):
                for sym in list(self.broker.pos):
                    stt = self.syms.get(sym)
                    self.broker.flatten(sym, "계좌 낙폭 한도 — 전부 정리", stt.last if stt else None)
        self.risk.halted = f"계좌 낙폭 한도로 멈춤 ({lock}) — 텔레그램 '재개'로 다시 시작"

    # ───────── 성적 나쁜 매수 방식 자동 끄기 ─────────
    def shadow_tick(self, n, no_entry_t, flatten_t, today):
        """단타 그림자 장부: 단타 신호가 나면 '샀다고 치고' 같은 규칙(손절·나눠 익절·추적)으로 관리, 장 마감에 정리.
        실제 주문은 없음. 확신도 등급과 상관없이 모든 신호를 기록 (등급별로 나중에 비교)."""
        sh, rk = self.shadow, self.shadow_risk
        if n >= flatten_t:
            if getattr(self, "_sh_flat", "") != today:
                self._sh_flat = today
                for sym in list(sh.pos):
                    st = self.syms.get(sym)
                    sh.flatten(sym, "장 마감 정리", st.last if st else None)
                got = [p for p in sh.closed]
                if got:
                    tot = sum(p.pnl for p in got)
                    self.notify(f"👻 오늘 단타 그림자(실제 매매 아님): 신호 {len(got)}건 · 샀다면 {money(self.m, tot)}"
                                f" · 승 {sum(1 for p in got if p.pnl > 0)} / 패 {sum(1 for p in got if p.pnl <= 0)}  ('단타기록' 으로 누적 보기)")
            return
        if not (self.open_t <= n < no_entry_t) or time.time() - self._sh_t < 3:
            return
        self._sh_t = time.time()
        if rk.can_enter(sh.open_count(), sh.invested()):
            return
        cands = []
        for sym in self.universe:
            if sym in sh.pos or sym in self.broker.pos or sym not in self.syms:
                continue
            st = self.syms[sym]
            if st.__dict__.get("_sh_n", 0) >= self.cfg["strategy"].get("max_entries_per_symbol", 2):
                continue
            try:
                sig = entry_signal(st, n, self.open_t, self.cfg, self.ctx)
            except RuntimeError:
                sig = None
            if sig:
                cands.append((sig["score"], sym, sig))
        mk = self.market_move()
        if mk is not None and mk <= self.cfg.get("market_filter", {}).get("weak_pct", -0.7):
            cands = [x for x in cands if x[1] in self.ctx.get("earn_good", {})]
        for _, sym, sig in sorted(cands, key=lambda x: -x[0]):
            st = self.syms[sym]
            est = add_ticks(self.m, st.last, 1)
            tp, sl, R, a = exit_levels(self.m, est, sig, self.cfg, st, self.open_t)
            fee = self.cfg["fees"][self.m]
            w = rk.strength(sig)
            q = rk.qty_for(est, add_ticks(self.m, sl, -1), None, (fee["buy_pct"] + fee["sell_pct"] + fee["sell_tax_pct"]) / 100, sh.invested(), w)
            if q < 1:
                continue
            p = sh.enter(sym, st.name or sym, st.last, q, tp, sl, sig["reason"], r=R, atr=a)
            if p:
                p.extra["setup"], p.extra["grade"] = sig.get("setup"), sig.get("grade")
                st._sh_n = st.__dict__.get("_sh_n", 0) + 1
                rk.trades += 1
                self.log(f"👻 [그림자] {st.name or sym} 샀다고 침 · {sig.get('grade') or ''} · {sig['reason'][:40]}")
                break

    def fresh_cash(self, cash, force=False):
        """계좌의 실제 매수 가능 금액으로 다시 맞춤 (20초에 한 번).
        예전엔 장 시작 때 한 번 읽고 살 때마다 빼기만 해서, 판 돈이 돌아와도 반영이 안 됐음
        → 오후엔 '현금이 3만 원 남았다'고 착각해 2주만 산 일이 있었음 (삼성중공업 10/30)."""
        if not getattr(self, "_cash_track", False):
            return cash
        if not force and time.time() - getattr(self, "_cash_t", 0) < 20:
            return cash
        try:
            v = f((self.api.buying_power("KRW" if self.m == "KR" else "USD") or {}).get("cashBuyingPower"))
            self._cash_t = time.time()
            return v if v is not None else cash
        except TossError:
            return cash

    # ───────── 장기 ETF 모으기 (매달 첫 거래일) ─────────
    def etf_advise(self, n):
        """매달 첫 거래일: 직접 사실 ETF 수량 추천 (로봇은 주문하지 않음)."""
        st = load_settings()
        month = n.strftime("%Y-%m")
        if (st.get("etf_month") or {}).get(self.m) == month:
            return
        budget = float((st.get("etf_budget") or {}).get(self.m) or 0)
        st.setdefault("etf_month", {})[self.m] = month
        save_settings(st)
        if budget <= 0:
            return
        from .app import etf_plan_text
        self.notify(f"🏦 {month} ETF 적립일이에요 (직접 매수 · 로봇은 주문 안 함)\n" + etf_plan_text(self.m, budget))

    def etf_buy(self, n, cash) -> float:
        c = self.cfg.get("etf_long", {})
        items = (c.get("list") or {}).get(self.m) or []
        st = load_settings()
        budget = float((st.get("etf_budget") or {}).get(self.m) or 0)
        month = n.strftime("%Y-%m")
        if not items or budget <= 0 or (st.get("etf_month") or {}).get(self.m) == month:
            return 0.0
        if self.risk.kill_switch():
            return 0.0
        try:
            px = {x.get("symbol"): f(x.get("lastPrice")) for x in (self.api.prices([x[0] for x in items]) or [])}
        except TossError as e:
            self.log(f"🏦 ETF 시세 조회 실패: {e}")
            return 0.0
        for code, name, _ in items:            # 레버리지·인버스는 절대 안 삼 (이름 확인)
            try:
                nm = ((self.api.stocks([code]) or [{}])[0].get("name") or "")
            except TossError:
                nm = ""
            if LEV.search(nm):
                px.pop(code, None)
        buys, bank = ETF.plan(budget, items, (st.get("etf_bank") or {}).get(self.m) or {}, px, cash)
        spent, done = 0.0, []
        for code, name, q, price in buys:
            p = self.broker.pos.get(code)
            why = f"🏦 장기 ETF 적립 ({month})"
            if p and p.extra.get("etf_long") and p.state == "open":
                self.broker.add(p, q, price, why)
            elif p:
                bank[code] = bank.get(code, 0) + q * price      # 다른 방식으로 보유 중이면 이번 달은 이월
                continue
            else:
                p = self.broker.enter(code, name, price, q, price * 100, 0.0, why, r=price, atr=None)
                if not p:
                    bank[code] = bank.get(code, 0) + q * price
                    continue
                p.extra.update(etf_long=True, nostop=True, locked=True, carry=True, setup="etf")
                self.broker.save_state()
            spent += q * price
            done.append(f"{name} {q}주")
        st = load_settings()
        st.setdefault("etf_bank", {})[self.m] = {k: round(v, 2) for k, v in bank.items()}
        st.setdefault("etf_month", {})[self.m] = month
        save_settings(st)
        left = ", ".join(f"{nm} {money(self.m, bank.get(cd, 0))}" for cd, nm, _ in items if bank.get(cd, 0) >= 1)
        msg = (f"🏦 {month} ETF 적립 ({money(self.m, budget)}/월): " + (", ".join(done) if done else "이번 달은 1주 값이 안 돼 이월")
               + (f" · 다음 달로 이월 {left}" if left else ""))
        self.log(msg)
        self.notify(msg)
        return spent

    # ───────── 일봉 스윙 ─────────
    def swing_on(self) -> list:
        c = self.cfg.get("daily_swing", {})
        return (c.get("markets") or {}).get(self.m, []) if c.get("enabled") else []

    @staticmethod
    def entry_factors(x, ev):
        """살 때 본 정보를 숫자로 남김 — 나중에 '어떤 정보가 실제로 돈이 됐나'를 실전 기록으로 따져 보려고."""
        ai = (ev or {}).get("ai") or {}
        g = lambda k: (lambda v: round(float(v), 4) if v not in (None, "") else None)((ev or {}).get(k)) if (ev or {}).get(k) not in (None, "") else None
        out = dict(ai=ai.get("view"), ai_adj=ai.get("adj"), ai_veto=ai.get("veto"), rec=g("rec"), tgt_up=g("tgt_up"),
                   n_an=g("n_analysts"), up30=g("up30"), down30=g("down30"), eps_yoy=g("eps_yoy"), surprise=g("last_surprise"),
                   short=g("short_pct"), fpe=g("fpe"), score=x.get("_fscore", x.get("score")), setup=x.get("setup") or "fs")
        for k in ("flow", "frg", "inst", "insider"):
            if (ev or {}).get(k) is not None:
                out[k] = ev[k]
        return {k: v for k, v in out.items() if v is not None}

    def real_review(self, days: int = 120):
        """실전 기록 점검 (매주 한 번): 로봇이 실제로 산 종목을 '살 때 본 정보'별로 나눠 승률·평균 수익을 봄.
        차트만 보는 10년 시험과 달리, 애널리스트·실적 전망·AI 판단·수급까지 그때 본 그대로 반영된 결과."""
        from .stats import load, merge
        recs = [r for r in merge(load(ROOT, self.m, days, self.mode)) if (r.get("extra") or {}).get("sw")]
        if not recs:
            return None

        def pct(r):
            try:
                return (float(r["exit_price"]) / float(r["entry"]) - 1) * 100
            except Exception:
                return 0.0

        def fac(r):
            ex = r.get("extra") or {}
            fc = dict(ex.get("fac") or {})
            why = " ".join(ex.get("fwhy") or [])
            if "rec" not in fc and "애널리스트 의견 매수 우세" in why:
                fc["rec"] = 1.9
            if "up30" not in fc and "이익 전망 상향" in why:
                fc["up30"], fc["down30"] = 1, 0
            fc.setdefault("setup", ex.get("sw_kind") or "fs")
            fc.setdefault("score", ex.get("fscore"))
            return fc

        groups = {
            "AI 판단": lambda f: {"긍정": "긍정", "중립": "중립", "부정": "부정"}.get(f.get("ai"), "판단 없음"),
            "애널리스트 의견": lambda f: "매수 우세(≤2.0)" if (f.get("rec") or 9) <= 2.0 else ("보통 이하" if f.get("rec") else "정보 없음"),
            "목표가 여유": lambda f: "15% 이상" if (f.get("tgt_up") or -1) >= 0.15 else ("15% 미만" if f.get("tgt_up") is not None else "정보 없음"),
            "이익 전망 (30일)": lambda f: "상향이 더 많음" if (f.get("up30") or 0) > (f.get("down30") or 0) else
                                  ("하향이 더 많음" if (f.get("down30") or 0) > (f.get("up30") or 0) else "변화 없음/정보 없음"),
            "점수": lambda f: "10 이상" if (f.get("score") or 0) >= 10 else ("6~9" if (f.get("score") or 0) >= 6 else "5 이하"),
            "방식": lambda f: {"fs": "현재·미래 점수", "hi52": "52주 신고가", "rb": "하락장 저점", "ep": "큰 재료 갭"}.get(f.get("setup"), str(f.get("setup"))),
        }
        L = [f"📋 실전 기록 점검 ({'미장' if self.m == 'US' else '국장'} 최근 {days}일 · 로봇이 산 스윙 {len(recs)}건)",
             "살 때 본 정보별로 나눠 본 실제 결과 (애널리스트·실적 전망·AI·수급 포함):"]
        tot = sum(float(r.get("pnl") or 0) for r in recs)
        L.append(f"전체: 승률 {sum(1 for r in recs if float(r.get('pnl') or 0) > 0) / len(recs) * 100:.0f}% · 평균 {sum(map(pct, recs)) / len(recs):+.2f}% · 합계 {money(self.m, tot)}")
        for gname, fn in groups.items():
            b = {}
            for r in recs:
                b.setdefault(fn(fac(r)), []).append(r)
            if len(b) < 2:
                continue
            L.append(f"\n[{gname}]")
            for k, rs in sorted(b.items(), key=lambda kv: -len(kv[1])):
                w = sum(1 for r in rs if float(r.get("pnl") or 0) > 0)
                L.append(f"  {k}: {len(rs)}건 승률 {w / len(rs) * 100:.0f}% 평균 {sum(map(pct, rs)) / len(rs):+.2f}% 합계 {money(self.m, sum(float(r.get('pnl') or 0) for r in rs))}")
        ex = {}
        for r in recs:
            ex.setdefault(str(r.get("exit_reason") or "?").split(":")[0].split("(")[0].strip()[:18], []).append(r)
        L.append("\n[판 이유]")
        for k, rs in sorted(ex.items(), key=lambda kv: -len(kv[1]))[:6]:
            L.append(f"  {k}: {len(rs)}건 평균 {sum(map(pct, rs)) / len(rs):+.2f}% 합계 {money(self.m, sum(float(r.get('pnl') or 0) for r in rs))}")
        try:                                                  # 안 산 후보는 그 뒤 어떻게 됐나 (이유별)
            hist = json.loads((Path(self.dir) / "missed.json").read_text(encoding="utf-8"))
            mb = {}
            for h in hist:
                for r in h.get("rows", []):
                    if r.get("st") != "매수" and r.get("chg") is not None:
                        mb.setdefault((r.get("why") or "?").split("—")[0].split("(")[0].strip()[:22], []).append(r["chg"])
            if mb:
                L.append("\n[안 산 후보의 그 뒤 변화 (이유별)]")
                for k, v in sorted(mb.items(), key=lambda kv: -len(kv[1]))[:6]:
                    L.append(f"  {k}: {len(v)}건 평균 {sum(v) / len(v):+.1f}% (5% 넘게 오른 것 {sum(1 for x in v if x >= 5)}건)")
        except Exception:
            pass
        L.append("\n※ 건수가 적으면 우연일 수 있어요. 10건 넘게 쌓인 묶음만 판단에 씀.")
        return "\n".join(L)

    def real_review_weekly(self):
        if os.getenv("TOSSBOT_OFFLINE") == "1" or not getattr(self, "dir", None) or not self.swing_on():
            return
        wk = now(self.m).date().isocalendar()
        key = f"{wk[0]}-{wk[1]}"
        f_ = Path(self.dir) / "review_week.txt"
        try:
            if f_.read_text().strip() == key:
                return
        except Exception:
            pass
        txt = Bot.real_review(self)
        f_.write_text(key)
        if txt:
            self.notify(txt)

    def loop_notice(self):
        """매주 GitHub 에서 돈 '매매 점검 루프' 결과(research/loop-proposals.json)가 새로 나오면 한 번 알림.
        통과한 수정안은 '제안'일 뿐 — 로봇 설정은 사용자가 승인해야 바뀜."""
        if self.m != "US" or os.getenv("TOSSBOT_OFFLINE") == "1" or not getattr(self, "dir", None):
            return
        url = self.cfg.get("loop_url", "https://raw.githubusercontent.com/ehdqls5892-netizen/stock-chart/main/tossbot/research/loop-proposals.json")
        seen = Path(self.dir) / "loop_seen.txt"
        try:
            last = seen.read_text().strip()
        except Exception:
            last = ""
        import requests
        j = requests.get(url, timeout=20).json()
        if not j.get("date") or j["date"] <= last:
            return
        seen.write_text(j["date"])
        b, fin, ps = j.get("base") or {}, j.get("final") or {}, j.get("proposals") or []
        msg = [f"🔁 매매 점검 루프 결과 ({j['date']}, 미장 10년 · 고치는 기간/확인 기간 나눠 시험)",
               f"지금 설정: 확인 기간 연 {(b.get('check') or {}).get('cagr', 0):+.1f}%"]
        if ps:
            msg.append("통과한 수정안 (승인하셔야 반영돼요):")
            msg += [f"{i}. {x['desc']} → 연 {x['result']['check']['cagr']:+.1f}%" for i, x in enumerate(ps, 1)]
        else:
            msg.append("통과한 수정안 없음 → 지금 설정 그대로")
        self.notify("\n".join(msg))

    def swing_load(self):
        if not self.swing_on() or os.getenv("TOSSBOT_OFFLINE") == "1":
            return
        c = self.cfg["daily_swing"]
        d = SW.load_picks(c["picks_url"], self.m, self.open_t.date(), self.log)
        if not d:
            return
        Bot.split_picks(self, d)
        pa = getattr(self, "_pm_add", None)
        if pa and pa[0] == self.open_t.date().isoformat():
            hv = {str(x["code"]) for x in d["picks"]}
            d["picks"] = d["picks"] + [x for x in pa[1] if str(x["code"]) not in hv]   # 프리장 점검에서 새로 들어온 종목
        self.sw_picks = d
        self.sw_regime = d.get("regime")                 # 추가 매수 판단용 (시장 좋을 때만)
        if True:
            msg = self.swing_table_text()
            pv = getattr(self, "_pv_table", None)
            if pv is not None and getattr(self, "_pv_sent", "") == self.open_t.date().isoformat():
                now_t = {r[0]["code"]: r[1] for r in getattr(self, "sw_table", [])}
                names = {x["code"]: x["name"] for x in d["picks"]}
                diff = [f"{names.get(k, k)}: {pv.get(k, '없음')} → {now_t.get(k, '없음')}" for k in set(pv) | set(now_t)
                        if pv.get(k) != now_t.get(k)]
                msg += ("\n🔁 미리 보기와 달라진 점: " + " / ".join(diff)) if diff else "\n✅ 미리 보기와 같아요"
        self.log(msg)
        self.notify(msg)

    def swing_timing(self, x, d=None) -> str:
        """이 후보를 언제 사나: entry_at 은 entry_at_since(신호 날짜) 이후 신호부터 적용 — 이미 알린 후보의 시점은 안 바꿈."""
        c = self.cfg["daily_swing"]
        d = d if d is not None else (self.sw_picks or {})
        if str(d.get("date", "")) < str(c.get("entry_at_since", "")):
            return "open"
        eo = c.get("early_conf") or {}
        if eo.get("enabled") and Bot.early_ok(self, x):
            return "open"                         # 확신 높은 종목: 마감까지 안 기다리고 장 시작 후 흐름 확인되면 바로 삼
        return (c.get("entry_at") or {}).get(x.get("setup"), "open")

    def early_ok(self, x) -> bool:
        """확신 종목 = 점수 높음 + AI 부정·매수 금지 아님 + 손익비 충분 + 실적 전 전망치 하향 아님."""
        eo = self.cfg["daily_swing"].get("early_conf") or {}
        ai = (x.get("ev") or {}).get("ai") or {}
        rr = (x.get("rr") or {}).get("rr")
        if not isinstance(x.get("score"), (int, float)) or x["score"] < eo.get("score", 8):
            return False
        if ai.get("veto") or ai.get("view") == "부정" or x.get("no_buy"):
            return False
        if rr is not None and rr < eo.get("min_rr", 2.0):
            return False
        return True

    def swing_premarket(self):
        """정규장 시작 전(프리장: 국장 넥스트레이드 08:00~08:50, 미장 프리마켓) 점검 — 정규장 열리기 전에 끝남.
        1) 후보 전체를 프리장 가격으로 다시 점검·순위 (크게 오르거나 빠진 종목 알림)
        2) 후보 밖 전 종목(밤에 점수 매긴 수백 개)의 프리장 가격도 봐서, 차트가 좋은데 프리장에서 힘이 붙은 종목을 후보에 넣음
        여기서 사지는 않음 — 매수는 정규장에서 지금 가격으로 한 번 더 점검하고."""
        import requests
        c = self.cfg["daily_swing"]
        pc = c.get("premarket_review") or {}
        if not pc.get("enabled", True) or not self.swing_on() or os.getenv("TOSSBOT_OFFLINE") == "1":
            return
        key = self.open_t.date().isoformat()
        if getattr(self, "_pm_done", "") == key:
            return
        self._pm_done = key
        d = SW.load_picks(c["picks_url"], self.m, self.open_t.date(), lambda *a: None)
        if not d or not d.get("picks"):
            return
        try:
            if SW.next_biz(dt.date.fromisoformat(d["date"])) < self.open_t.date():
                return
        except (KeyError, ValueError):
            return
        Bot.split_picks(self, d)
        if not d["picks"]:
            return                                 # 시장 국면 때문에 오늘은 새로 안 사는 날 → 프리장 점검도 쉼
        cands = [x for x in d["picks"]]
        have = {str(x["code"]) for x in cands}
        base = c["picks_url"].format(m=self.m.lower()).rsplit("/", 1)[0]
        try:
            stats = requests.get(base + "/scores.json", timeout=30).json() or {}
            names = requests.get(base + "/names.json", timeout=30).json() or {}
        except Exception:
            stats, names = {}, {}
        pool = [k for k, v in stats.items() if k not in have and v.get("close") and not v.get("ld")
                and (v.get("c") or -99) >= pc.get("min_chart", 3)]
        codes = list(have) + pool
        try:
            px = {r.get("symbol"): f(r.get("lastPrice")) for r in (self.api.prices(codes) or [])}
        except Exception as e:
            self.log(f"🌅 프리장 시세 조회 실패: {e}")
            return
        prev = {str(x["code"]): float(x["close"]) for x in cands}
        prev.update({k: float(stats[k]["close"]) for k in pool})
        gap = {k: px[k] / prev[k] - 1 for k in codes if px.get(k) and prev.get(k)}
        if not gap or sum(1 for k in gap if abs(gap[k]) < 1e-9) >= len(gap) * 0.9:
            self.notify("🌅 프리장 점검: 프리장 가격이 아직 안 들어와요(지금 가격 = 어제 종가). "
                        "정규장 시작 15분 뒤 매수 직전에 지금 가격으로 후보 전체를 다시 점검해요.")
            return
        lo, hi = pc.get("add_min_gap", 2.0) / 100, pc.get("add_max_gap", 8.0) / 100
        movers = sorted((k for k in pool if k in gap and lo <= gap[k] <= hi),
                        key=lambda k: -(stats[k].get("c", 0) + gap[k] * 100))[: pc.get("max_add", 15)]
        add = []
        for k in movers:
            bars = Bot.daily_full(self, k)
            if len(bars) < 30:
                continue
            trs = [max(bars[i][2] - bars[i][3], abs(bars[i][2] - bars[i - 1][4]), abs(bars[i][3] - bars[i - 1][4]))
                   for i in range(len(bars) - 14, len(bars))]
            atr = sum(trs) / len(trs)
            cl = prev[k]
            add.append({"code": k, "name": names.get(k) or k, "setup": "fs", "close": round(cl, 4), "stop": round(cl - 2 * atr, 4),
                        "atr": round(atr, 4), "score": stats[k].get("c", 0), "ev": {}, "_pm_add": True,
                        "reason": f"🌅 프리장 {gap[k] * 100:+.1f}% · 밤 차트 점수 {stats[k].get('c', 0):+d} (후보 밖에서 새로 들어옴)"})
        allc = cands + add
        ec = c.get("earnings") or {}
        old_b = getattr(self, "broker", None)
        tmp = PaperBroker(self.m, self.cfg, self.dir, lambda *a: None, lambda *a: None)
        tmp.restore()
        try:
            self.broker = old_b or tmp
            ranked = Bot.rerank(self, allc, {k: v for k, v in px.items() if v}, self.open_t.date(), ec, dry=True)
        finally:
            self.broker = old_b
        ok = [x for x in ranked if x.get("_chk") and not x["_chk"][1][0]]
        bad = [x for x in ranked if x.get("_chk") and x["_chk"][1][0]]
        for x in ok:
            if x.get("_pm_add"):
                x["score"] = x.get("_fscore") or x["score"]          # 새로 들어온 종목은 프리장 점검 점수로
        self._pm_add = (key, [x for x in ok if x.get("_pm_add")])
        self._pm_rank = (key, [str(x["code"]) for x in ok])
        nm = lambda x: f"{x['name']}({gap.get(str(x['code']), 0) * 100:+.1f}%)"
        up = [x for x in cands if gap.get(str(x["code"]), 0) >= 0.03]
        dn = [x for x in cands if gap.get(str(x["code"]), 0) <= -0.03]
        when = "국장 넥스트레이드 프리장" if self.m == "KR" else "미장 프리마켓"
        msg = [f"🌅 프리장 점검 ({when} 가격 · 정규장 전) — 후보 {len(cands)}개 + 후보 밖 {len(pool)}개 확인"]
        if up:
            msg.append("📈 후보 중 프리장 크게 오름: " + ", ".join(nm(x) for x in up[:6]))
        if dn:
            msg.append("📉 후보 중 프리장 크게 빠짐: " + ", ".join(nm(x) for x in dn[:6]))
        if self._pm_add[1]:
            msg.append("🆕 후보 밖에서 새로 들어옴: " + ", ".join(f"{nm(x)} 점수 {x.get('_fscore', 0):+d}" for x in self._pm_add[1][:6]))
        msg.append("✅ 지금(프리장) 기준 점수 순: " + (", ".join(f"{x['name']}({x.get('_fscore', 0):+d})" for x in ok[:8]) or "통과 없음"))
        if bad:
            msg.append(f"❌ 프리장 기준 탈락 {len(bad)}개: " + ", ".join(f"{x['name']}({x['_chk'][1][0][:25]})" for x in bad[:5]))
        msg.append("→ 정규장 시작 15분 뒤 사기 직전에 지금 가격으로 한 번 더 점검하고 점수 높은 순으로 사요.")
        self.notify("\n".join(msg))

    def swing_preview(self):
        """장 시작 전 대기 중: 다음 장 후보(picks.json)가 새로 만들어졌으면 판단표를 한 번 미리 보냄.
        주문·계좌 변경 없음 (보유 종목은 저장된 상태 파일을 읽기만)."""
        if not self.swing_on() or os.getenv("TOSSBOT_OFFLINE") == "1":
            return
        key = self.open_t.date().isoformat()
        if getattr(self, "_pv_sent", "") == key:
            return
        c = self.cfg["daily_swing"]
        d = SW.load_picks(c["picks_url"], self.m, self.open_t.date(), lambda *a: None)
        if not d:
            return
        try:
            if SW.next_biz(dt.date.fromisoformat(d["date"])) < self.open_t.date():
                return                                       # 아직 지난 장 후보 → 새 후보 나올 때까지 기다림
        except (KeyError, ValueError):
            return
        Bot.split_picks(self, d)
        old_b, old_p = getattr(self, "broker", None), self.sw_picks
        tmp = PaperBroker(self.m, self.cfg, self.dir, lambda *a: None, lambda *a: None)
        tmp.restore()
        try:
            self.broker, self.sw_picks = tmp, d
            self.load_stopped()
            txt = self.swing_table_text()
            self._pv_table = {r[0]["code"]: r[1] for r in getattr(self, "sw_table", [])}
        finally:
            self.broker, self.sw_picks = old_b, old_p
        self._pv_sent = key
        when = self.open_t.strftime("%m/%d %H:%M")
        self.notify(f"📋 [미리 보기] 다음 장({when} {'한국' if self.m == 'KR' else '뉴욕'} 시간) 매수 판단표\n{txt}\n"
                    f"→ 장 시작 몇 분 전에 최종 판단표를 다시 보내요. 그사이 바뀌는 건 보유 종목 변화(손절 등)뿐이에요.")

    def any_regime(self) -> set:
        """시장 국면(지수 50일선 위/아래)과 상관없이 사는 방식. 시장별로 다르게 둘 수 있음
        (국장 fs 는 지수가 50일선 아래면 새로 안 삼 — tools/fsbt.py 백테스트 근거)."""
        c = self.cfg["daily_swing"]
        by = (c.get("any_regime_by_market") or {}).get(self.m)
        return set(by if by is not None else c.get("any_regime", []))

    def split_picks(self, d):
        """후보를 '오늘 시장 상태에서 쓰는 방식'(picks)과 '시장 상태 때문에 쉬는 방식'(_off)으로 나눔 — 판단표에 둘 다 보여 줌."""
        on, below = set(self.swing_on()), set(self.cfg["daily_swing"].get("below_setups", []))
        anyr = Bot.any_regime(self)
        allp = [x for x in d.get("picks", []) if x.get("setup") in on]
        bad = (self.cfg["daily_swing"].get("bad_regime_min_score") or {}).get(self.m) or {}
        keep = []
        for x in allp:
            st = x.get("setup")
            x.pop("_bad_min", None)
            if st in anyr or (st in below) != bool(d.get("regime")):
                keep.append(x)
            elif not d.get("regime") and st in bad:      # 시장 나쁨이어도 점수가 높으면 삼
                x["_bad_min"] = bad[st]
                keep.append(x)
        d["picks"] = keep
        d["_off"] = [x for x in allp if x not in d["picks"]]

    def swing_table_text(self) -> str:
        """장 전 판단표: 어제 종가로 매수 기준 ①~⑦을 그대로 적용한 결과. 당일엔 시가·장중 손절선·현금만 결과를 바꿀 수 있음."""
        d, c = self.sw_picks or {}, self.cfg["daily_swing"]
        try:
            Bot.swing_enter(self, None, dry=True)
        except Exception as e:
            self.log(f"🧭 판단표 오류: {e}")
        rows = getattr(self, "sw_table", [])
        when = {"open": "장 시작 1분 뒤", "close": "장 마감 15분 전"}
        ix = d.get("index") or {}
        out = [f"🧭 스윙 매수 판단표 ({d.get('date')} 종가 기준 · 시장 {'좋음' if d.get('regime') else '나쁨'} "
               f"{ix.get('close')} vs {ix.get('ma_n', 50)}일선 {ix.get('ma50')}) — 후보 {len(d.get('picks', []))}개"]
        for st, icon in (("매수", "✅ 매수 예정"), ("예비", "⏭ 예비 (예정 종목이 시가 조건에 걸리면 순서대로)")):
            g = [r for r in rows if r[1] == st]
            if g:
                out.append(icon)
                for x, _, why, q in g:
                    out.append(f"- {x['name']} ≈{q}주 × {money(self.m, x['close'])} · 손절 {money(self.m, x['stop'])} "
                               f"· {when.get(Bot.swing_timing(self, x, d), '')} · {x['reason']}" + (f" · {why}" if why else ""))
                    if x.get("_brief"):
                        out.append("   " + x["_brief"].replace("\n", "\n   "))
        if not d.get("picks"):
            out.append("✅ 매수 예정: 없음")
        off = d.get("_off") or []
        if off:
            nm = "코스피" if self.m == "KR" else "S&P500"
            mn = (d.get("index") or {}).get("ma_n", 50)
            why = (f"{nm}가 {mn}일선 아래(하락 국면)라 이 방식은 쉼" if not d.get("regime")
                   else f"{nm}가 {mn}일선 위(상승 국면)라 저점 매수는 쉼")
            out.append(f"⏸ 시장 상태 때문에 안 삼 ({why}): " + ", ".join(x["name"] for x in off[:10])
                       + (f" 외 {len(off) - 10}개" if len(off) > 10 else ""))
        g = [r for r in rows if r[1] == "탈락"]
        if g:
            out.append("❌ 안 삼")
            out += [f"- {x['name']}: {why}" for x, _, why, q in g[:12]]
            if len(g) > 12:
                out.append(f"- 그 밖 {len(g) - 12}개")
        out.append("이 표는 장 전 가격(프리장이 있으면 프리장) 기준이에요. 실제로 살 때 지금 가격으로 후보 전체를 다시 점검해서 점수 순으로 다시 줄 세워요.")
        out.append("당일엔 시가가 손절선 아래·10% 넘게 갭, (마감 매수는) 장중 손절선 닿음, 현금 부족일 때만 결과가 달라져요.")
        return "\n".join(out)

    def live_refresh(self, max_age: float = 5, after_open: bool = False) -> bool:
        """장 마감 약 2시간 전에 오늘 흐름(진행 중인 봉)으로 다시 고른 후보(live.json)가 있으면 그걸로 바꿔서 삼.
        시장 전체가 오늘 크게 빠지면(live_guard_index_pct) 오늘은 새로 안 삼."""
        import requests
        c = self.cfg["daily_swing"]
        url = c.get("live_picks_url")
        if not url or not self.sw_picks:
            return False
        try:
            d = requests.get(url.format(m=self.m.lower()), timeout=20).json()
        except Exception as e:
            self.log(f"🧭 장중 다시 고른 후보를 못 받음: {e}")
            return False
        today = now(self.m).date().isoformat()
        made = d.get("made") or ""
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(made)).total_seconds() / 3600
        except ValueError:
            age = 99
        if after_open:
            try:
                if dt.datetime.fromisoformat(made) < self.open_t.astimezone(dt.timezone.utc):
                    return False                       # 아직 오늘 장 시작 뒤에 다시 고른 게 아님 → 5분 뒤 다시 확인
            except (ValueError, TypeError):
                return False
        if d.get("date") != today or age > max_age or not d.get("picks"):
            if not after_open:
                self.log(f"🧭 장중 다시 고른 후보 없음(날짜 {d.get('date')}, {age:.1f}시간 전) — 밤 후보 그대로")
            return False
        ix = (d.get("index") or {}).get("today_pct")
        g = c.get("live_guard_index_pct")
        names = {x["code"]: x["name"] for x in d["picks"]}
        old = {x["code"]: x["name"] for x in self.sw_picks.get("picks", [])}
        add = [names[k] for k in d.get("added", []) if k in names][:6]
        drop = [old.get(k, k) for k in d.get("dropped", [])][:6]
        if g is not None and ix is not None and ix <= g:
            self.sw_picks = {**self.sw_picks, "picks": []}
            self.notify(f"🛑 오늘 {'코스피' if self.m == 'KR' else 'S&P500'}가 {ix:+.1f}% 빠지고 있어서 새로 안 사요 (시장 전체 흐름이 나쁜 날)")
            return True
        keep = [x for x in self.sw_picks.get("picks", []) if x.get("setup") not in (None, "fs")]   # 신고가·재료 갭·반등 후보는 밤 신호 그대로 유지
        self.sw_picks = {**self.sw_picks, "picks": d["picks"] + keep, "live": True}
        self.notify("🔄 오늘 흐름으로 후보를 다시 골랐어요" + (f" · 새로: {', '.join(add)}" if add else "")
                    + (f" · 빠짐: {', '.join(drop)}" if drop else "") + (f" · 지수 오늘 {ix:+.1f}%" if ix is not None else ""))
        return True

    def ai_check(self, x, price) -> dict:
        """매수 직전 AI 판독 (신고가·반등·재료 갭처럼 밤에 AI 가 안 읽은 후보용).
        보는 것: 신호 이유, 지금 가격, 현재·미래 점수 근거(차트·거래량·추세), 애널리스트 목표가·의견·이익 전망, 실적일·서프라이즈,
        (국장) 외국인·기관 수급, 최근 기사 제목. → {"view","catalyst","risk","adj","veto"?}. 키가 없거나 실패하면 {}."""
        from . import news_judge as NJ
        if os.getenv("TOSSBOT_OFFLINE") == "1" or not os.getenv("ANTHROPIC_API_KEY", "").strip():
            return {}
        ev = x.get("ev") or {}
        try:
            est = Bot.est_row(self, x["code"]) or {}
        except Exception:
            est = {}
        try:
            news = Bot.stock_news(self, x["code"], x["name"])
        except Exception:
            news = []
        kind = {"hi52": "52주 신고가 돌파", "rb": "급락 뒤 반등", "ep": "큰 재료 갭", "dip": "저점 매수"}.get(x.get("setup"), x.get("setup"))
        facts = {"종목": x.get("name"), "시장": self.m, "사는 방식": kind, "신호 이유": x.get("reason"), "신호 날 종가": x.get("close"),
                 "지금 가격": price, "손절선": x.get("stop"), "현재·미래 점수": x.get("_fscore"), "점수 근거": (x.get("_fwhy") or [])[:14],
                 "애널리스트·이익 전망": {k: v for k, v in est.items() if v not in (None, "")} if isinstance(est, dict) else est,
                 "실적·공시": {k: v for k, v in ev.items() if k not in ("ai", "news", "est")}, "최근 기사 제목": news[:8]}
        prompt = ("너는 15년 차 주식 애널리스트다. 이 종목은 '" + str(kind) + "' 신호로 오늘 마감 전에 사려는 후보다. 아래 자료만 보고 "
                  "앞으로 1~3주 들고 갈 만한지 판단해라. 규칙: 1) 자료에 없는 사실은 지어내지 마라. 2) 시장 전체 얘기는 근거로 쓰지 마라. "
                  "3) 확실한 악재(주식 희석·회계 문제·실적 쇼크·가이던스 대폭 하향·대형 소송·규제 제재·대주주 대량 매도)가 있으면 veto 에 이유를 적어라. "
                  "4) 급락 뒤 반등이면 '왜 급락했는지'가 아직 해결 안 된 악재인지 꼭 봐라. 5) 쉬운 말. "
                  'JSON 한 줄만 답해라: {"view":"긍정|중립|부정","catalyst":"움직임의 이유 한 문장(모르면 \'확인 안 됨\')",'
                  '"risk":"가장 큰 위험 한 문장","adj":-2~1 정수,"veto":"사면 안 되는 이유 또는 빈 문자열"}\n\n'
                  + json.dumps(facts, ensure_ascii=False, default=str)[:7000])
        model = os.getenv("CLAUDE_BUY_MODEL", "").strip() or "claude-opus-5-5"
        d = NJ._call(prompt, model)
        if not isinstance(d, dict) or not d.get("view"):
            return {}
        v = str(d.get("view"))
        v = "긍정" if "긍정" in v else "부정" if "부정" in v else "중립"
        out = {"view": v, "catalyst": str(d.get("catalyst", ""))[:200], "risk": str(d.get("risk", ""))[:200], "adj": 0, "model": model, "at_buy": True}
        try:
            out["adj"] = max(-2, min(1, int(d.get("adj", 0))))
        except (TypeError, ValueError):
            pass
        if str(d.get("veto") or "").strip():
            out["veto"] = str(d["veto"])[:120]
        return out

    def news_judged(self) -> dict:
        """1시간마다 AI 가 다시 읽은 종목 뉴스 판정 (6시간 넘은 건 안 씀). 10분 동안 기억."""
        import requests
        from . import news_judge as NJ
        c = self.cfg["daily_swing"]
        url = c.get("news_judge_url")
        if not url or os.getenv("TOSSBOT_OFFLINE") == "1":
            return {}
        hit = getattr(self, "_nj", None)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        try:
            d = NJ.fresh(requests.get(url.format(m=self.m.lower()), timeout=15).json(), c.get("news_judge_hours", 6))
        except Exception:
            d = hit[1] if hit else {}
        self._nj = (time.time(), d)
        return d

    def manual_buy(self, x, cash) -> float:
        """휴대폰 앱에서 고른 종목을 스윙 규칙 그대로(손절선·손실 한도·업종·열린 손실·점수·AI·공시·뉴스 점검) 사기.
        점검에 걸리면 이유를 알림으로 보내고 안 삼."""
        old, old_t = self.sw_picks, getattr(self, "sw_table", [])
        d = {k: v for k, v in (old or {}).items() if k not in ("picks", "_off")}
        d.setdefault("regime", True)
        d["picks"] = [dict(x, _manual=True)]
        self.sw_picks = d
        try:
            spent = Bot.swing_enter(self, cash, "open", manual=True)
        finally:
            self.sw_picks, self.sw_table = old, old_t
        if not spent:
            self.log(f"🧭 {x['name']} 봇 매수 요청 처리 끝 (안 삼 — 이유는 위 알림)")
        return spent

    def swing_enter(self, cash, when: str = "open", dry: bool = False, manual: bool = False) -> float:
        """when: open(장 시작 직후) / close(장 마감 15분 전, 그날 흐름을 보고) — 방식별로 config entry_at.
        dry: 주문 없이 어제 종가로 같은 판단을 해서 판단표(self.sw_table)만 만듦 — 장 전에 미리 알리는 용도."""
        d, c = self.sw_picks or {}, self.cfg["daily_swing"]
        table = self.sw_table = []

        why_map, bought = {}, set()

        def skip(x, why, loud=False):
            why_map.setdefault(x.get("code"), why)
            if dry:
                table.append((x, "탈락", why, 0))
            elif loud or x.get("_manual"):
                self.notify(f"🧭 {x['name']} 건너뜀 — {why}")
            else:
                self.log(f"🧭 {x['name']} 안 사요 — {why}")
        if not d.get("picks"):
            return 0.0
        if self.risk.kill_switch() or self.risk.halted:
            self.log(f"🧭 스윙 매수 건너뜀: {self.risk.halted or '긴급 정지'}")
            return 0.0
        if not manual and (load_settings().get("nobuy") or {}).get(self.m) == now(self.m).date().isoformat():
            self.notify("🛑 오늘은 새로 사지 않기로 해 둬서 매수 안 했어요 (보유 종목 손절·관리는 그대로)")
            return 0.0
        exd = (load_settings().get("exclude") or {}).get(self.m) or {}
        if not manual and exd.get("date") == now(self.m).date().isoformat() and exd.get("codes"):
            d = {**d, "picks": [x for x in d["picks"] if str(x.get("code")) not in set(exd["codes"])]}
            self.log(f"🧭 오늘 뺀 종목은 안 사요: {', '.join(exd['codes'])}")
        have = [p for p in self.broker.pos.values() if p.extra.get("sw") and p.state in ("pending", "open", "closing")]
        extra = (c.get("extra_slots") or {}).get(self.m) or {}     # 따로 자리를 주는 방식 (예: 미장 신고가 3·반등 3) — 기본 자리와 별개
        slots = (c.get("max_pos_by_market") or {}).get(self.m, c.get("max_pos", 6)) - sum(1 for p_ in have if p_.extra.get("sw_kind") not in extra)
        ex_room = sum(max(0, n_ - sum(1 for p_ in have if p_.extra.get("sw_kind") == k_)) for k_, n_ in extra.items())
        below = set(c.get("below_setups", []))
        ec = c.get("earnings") or {}
        today = now(self.m).date()
        cands = []
        for x in d["picks"]:
            if x.get("setup") not in Bot.any_regime(self) and (x.get("setup") in below) == bool(d.get("regime")) and x.get("_bad_min") is None:
                continue
            if x["code"] in self.broker.pos:
                skip(x, "이미 보유 중")
            elif x["code"] in self.stopped:
                skip(x, "최근 손절한 종목 (며칠 재매수 금지)")
            elif x["code"] in getattr(self, "recent_sold", {}) and not manual:
                skip(x, f"최근({self.recent_sold[x['code']]})에 판 종목 — {self.cfg.get('reentry_block', {}).get('any_days', 3)}일은 다시 안 삼 (사고팔기 반복 방지)")
            else:
                cands.append(x)
        later = [] if dry or manual else [x for x in cands if Bot.swing_timing(self, x, d) != when]
        cands = cands if dry or manual else [x for x in cands if Bot.swing_timing(self, x, d) == when
                                             or (when == "close" and Bot.swing_timing(self, x, d) == "open")]   # 아침에 못 산 확신 종목은 마감에 다시 봄
        if when == "open" and later and slots > 0:
            self.notify(f"🧭 스윙 후보 {len(later)}개({', '.join(x['name'] for x in later[:5])})는 장 시작 직후 말고 "
                        f"장 마감 15분 전에 오늘 흐름을 보고 살게요. 장중에 손절선까지 밀리면 안 사요.")
        # 실적이 보유 기간 안에 없는 종목 먼저 (점수 순서는 유지)
        cands.sort(key=lambda x: SW.earn_plan(x, today, 100.0, 90.0, ec, self.m)[0] not in ("ok", "early"))   # 판정 좋은 실적주는 순서 그대로
        rerank = c.get("rerank", True) and not manual
        if not rerank:
            cands = cands if dry else cands[: max(0, slots) * 3]
        sec_n = {}
        for p0 in have:
            if p0.extra.get("sector"):
                sec_n[p0.extra["sector"]] = sec_n.get(p0.extra["sector"], 0) + 1
        if manual and slots <= 0 and cands:
            self.notify(f"🧭 {cands[0]['name']} 봇 매수 요청 — 스윙 동시 보유 한도({c.get('max_pos', 6)}종목)가 꽉 차서 안 샀어요")
        rot_on = bool((c.get("rotation") or {}).get("enabled")) and not manual
        log_on = not dry and not manual
        log_c = [cands]

        def done(ret, pxm=None):
            if log_on:
                try:
                    Bot.log_missed(self, log_c[0], why_map, bought, pxm or {}, slots_full=slots <= 0 and ex_room <= 0)
                except Exception as ex_:
                    self.log(f"📒 놓친 후보 기록 오류: {ex_}")
            return ret
        if (slots <= 0 and ex_room <= 0 and not dry and not rot_on) or not cands:   # 자리가 꽉 차도 교체 판단은 해야 함
            return done(0.0)
        if dry:
            px, cash = {x["code"]: float(x["close"]) for x in cands}, None
            if rerank and os.getenv("TOSSBOT_OFFLINE") != "1" and getattr(self, "api", None):
                try:                              # 장 전: 프리장(국장 NXT·미장 프리마켓) 가격이 있으면 그걸로 미리 판단
                    for r in self.api.prices([x["code"] for x in cands]) or []:
                        v = f(r.get("lastPrice"))
                        if v and r.get("symbol") in px:
                            px[r["symbol"]] = v
                except Exception:
                    pass
        else:
            try:
                px = {x.get("symbol"): f(x.get("lastPrice")) for x in (self.api.prices([x["code"] for x in cands]) or [])}
            except TossError as e:
                self.log(f"🧭 스윙 시세 조회 실패: {e}")
                return done(0.0)
        cap = self.risk.capital or 0
        spent = 0.0
        alt = 0
        for x in cands:                   # 시장별 손절선 (국장 3×ATR · 미장 사는 값 −5% 등) — 수량·점검 모두 이 손절선으로
            x.setdefault("_stop_orig", x["stop"])
            x["stop"] = round(SW.stop_for(c, self.m, x, px.get(x["code"])), 4)
        planned_r = 0.0                   # 판단표(dry)에서 앞서 '매수 예정'으로 잡은 종목들의 손실 한도 합
        mgap = None                      # 기록용: 지수(SPY) 개장 갭 — 1월 점검 때 '지수 급등 출발 날 신고가 매수 건너뛰기' 검증
        if not dry and self.m == "US" and (d.get("index") or {}).get("close"):
            try:
                sp = f(((self.api.prices(["SPY"]) or [{}])[0]).get("lastPrice"))
                mgap = round(sp / float(d["index"]["close"]) - 1, 4) if sp else None
            except Exception:
                mgap = None
        caps = (c.get("setup_slots") or {}).get(self.m) or {}     # 방식별 최대 보유 수 (예: 급락 뒤 반등은 3종목까지) — 그 자리만큼은 먼저 채움
        held_by = {}
        for p_ in have:
            held_by[p_.extra.get("sw_kind")] = held_by.get(p_.extra.get("sw_kind"), 0) + 1
        if rerank:
            cands = Bot.rerank(self, cands, px, today, ec, dry)
            if caps or extra:
                cands = [x for x in cands if x.get("setup") in caps or x.get("setup") in extra] + \
                        [x for x in cands if x.get("setup") not in caps and x.get("setup") not in extra]
            log_c[0] = cands
            if not dry:
                cands = cands[: max(0, slots) * 3 + 3 + sum(caps.values()) + sum(extra.values()) * 2]
        if rerank and not dry and slots <= 0:
            try:
                if Bot.rotate(self, cands, have, extra, px, today):
                    slots += 1
            except Exception as ex_:
                self.log(f"🔁 교체 판단 오류: {ex_}")
        mn = None if manual else (c.get("max_new_by_market") or {}).get(self.m)    # 하루 새로 사는 종목 수 한도 (자리를 남겨 다음 날 좋은 후보도 살 수 있게)
        nd = getattr(self, "_new_day", (None, 0))
        newn = nd[1] if (nd[0] == today.isoformat() and not dry) else 0
        for x in cands:
            is_ex = x.get("setup") in extra
            if mn and newn >= mn:
                skip(x, f"오늘 새로 사는 건 최대 {mn}종목까지 — 자리를 남겨 다음 날 좋은 후보도 살 수 있게")
                continue
            room_ex = lambda: sum(max(0, n_ - held_by.get(k_, 0)) for k_, n_ in extra.items())
            if slots <= 0 and room_ex() <= 0 and not dry:
                break
            if not is_ex and slots <= 0 and not dry:
                continue
            sym, e, stop = x["code"], px.get(x["code"]), float(x["stop"])
            if not e:
                continue
            if is_ex and held_by.get(x["setup"], 0) >= extra[x["setup"]]:
                skip(x, f"이 방식은 따로 {extra[x['setup']]}종목까지")
                continue
            if not is_ex and x.get("setup") in caps and held_by.get(x["setup"], 0) >= caps[x["setup"]]:
                skip(x, f"이 방식은 최대 {caps[x['setup']]}종목까지")
                continue
            if stop >= e * 0.995:
                skip(x, f"손절선({money(self.m, stop)}) 근처·아래로 열림")
                continue
            if when == "close" and not dry:
                try:
                    lo = min(r[2] for r in self.swing_bars(sym, e)[-1:])
                except Exception:
                    lo = e
                if lo <= stop:
                    self.notify(f"🧭 {x['name']} 오늘 장중 손절선({money(self.m, stop)})까지 밀려서 안 사요 (저가 {money(self.m, lo)})")
                    continue
            if e > x["close"] * (1 + c.get("max_gap_pct", 10) / 100):
                skip(x, f"어제 종가보다 {(e / x['close'] - 1) * 100:.0f}% 넘게 올라 열려서 안 쫓아감")
                continue
            rg = (c.get("max_rel_gap_by_setup") or {}).get(x.get("setup"))
            if rg and not dry and mgap is not None and (e / x["close"] - 1) - mgap >= rg / 100:
                skip(x, f"밤사이 시장보다 {((e / x['close'] - 1) - mgap) * 100:.1f}%p 더 뛰어 열림 (재료로 급등한 갭은 안 쫓아감, 기준 {rg:g}%p)", loud=True)
                continue
            eo = c.get("early_conf") or {}
            if when == "open" and not dry and eo.get("enabled") and Bot.early_ok(self, x) and e > x["close"] * (1 + eo.get("max_up_pct", 7) / 100):
                skip(x, f"확신 종목이지만 벌써 {(e / x['close'] - 1) * 100:+.1f}% 올라 있어 추격 안 함 (기준 +{eo.get('max_up_pct', 7):g}%) — 마감 무렵 다시 봄", loud=True)
                continue
            if c.get("red_flag_news", True) and not getattr(self, "_offline", False):
                try:
                    from .news import red_flags, sec_flags
                    if c.get("sec_user_agent"):
                        os.environ.setdefault("SEC_UA", c["sec_user_agent"])
                    rf = (sec_flags(sym) if self.m == "US" else []) + red_flags(x["name"], self.m)
                except Exception:
                    rf = []
                if rf:
                    skip(x, "최근 2일 치명적 악재 뉴스: " + " / ".join(rf[:2]), loud=True)
                    continue
            try:
                info = (self.api.stocks([sym]) or [{}])[0]
                if LEV.search(info.get("name") or "") or str(info.get("securityType") or "STOCK") not in ("STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPT"):
                    skip(x, "레버리지·인버스·ETF 등 일반 주식 아님")
                    continue
                if any(w.get("warningType") in BAD_WARNINGS and not w.get("endDate") for w in (self.api.warnings(sym) or [])):
                    skip(x, "투자경고·거래정지 등 경고")
                    continue
            except TossError:
                pass
            ev = x.get("ev") or {}
            sec = ev.get("sector")
            if sec and ec.get("sector_max") and sec_n.get(sec, 0) >= ec["sector_max"]:
                skip(x, f"같은 업종({sec}) 이미 {sec_n[sec]}종목 (한 업종 최대 {ec['sector_max']})")
                continue
            plan, per, etxt = SW.earn_plan(x, today, e, stop, ec, self.m)
            if plan == "skip":
                skip(x, etxt, loud=True)
                continue
            size_stop = e - per
            rp = c.get("risk_pct", 0.5)
            if str(d.get("date", "")) >= str(c.get("risk_by_setup_since", "9999")):
                rp = (c.get("risk_by_setup") or {}).get(x.get("setup"), rp)      # 저점 매수 1.5% (보유 종목이 적고 낙폭이 낮음)
            q = SW.qty_for(cap, rp, c.get("cap_pct", 20), e, size_stop, None if cash is None else cash - spent)
            if q < 1 and plan in ("hold", "early"):
                skip(x, f"{etxt}: 실적 갭이 커서 1주도 손실 한도를 넘음", loud=True)
                continue
            if q < 1:
                full = SW.qty_for(cap, rp, c.get("cap_pct", 20), e, size_stop, None)
                if full >= 1 and cash is not None:
                    self.notify(f"💸 🧭 {x['name']} 계획은 {full}주인데 계좌 매수 가능 금액({money(self.m, max(0, cash - spent))})이 "
                                f"모자라 절반도 못 사서 건너뛰었어요. 현금을 채우거나 '투자금' 설정을 실제 현금에 맞춰 주세요.")
                else:
                    skip(x, f"수량 0주 (1주 손절폭 {money(self.m, e - size_stop)}가 손실 한도보다 큼)")
                continue
            try:
                ck = x.pop("_chk", None)
                bad, btxt, warn = ck[1] if ck and ck[0] == e else Bot.cand_check(self, x, e)
            except Exception as ex:
                bad, btxt, warn = None, "", []
                self.log(f"🧭 {x['name']} 종합 점검 오류: {ex}")
            if bad:
                skip(x, bad, loud=True)
                continue
            if not dry and not (ev.get("ai") or {}).get("view") and (x.get("setup") or "fs") not in set(c.get("strict_setups") or ["fs"]):
                try:                                    # 신고가·반등·재료 갭: 차트 신호만으로 사지 않게 — 사기 직전에 AI 가 전체 정보를 읽고 판정
                    a_ = Bot.ai_check(self, x, e)
                    if a_:
                        ev = x.setdefault("ev", {})
                        ev["ai"] = a_
                        self.log(f"🤖 {x['name']} AI 판독 {a_.get('view')}: {a_.get('catalyst', '')[:60]} / 위험: {a_.get('risk', '')[:60]}")
                    elif c.get("ai_required_other", True) and os.getenv("TOSSBOT_OFFLINE") != "1":
                        skip(x, "AI 판독을 못 받아서 안 사요 (차트 신호만으로는 안 삼)", loud=True)
                        continue
                except Exception as ex:
                    self.log(f"🤖 {x['name']} AI 판독 오류: {ex}")
            mrr = c.get("min_rr")
            if mrr and x.get("_strict", True) and x.get("_rr") is not None and x["_rr"] < mrr:
                skip(x, f"손익비 {x['_rr']:.1f} (기준 {mrr:g} 이상) — 목표가까지 남은 폭이 손절 폭에 비해 작아서 안 사요", loud=True)
                continue
            veto = (ev.get("ai") or {}).get("veto")
            if veto:
                skip(x, f"AI 최종 판독 매수 금지: {veto}", loud=True)
                continue
            aiv = (ev.get("ai") or {}).get("view")
            if aiv == "부정":
                skip(x, "AI 판독이 부정적이라 안 사요 (점수가 높아도 위험 쪽이 더 크다고 봄)", loud=True)
                continue
            dfl = ev.get("dart_flags")
            if dfl:
                skip(x, "최근 공시 위험: " + " · ".join(dfl), loud=True)
                continue
            # 점수 등급별 크기: 근거가 강할수록(A) 더, 애매하면(B) 덜
            rg = c.get("risk_by_grade") or {}
            if x.get("_fscore") is not None and rg:
                g = SW.grade_of(x["_fscore"])
                if g in rg and rg[g] != rp:
                    rp = rg[g]
                    q = SW.qty_for(cap, rp, c.get("cap_pct", 20), e, size_stop, None if cash is None else cash - spent)
                    if q < 1:
                        skip(x, f"등급 {g} 손실 한도 {rp:g}%로는 1주도 못 삼")
                        continue
            strong = c.get("strong") or {}
            fsx = x.get("_fscore")
            is_strong = (strong.get("enabled") and aiv == "긍정" and not (ev.get("ai") or {}).get("veto")
                         and (fsx is None or fsx >= strong.get("min_score", 8))
                         and (not x.get("_strict", True) or x.get("_rr") is None or x["_rr"] >= strong.get("min_rr", 2.5)))
            if is_strong:                                  # 근거가 확실한 종목(AI 긍정 + 점수 높음 + 손익비 충분)은 더 크게
                rp = strong.get("risk_pct", 2.0)
                q = SW.qty_for(cap, rp, strong.get("cap_pct", 30), e, size_stop, None if cash is None else cash - spent)
                x["reason"] = "💪 근거가 확실해서 크게 · " + x["reason"]
            if aiv == "중립" and rg.get("B") is not None and rp > rg["B"]:
                rp = rg["B"]                               # AI 가 애매하다고 보면 작게만
                q = SW.qty_for(cap, rp, c.get("cap_pct", 20), e, size_stop, None if cash is None else cash - spent)
                if q < 1:
                    skip(x, "AI 판독 중립이라 작게만 사려는데 1주도 안 됨")
                    continue
            x["_rp"] = rp
            # 이미 가진 종목과 너무 똑같이 움직이면(60일 수익률 상관 > 기준) 위험이 겹침
            cm = c.get("max_corr")
            if cm:
                try:
                    hit = Bot.corr_hit(self, sym, cm)
                except Exception as ex:
                    hit = None
                    self.log(f"🧭 {x['name']} 상관 점검 오류: {ex}")
                if hit:
                    skip(x, f"보유 중인 {hit[0]}와 움직임이 거의 같음(상관 {hit[1]:.2f}) — 위험이 겹쳐서 건너뜀", loud=True)
                    continue
            mr = (c.get("max_open_risk_by_market") or {}).get(self.m, c.get("max_open_risk_pct"))
            if mr and is_ex:
                mr += (c.get("extra_open_risk_by_market") or {}).get(self.m, 0)     # 따로 자리 몫의 손실 한도
            if mr and cap:
                open_r = sum(max(0.0, pp.entry - pp.sl) * pp.qty for pp in self.broker.pos.values()
                             if pp.extra.get("sw") and pp.state in ("pending", "open")) + planned_r
                if open_r + (e - round_tick(self.m, stop, "down")) * q > cap * mr / 100:
                    skip(x, f"스윙 전체 열린 손실(손절선까지)이 투자금의 {mr:g}%를 넘게 됨 (지금 {money(self.m, open_r)})", loud=not dry)
                    continue
            x["_brief"] = btxt + (("\n⚠️ " + " · ".join(warn)) if warn else "")
            if dry:
                if (is_ex and held_by.get(x["setup"], 0) < extra[x["setup"]]) or (not is_ex and slots > 0):
                    planned_r += (e - round_tick(self.m, stop, "down")) * q
                    table.append((x, "매수", etxt, q))
                    newn += 1
                    if not is_ex:
                        slots -= 1
                    held_by[x.get("setup")] = held_by.get(x.get("setup"), 0) + 1
                    if sec:
                        sec_n[sec] = sec_n.get(sec, 0) + 1
                elif alt < 3:
                    table.append((x, "예비", etxt, q))
                    alt += 1
                continue
            sl = round_tick(self.m, stop, "down")
            p = self.broker.enter(sym, x["name"], e, q, round_tick(self.m, e * 3, "up"), sl,
                                  f"🧭 스윙 · {x['reason']}", r=e - sl, atr=x.get("atr"))
            if not p:
                continue
            base = dt.datetime.now(TZ["KR"]).date() if self.m == "US" else now(self.m).date()
            p.extra.update(sw=True, carry=True, setup="sw_" + x["setup"], sw_kind=x["setup"], sw_stop0=sl, sw_hi=e, sw_days=0,
                           expire=(base + dt.timedelta(days=c.get("expire_days", 20))).isoformat(),
                           sector=sec, earn_react=ev.get("react"), earn_gap=ev.get("gap_avg"), earn_hold=plan in ("hold", "early"),
                           mkt_gap=mgap, entry_when=when, risk_pct=x.get("_rp"), tgt=x.get("_tgt"), fscore=x.get("_fscore"), fwhy=(x.get("_fwhy") or [])[:10],
                           grade=SW.grade_of(x["_fscore"]) if x.get("_fscore") is not None else None,
                           fac=Bot.entry_factors(x, ev))           # 살 때 본 모든 정보(애널리스트·실적 전망·AI·수급) → 실전 기록 점검용
            try:                                               # 산 직후부터 앱에 로봇 판정을 보여 줌 (옛 '나쁨' 표시 대신)
                Bot.set_review(self, p, x.get("_fscore"), x.get("_fwhy") or [], 0.0)
            except Exception:
                pass
            if when == "close":
                p.extra["carry_done"] = today.isoformat()      # 방금 샀으니 오늘 일봉 점검·정리 대상 아님
            if sec:
                sec_n[sec] = sec_n.get(sec, 0) + 1
            st = self.syms.setdefault(sym, SymState(sym))
            st.name = x["name"]
            if self.stream:
                self.stream.set_codes(list(dict.fromkeys(list(self.broker.pos) + self.universe)))
            self.broker.save_state()
            spent += e * q
            bought.add(sym)
            newn += 1
            self._new_day = (today.isoformat(), newn)
            if not is_ex:
                slots -= 1
            held_by[x.get("setup")] = held_by.get(x.get("setup"), 0) + 1
            self.notify(f"🧭 스윙 매수 {x['name']} {q}주 ≈ {money(self.m, e)} · 손절 {money(self.m, sl)} "
                        f"(-{(1 - sl / e) * 100:.1f}%, 손실 한도 투자금의 {rp:g}%)\n산 이유: {x['reason']}"
                        + SW.ev_text(ev, etxt) + (f"\n{x['_brief']}" if x.get("_brief") else ""))
        return done(spent, px)

    def log_missed(self, cands, why_map, bought, px, slots_full=False):
        """매수 때 본 후보 전부와 안 산 이유를 기록 (logs/missed-시장.json, 최근 10일).
        지난 기록은 지금 가격으로 '그 뒤 얼마나 움직였나'를 채움 → 안 산 판단이 맞았는지 앱·알림으로 확인."""
        if not getattr(self, "dir", None):
            return
        f_ = Path(self.dir) / "missed.json"
        try:
            hist = json.loads(f_.read_text(encoding="utf-8"))
        except Exception:
            hist = []
        today = now(self.m).date().isoformat()
        rows = []
        for i, x in enumerate(cands[:30]):
            code = x.get("code")
            if code in bought:
                st, why = "매수", ""
            else:
                st = "안 삼"
                why = why_map.get(code) or ((x.get("_chk") or (None, (None,)))[1][0] if x.get("_chk") else None) \
                    or ("자리 꽉 참 (보유 종목이 한도까지 참)" if slots_full else "순서가 밀림 (앞 순위 종목으로 자리·한도가 참)")
            rows.append({"rank": i + 1, "code": code, "name": x.get("name"), "setup": x.get("setup") or "fs",
                         "score": x.get("_fscore", x.get("score")), "ai": ((x.get("ev") or {}).get("ai") or {}).get("view"),
                         "px": px.get(code) or x.get("close"), "st": st, "why": str(why)[:120]})
        hist = [h for h in hist if h.get("date") != today and h.get("date", "") >= (now(self.m).date() - dt.timedelta(days=14)).isoformat()]
        old = [r for h in hist for r in h.get("rows", []) if r.get("px")]
        if old and getattr(self, "api", None):
            try:
                cur = {r_.get("symbol"): f(r_.get("lastPrice")) for r_ in (self.api.prices(list({r["code"] for r in old})[:100]) or [])}
                for r in old:
                    if cur.get(r["code"]):
                        r["now"] = cur[r["code"]]
                        r["chg"] = round((cur[r["code"]] / float(r["px"]) - 1) * 100, 1)
            except Exception:
                pass
        hist.append({"date": today, "rows": rows})
        hist = hist[-10:]
        f_.parent.mkdir(parents=True, exist_ok=True)
        f_.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
        if len(hist) >= 2:                                    # 지난 매수일 후보들이 그 뒤 어떻게 됐는지 한 줄 알림 (산 것·안 산 것 모두)
            prev = [r for r in hist[-2]["rows"] if r.get("chg") is not None]
            if prev:
                no = sorted([r for r in prev if r["st"] != "매수"], key=lambda r: -r["chg"])
                yes = [r for r in prev if r["st"] == "매수"]
                t = lambda r: f"{r['name']} {r['chg']:+.1f}%"
                msg = f"📒 지난 매수일({hist[-2]['date']}) 후보 그 뒤: 산 것 " + (", ".join(t(r) for r in yes) or "없음")
                if no:
                    msg += " / 안 산 것 중 많이 오른 " + ", ".join(f"{t(r)}({r['why'][:20]})" for r in no[:3] if r["chg"] > 0) \
                           + " / 많이 내린 " + ", ".join(t(r) for r in no[::-1][:2] if r["chg"] < 0)
                self.notify(msg)

    def rerank(self, cands, px, today, ec, dry=False) -> list:
        """매수 직전 후보 전체 다시 점검: 지금 가격(프리장·장중)으로 모든 후보의 점수·차트·뉴스를 다시 계산해서
        통과한 것 중 점수 높은 순으로 다시 줄 세움 (어젯밤 순서 그대로 사지 않음). 실적 판정 좋은 종목 우선은 유지."""
        c = self.cfg["daily_swing"]
        n_max = c.get("rerank_max", 30)
        good, out, bad_n = [], [], []
        for x in cands:
            x.pop("_chk", None)
        from concurrent.futures import ThreadPoolExecutor
        try:
            Bot.est_row(self, "")                 # 증권사 자료를 먼저 한 번 받아 둠 (동시에 여러 번 받지 않게)
        except Exception:
            pass
        todo = [(i, x) for i, x in enumerate(cands) if px.get(x["code"]) and i < n_max]

        def one(ix):
            try:
                return Bot.cand_check(self, ix[1], px[ix[1]["code"]])
            except Exception as ex:
                self.log(f"🧭 {ix[1]['name']} 다시 점검 오류: {ex}")
                return None
        with ThreadPoolExecutor(max_workers=c.get("rerank_workers", 4)) as pool:   # 여러 종목을 동시에 점검 → 몇 초 안에 끝남
            res0 = dict(zip([i for i, _ in todo], pool.map(one, todo)))
        for i, x in enumerate(cands):
            e = px.get(x["code"])
            chk = res0.get(i)
            if chk is None:
                out.append((1, 0, i, x))
                continue
            x["_chk"] = (e, chk)                 # 같은 가격일 때만 다시 씀 (다음 매수 때는 새로 점검)
            if chk[0]:
                bad_n.append(x["name"])
                out.append((2, 0, i, x))
                continue
            ep = SW.earn_plan(x, today, 100.0, 90.0, ec, self.m)[0] not in ("ok", "early")
            good.append(x)
            out.append((0, (ep, -(x.get("_fscore") or 0)), i, x))
        out.sort(key=lambda t: (t[0], t[1], t[2]))
        res = [t[3] for t in out]
        if not dry and good:
            top = [x for x in res if x in good][:5]
            self.notify(f"🔄 지금 가격으로 후보 {min(len(cands), n_max)}개 다시 점검 → 통과 {len(good)}개, 점수 순: "
                     + ", ".join(f"{x['name']}({x.get('_fscore', 0):+d})" for x in top)
                     + (f" · 탈락 {len(bad_n)}개" if bad_n else ""))
        return res

    def swing_bars(self, sym, last):
        """토스 일봉 + 오늘(지금 가격을 종가로)."""
        today = now(self.m).date()
        rows = []
        for cd in self.api.candles(sym, "1d", 40) or []:
            t = parse_ts(cd.get("timestamp"), self.m)
            cl = f(cd.get("closePrice"))
            if not t or not cl:
                continue
            rows.append((t.date(), f(cd.get("highPrice")) or cl, f(cd.get("lowPrice")) or cl, cl))
        rows.sort(key=lambda r: r[0])
        old = [r for r in rows if r[0] < today]
        cur = [r for r in rows if r[0] == today]
        if last:
            h = max([last] + [r[1] for r in cur])
            l = min([last] + [r[2] for r in cur])
            old.append((today, h, l, last))
        elif cur:
            old.append(cur[-1])
        return old

    def swing_daily(self, p, last):
        """장 마감 15분 전 일봉 점검: 손절선 올리기 / 추세 꺾이면 정리."""
        c = dict(self.cfg["daily_swing"])
        c.update((c.get("exit_by_setup") or {}).get(p.extra.get("sw_kind"), {}))   # 저점 매수는 짧게 (반등 먹고 나옴)
        bars = self.swing_bars(p.symbol, last)
        if len(bars) < 12:
            self.log(f"🧭 {p.name} 일봉이 부족해 오늘 점검은 건너뜀")
            return
        held = int(p.extra.get("sw_days", 0))
        if Bot.earn_presell(self, p, bars[-1][3]):
            return
        fee = self.cfg["fees"][self.m]
        cost = (fee["buy_pct"] + fee["sell_pct"] + fee["sell_tax_pct"]) / 100
        new, hi, why, note = SW.daily_check(p.entry, p.sl, p.extra.get("sw_stop0") or p.sl, p.extra.get("sw_hi"), held, bars, c, cost)
        p.extra["sw_hi"] = hi
        p.extra["sw_days"] = held + 1
        close = bars[-1][3]
        stm = (c.get("stop_by_market") or {}).get(self.m) or {}
        if stm.get("all") and stm.get("pct") and p.entry:          # 손절선 고정(예: 산 값 -5%) — 예전에 넓게 잡은 보유 종목도 맞춤 (더 높은 손절선은 그대로)
            fx = round_tick(self.m, p.entry * (1 - stm["pct"] / 100), "down")
            if fx > max(new, p.sl):
                new = fx
                note = (note + " · " if note else "") + f"손절선 산 값 -{stm['pct']:g}%로 맞춤"
            if not why and close <= fx:
                why = f"손절선 산 값 -{stm['pct']:g}%({money(self.m, fx)}) 아래로 마감 — 고정 손절"
        try:
            fs, fw, brief = Bot.hold_score(self, p, close)
        except Exception as e:
            fs, fw, brief = None, [], ""
            self.log(f"🧭 브리핑 오류 {p.name}: {e}")
        # 하루 한 번 AI 가 다시 읽은 보유 종목 뉴스: 심한 악재면 오늘 정리, 그냥 악재면 점수에 반영
        nj = {}
        try:
            from . import mobile as MB
            from . import news_judge as NJ
            nj = NJ.fresh(MB.NJ_LOCAL.get(self.m), 30).get(p.symbol) or Bot.news_judged(self).get(p.symbol) or {}
        except Exception:
            nj = {}
        if nj.get("impact") == "악재":
            if not why and nj.get("severe"):
                why = f"심한 악재 뉴스(AI): {nj.get('why', '')} — 손절선까지 기다리지 않고 정리"
            elif fs is not None:
                fs += int(nj.get("adj") or -1)
                fw = [f"－새 악재 뉴스(AI): {nj.get('why', '')}"] + list(fw)
        # 손실 검토: -3% 넘게 잃고 있는데 현재·미래 점수까지 약하면 손절선까지 기다리지 않고 정리. 점수가 버티면 보유.
        lr = c.get("loss_review") or {}
        lr_note = ""
        loss = (close / p.entry - 1) * 100 if p.entry else 0
        if not why and lr.get("enabled") and fs is not None and loss <= -lr.get("pct", 3):
            k = lr.get("max_score", 2)
            neg = " ".join(w for w in fw if w.startswith("－"))[:120]
            if fs <= k:
                why = (f"손실 {loss:+.1f}% + 현재·미래 점수 {fs:+d}(기준 {k:+d} 이하)로 약함 — 손절선까지 기다리지 않고 정리"
                       + (f": {neg}" if neg else ""))
            else:
                pos_ = " ".join(w for w in fw if w.startswith("＋"))[:100]
                lr_note = f"손실 검토: {loss:+.1f}%지만 점수 {fs:+d}로 버틸 근거가 있어 보유" + (f" ({pos_})" if pos_ else "")
        Bot.set_review(self, p, fs, fw, loss, bool(why))
        xs = c.get("exit_forward_score")
        if not why and xs is not None and fs is not None and fs <= xs and held >= 1:
            why = (f"현재·미래 근거가 약해짐 (점수 {fs:+d} ≤ {xs:+d}): "
                   + " ".join(w for w in fw if w.startswith("－"))[:120])
        pt_now = (not why and Bot.profit_take(self, p, close)) or p.extra.get("pt_day") == now(self.m).date().isoformat()
        if not why:
            Bot.profit_lock(self, p, close)
        tp_c = c.get("target_take") or {}
        if not why and tp_c.get("enabled") and not p.extra.get("tgt_done") and p.qty >= 2 and hasattr(self.broker, "sell_part"):
            try:
                tgt = SW.cons_target(self.m, Bot.est_row(self, p.symbol) or {}) or p.extra.get("tgt")
                be = round_tick(self.m, p.entry * (1 + cost), "up")
                # 목표가가 산 값보다 낮으면(이미 목표가 위에서 산 경우) '도달'이 아님 — 손해 보고 파는 일 막음
                if tgt and tgt > be and close > be and close >= tgt * (1 - tp_c.get("near_pct", 0) / 100):
                    q = max(1, int(p.qty * tp_c.get("frac", 0.5)))
                    if self.broker.sell_part(p, q, close, f"애널리스트 목표가({money(self.m, tgt)}) 도달 — 일부 익절", be if close > be else None):
                        p.extra["tgt_done"] = True
                        self.notify(f"🎯 {p.name} 증권사 평균 목표가 {money(self.m, tgt)} 도달 → {q}주 익절, 나머지 {p.qty}주는 "
                                    f"손절선 {money(self.m, p.sl)}(본전 이상)으로 계속 보유")
            except Exception as e:
                self.log(f"🧭 목표가 익절 판단 오류 {p.name}: {e}")
        if not why and not pt_now:                       # 오늘 일부 익절했으면 같은 날 다시 사 더하지 않음
            try:
                Bot.swing_add_on(self, p, bars, close)
            except Exception as e:
                self.log(f"🧭 추가 매수 판단 오류 {p.name}: {e}")
        if why:
            self.notify(f"🧭 스윙 정리 {p.name} ({held + 1}거래일째, {(close / p.entry - 1) * 100:+.1f}%) — {why}"
                        + (f"\n{brief}" if brief else ""))
            self.broker.flatten(p.symbol, f"스윙 정리: {why}", close)
            return
        new = round_tick(self.m, new, "down")
        base = dt.datetime.now(TZ["KR"]).date() if self.m == "US" else now(self.m).date()
        p.extra["expire"] = (base + dt.timedelta(days=c.get("expire_days", 20))).isoformat()   # 손절 주문 만료 늘리기
        if hasattr(self.broker, "_modify"):
            try:
                self.broker._modify(p, p.qty, max(new, p.sl))
            except TossError as e:
                self.log(f"[실전] 🧭 {p.name} 손절 주문 갱신 실패: {e}")
                return
        if new > p.sl:
            p.sl = new
        p.extra["carry"] = True
        d = now(self.m).date().isoformat()
        if d not in p.extra.setdefault("days_held", []):
            p.extra["days_held"].append(d)
        self.notify(f"🧭 스윙 점검 {p.name} {held + 1}거래일째 · {money(self.m, close)} ({(close / p.entry - 1) * 100:+.1f}%) "
                    f"· 손절 {money(self.m, p.sl)}" + (f" · {note}" if note else "") + " · 계속 보유"
                    + (f"\n📉 {lr_note}" if lr_note else "") + (f"\n{brief}" if brief else ""))

    def adopt_holdings(self):
        """계좌에 있는데 로봇 관리 목록에 없는 종목(예전에 산 뒤 관리가 끊긴 것)을 스윙 방식으로 이어받아 계속 관리.
        손절선 = 지금가 − 3×ATR (수익이 1×ATR 넘게 나 있으면 최소 본전), 이후 매일 장 마감 15분 전 일봉 점검
        (손절선 따라 올리기·10일선 깨지면 익절/정리·조건 맞으면 한 번 추가 매수). 장기 ETF·ETF·레버리지는 제외."""
        c = self.cfg["daily_swing"]
        ac = c.get("adopt") or {}
        if not ac.get("enabled") or not hasattr(self.broker, "api"):
            return
        items = (self.api.holdings() or {}).get("items", [])
        etf_codes = {x[0] for x in ((self.cfg.get("etf_long") or {}).get("list") or {}).get(self.m, [])}
        skip = set(ac.get("exclude") or [])
        for h in items:
            sym = str(h.get("symbol") or "")
            mk = h.get("marketCountry") or ("KR" if sym[:1].isdigit() else "US")
            qty = int(f(h.get("quantity"), 0) or 0)
            avg = f(h.get("averagePurchasePrice")) or f(h.get("averagePrice"))
            if mk != self.m or qty <= 0 or not avg or sym in self.broker.pos or sym in etf_codes or sym in skip:
                continue
            name = h.get("name") or sym
            try:
                info = (self.api.stocks([sym]) or [{}])[0]
                name = info.get("name") or name
                if LEV.search(name) or str(info.get("securityType") or "STOCK") not in ("STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPT"):
                    continue
            except TossError:
                pass
            last = f(h.get("lastPrice")) or avg
            bars = self.swing_bars(sym, last)
            a = SW.atr([b[1] for b in bars], [b[2] for b in bars], [b[3] for b in bars]) if len(bars) > 21 else None
            a = a or last * 0.04
            fee = self.cfg["fees"][self.m]
            cost = (fee["buy_pct"] + fee["sell_pct"] + fee["sell_tax_pct"]) / 100
            stop = last - ac.get("stop_atr", 3) * a
            if last - avg >= a:
                stop = max(stop, avg * (1 + cost))               # 수익 중이면 최소 본전은 지킴
            sl = round_tick(self.m, stop, "down")
            from .broker import Position
            p = Position(sym, name, qty, avg, round_tick(self.m, avg * 3, "up"), sl, "계좌 보유 이어받기 (스윙 관리)",
                         now(self.m).isoformat(timespec="seconds"), state="open")
            base = dt.datetime.now(TZ["KR"]).date() if self.m == "US" else now(self.m).date()
            p.extra.update(sw=True, carry=True, setup="sw_adopt", sw_kind="adopt", adopted=True,
                           sw_stop0=round(avg - ac.get("stop_atr", 3) * a, 4), sw_hi=max(last, avg), sw_days=0,
                           expire=(base + dt.timedelta(days=c.get("expire_days", 20))).isoformat())
            self.broker.pos[sym] = p
            try:
                self.broker._arm_exit(p)
            except Exception as e:
                self.log(f"🧭 {name} 손절 주문 걸기 실패: {e}")
            self.broker.save_state()
            st = self.syms.setdefault(sym, SymState(sym))
            st.name = name
            self.notify(f"🧭 계좌에 있던 {name} {qty}주(평균 {money(self.m, avg)}, 지금 {(last / avg - 1) * 100:+.1f}%)를 "
                        f"로봇 관리로 이어받았어요 · 손절선 {money(self.m, sl)}"
                        f"{' (본전 이상)' if sl >= avg else ''} · 매일 장 마감 15분 전 일봉으로 '더 들고 갈지/익절할지/추가 매수할지' 판단해요")

    def swing_add_on(self, p, bars, close) -> bool:
        """수익 중인 스윙 종목에 한 번만 추가 매수(불타기): +1R 이상 · 20일 종가 신고가 · 시장 좋음 → 지금 수량의 절반.
        백테스트(국장 2년·미장 10년): 이 조건만 두 시장 모두 칼마비율 개선(2.63→2.85, 0.65→0.72). 다른 조건은 나빠지거나 들쭉날쭉."""
        ac = self.cfg["daily_swing"].get("add_on") or {}
        if not ac.get("enabled") or p.extra.get("added") or p.extra.get("add_oid") or getattr(self, "sw_regime", None) is not True:
            return False
        closes = [b[3] for b in bars]
        if len(closes) < 22:
            return False
        R = max(p.entry - (p.extra.get("sw_stop0") or p.sl), p.entry * 0.005)
        if close < p.entry + ac.get("min_R", 1) * R or close < max(closes[-21:-1]):
            return False
        cap = self.risk.capital or 0
        q = int(p.qty * ac.get("frac", 0.5))
        if cap:
            q = min(q, int(cap * self.cfg["daily_swing"].get("cap_pct", 20) / 100 * ac.get("cap_x", 1.5) / close) - p.qty)
        if q < 1:
            return False
        p.extra["added"] = True
        before = p.qty
        self.broker.add(p, q, close, f"🧭 추가 매수: +{(close / p.entry - 1) * 100:.1f}% · 20일 신고가 마감 · 시장 좋음")
        self.notify(f"🧭 추가 매수 {p.name} {q}주 ≈ {money(self.m, close)} (갖고 있던 {before}주에 더함) — 수익 중·20일 신고가·시장 좋음 "
                    f"· 손절선 {money(self.m, p.sl)} (본전 이상이면 추가분도 손실 작음)")
        return True

    def daily_full(self, sym) -> list:
        bars = []
        try:
            for cd in (self.api.candles_upto(sym, "1d", 260) if hasattr(self.api, "candles_upto") else self.api.candles(sym, "1d", 260)) or []:
                t = parse_ts(cd.get("timestamp"), self.m)
                cl = f(cd.get("closePrice"))
                if t and cl:
                    bars.append((t.date(), f(cd.get("openPrice")) or cl, f(cd.get("highPrice")) or cl, f(cd.get("lowPrice")) or cl,
                                 cl, f(cd.get("volume"), 0.0) or 0.0))
        except Exception:
            pass
        bars.sort(key=lambda b: b[0])
        return bars

    def stock_news(self, code, name):
        """최근 기사 제목: 국장은 네이버 증권 종목 뉴스(종목 코드로 묶여 이름 혼동 없음) + 구글 뉴스, 미장은 구글 뉴스."""
        out = []
        try:
            from .news import recent_news, naver_stock_news
            if self.m == "KR":
                out = naver_stock_news(code, 3)
            out += recent_news(name, self.m)
        except Exception:
            pass
        return list(dict.fromkeys(out))[:5]

    def corr_hit(self, sym, lim, n=60):
        """새 후보와 보유 스윙 종목의 최근 n일 일간 수익률 상관. 기준 넘는 첫 종목 → (이름, 상관) 또는 None."""
        held = [pp for pp in self.broker.pos.values() if pp.state in ("pending", "open") and pp.symbol != sym]
        if not held:
            return None

        def rets(s_):
            b = Bot.daily_full(self, s_) or []
            cl = [r[4] for r in b][-(n + 1):]
            return [cl[i] / cl[i - 1] - 1 for i in range(1, len(cl)) if cl[i - 1]]
        a = rets(sym)
        for pp in held:
            bb = rets(pp.symbol)
            k = min(len(a), len(bb))
            if k < 30:
                continue
            r = SW.corr(a[-k:], bb[-k:])
            if r is not None and r > lim:
                return (pp.extra.get("name") or pp.symbol, r)
        return None

    def est_row(self, sym):
        c = self.cfg["daily_swing"]
        today = now(self.m).date()
        if getattr(self, "_est_day", None) != today and c.get("estimates_url"):
            self._est = SW.load_estimates(c["estimates_url"], self.m, today, self.log)
            self._est_day = today
        return (getattr(self, "_est", None) or {}).get(sym)

    def cand_check(self, x, price) -> tuple:
        """매수 직전 후보 종합 점검 → (안 살 이유 또는 None, 브리핑 글, 주의 목록).
        보는 것: 신호 난 날·오늘 봉과 거래량, 20일선, 애널리스트 목표가·의견·이익 전망, 실적일, (국장) 외국인·기관, 최근 뉴스."""
        c = self.cfg["daily_swing"]
        ev = x.get("ev") or {}
        est = Bot.est_row(self, x["code"]) or {}
        if self.m == "US" and not est.get("targetMeanPrice") and ev.get("tgt_up") is not None:
            est = {**est, "targetMeanPrice": x["close"] * (1 + ev["tgt_up"]), "recommendationMean": ev.get("rec"),
                   "numberOfAnalystOpinions": ev.get("n_analysts"), "eps_0q_current": ev.get("eps0q"), "eps_0q_30daysAgo": ev.get("eps0q_30d")}
        news = []
        if c.get("brief_news", True) and os.getenv("TOSSBOT_OFFLINE") != "1":
            news = Bot.stock_news(self, x["code"], x["name"]) + [t for t in (ev.get("news") or [])[:3]]
            news = list(dict.fromkeys(news))[:5]
        bars = Bot.daily_full(self, x["code"])
        txt = SW.brief_text(self.m, bars, price, est, ev.get("react"), news, now(self.m).date(), lambda v: money(self.m, v))
        tone = 0
        try:
            from .news import NewsWatch
            nw = NewsWatch.__new__(NewsWatch)
            nw.m = self.m
            tone = sum(NewsWatch.tone(nw, t) for t in news)
        except Exception:
            tone = 0
        fs, fwhy = SW.forward_score(self.m, bars, price, est, ev, tone)
        tg0 = SW.cons_target(self.m, est)
        rr0 = None
        if tg0 and price and x.get("stop") and not SW.long_downtrend(bars, price):
            adj, rtxt, rr0 = SW.rr_score(tg0 / price - 1, (price - float(x["stop"])) / price)
            if rtxt:
                fs += adj
                fwhy = [rtxt] + fwhy
        nj = Bot.news_judged(self).get(str(x["code"])) or {}
        if nj.get("impact") in ("호재", "악재"):
            fs += int(nj.get("adj") or 0)
            fwhy = [f"{'＋' if nj['impact'] == '호재' else '－'}새 뉴스 AI {nj['impact']}: {nj.get('why', '')}"] + fwhy
        x["_news_ai"] = nj or None
        x["_rr"] = rr0
        x["_fscore"], x["_fwhy"] = fs, fwhy
        x["_tgt"] = SW.cons_target(self.m, est) or (price * (1 + ev["tgt_up"]) if (x.get("ev") or {}).get("tgt_up") is not None and price else None)
        txt = (f"🎯 현재·미래 점수 {fs:+d} ({SW.grade_of(fs)}): " + " ".join(fwhy[:8]) + ("\n" + txt if txt else "")) if fwhy else txt
        warn, why = [], None
        n = SW._num
        st_ = x.get("setup") or "fs"
        strict = st_ in set(c.get("strict_setups") or ["fs"])     # 과열·차트 모양·목표가·손절 폭 기준은 현재·미래 점수 방식에만 (다른 방식은 검증된 자기 규칙대로)
        rbx = st_ in set(c.get("rebound_setups") or [])           # 급락 뒤 반등: 떨어진 종목을 사는 방식이라 '오르는 추세' 기준을 안 씀
        x["_strict"] = strict
        if rbx and price and x.get("close"):
            if price < float(x["close"]) * (1 - c.get("rebound_fail_pct", 3) / 100) or (x.get("low") and price <= float(x["low"])):
                why = f"반등 신호 뒤 다시 밀림 (신호 날 종가 {money(self.m, float(x['close']))} → 지금 {money(self.m, price)}) — 반등 실패로 보고 안 삼"
        if self.m == "US":
            tg, rec = n(est.get("targetMeanPrice")), n(est.get("recommendationMean"))
            bad_rec = rec is not None and rec >= 3.0
        else:
            tg, rec = n(est.get("cs_priceTargetMean")), n(est.get("cs_recommMean"))
            bad_rec = rec is not None and rec <= 3.0
            fo, og = n(est.get("foreignerPureBuyQuant")), n(est.get("organPureBuyQuant"))
            if fo is not None and og is not None and fo < 0 and og < 0:
                warn.append("외국인·기관 동반 순매도")
        if tg and price and tg < price:
            warn.append(f"애널리스트 평균 목표가({money(self.m, tg)})가 지금 가격보다 낮음")
            if tg < price * (1 - c.get("tgt_below_pct", 5) / 100) and bad_rec:
                why = f"애널리스트 평균 목표가가 지금보다 {(1 - tg / price) * 100:.0f}% 낮고 의견도 중립 이하"
        rv, _ = SW.rev_state(ev)
        if rv == "down":
            warn.append("이익 전망 하향 중")
        mx = c.get("max_ext20")                       # 20일선보다 너무 높이 떠 있으면(단기 과열) 안 삼 — 후보가 많으니 굳이 위험 안 짐
        if why is None and strict and mx and len(bars) >= 20 and price:
            ma20 = sum(b[4] for b in bars[-20:]) / 20
            if price >= ma20 * (1 + mx):
                why = f"20일선보다 {(price / ma20 - 1) * 100:+.0f}% 높이 떠 있어 단기 과열 — 쉬어 갈 때 다시 봄"
        cq, cqw = SW.chart_quality(bars)
        x["_cq"] = cq
        if why is None and strict and cq <= c.get("min_chart_q", -2):
            why = f"차트 모양이 나빠요 (모양 점수 {cq:+d}): " + " ".join(w.replace("차트 모양: ", "") for w in cqw if w.startswith("－"))[:120]
        if why is None and c.get("skip_spike", False):
            sp = SW.spike_note(bars, price)
            if sp:
                why = sp
        if why is None and strict and c.get("need_target") and rr0 is None:
            good = cq >= c.get("no_target_chart_q", 3) or nj.get("impact") == "호재" or \
                ((x.get("ev") or {}).get("op_yoy") or 0) >= 0.2 and not (x.get("ev") or {}).get("op_stale")
            if not good:
                why = f"증권사 목표가가 없고 차트 모양({cq:+d})·호재도 충분하지 않음 — 목표가 없는 종목은 차트가 아주 좋거나 호재가 있을 때만"
        ms = c.get("max_stop_pct")                    # 손절 폭이 너무 넓으면 한 번 틀릴 때 많이 잃음 → 안 삼
        if why is None and strict and ms and price and x.get("stop") and (price - float(x["stop"])) / price > ms:
            why = f"손절 폭 {(price - float(x['stop'])) / price * 100:.0f}% — 너무 넓어서(기준 {ms * 100:.0f}%) 안 사요"
        ld = SW.long_downtrend(bars, price)
        if why is None and ld and not rbx:
            ok, nev = SW.turnaround(fwhy)
            if not ok:
                why = ld + f" · 실적·수급·뉴스 반전 근거 {nev}개(2개 이상 필요)"
            else:
                warn.append(f"장기 하락 중 — 반전 근거 {nev}개로 매수 허용")
        if why is None and nj.get("severe"):
            why = f"새로 나온 심한 악재 뉴스(AI 판정): {nj.get('why', '')} — {nj.get('head', '')[:40]}"
        elif nj.get("impact") == "악재":
            warn.append(f"새 악재 뉴스: {nj.get('why', '')}")
        bk = x.get("_bad_min")                   # 시장이 내려가는 중: 이 방식은 점수가 충분히 높을 때만 삼 (또 하나의 판단 근거)
        if why is None and bk is not None and fs < bk:
            why = f"시장 하락 추세(지수가 평균선 아래) — 이 방식은 점수 {bk:+d} 이상일 때만 삼 (지금 {fs:+d})"
        if why is None and fwhy and fs < c.get("min_forward_score", 2) and not rbx:
            why = f"현재·미래 근거 부족 (점수 {fs:+d}, 기준 {c.get('min_forward_score', 2):+d} 이상): " + " ".join(w for w in fwhy if w.startswith("－"))[:120]
        if bars and len(bars) >= 2 and bars[-1][0] == now(self.m).date() and price < bars[-1][1] * 0.97:
            warn.append("오늘 시가보다 3% 넘게 밀리는 중")
        return why, txt, warn

    def hold_brief(self, p, last=None) -> str:
        return Bot.hold_score(self, p, last)[2]

    def hold_score(self, p, last=None) -> tuple:
        """(현재·미래 점수 또는 None, 근거, 브리핑 글)"""
        """보유 종목의 지금 상황: 오늘 봉·거래량·20일선·애널리스트 목표가·이익 전망·(국장)외국인·기관·실적일·최근 뉴스.
        매일 장 마감 15분 전 점검 알림과 텔레그램 '분석' 에 붙음 (판단 재료 — 자동 매매 규칙은 따로)."""
        c = self.cfg["daily_swing"]
        today = now(self.m).date()
        bars = Bot.daily_full(self, p.symbol)
        est = Bot.est_row(self, p.symbol)
        news = []
        if c.get("brief_news", True) and os.getenv("TOSSBOT_OFFLINE") != "1":
            news = Bot.stock_news(self, p.symbol, p.name)
        last = last or (bars[-1][4] if bars else p.entry)
        txt = SW.brief_text(self.m, bars, last, est, p.extra.get("earn_react"), news, today, lambda v: money(self.m, v))
        fs, fw = None, []
        try:
            from .news import NewsWatch
            nw = NewsWatch.__new__(NewsWatch)
            nw.m = self.m
            fs, fw = SW.forward_score(self.m, bars, last, est, {}, sum(NewsWatch.tone(nw, t) for t in news))
            if fw:
                was = p.extra.get("fscore")
                txt = (f"🎯 현재·미래 점수 {fs:+d} ({SW.grade_of(fs)})" + (f" · 살 때 {was:+d}" if was is not None else "")
                       + ": " + " ".join(fw[:8]) + ("\n" + txt if txt else ""))
        except Exception:
            fs, fw = None, []
        return fs, fw, txt

    def rotate(self, cands, have, extra, px, today) -> bool:
        """자리가 꽉 찼을 때 교체: 지금 확실히 좋은 후보(점수 높고 AI 긍정·점검 통과)가 있고, 들고 있는 종목 중
        며칠째 제자리이면서 점수가 약해진 종목이 있으면 → 약한 종목을 팔고 그 자리에 삼. 하루 한 번만."""
        c = self.cfg["daily_swing"]
        rot = c.get("rotation") or {}
        if not rot.get("enabled") or getattr(self, "_rot_day", None) == today.isoformat():
            return False
        strict = set(c.get("strict_setups") or ["fs"])
        best = next((x for x in cands if x.get("_chk") and not x["_chk"][1][0] and (x.get("setup") or "fs") in strict
                     and (x.get("_fscore") or 0) >= rot.get("min_score", 10)
                     and ((x.get("ev") or {}).get("ai") or {}).get("view") == "긍정"
                     and not ((x.get("ev") or {}).get("ai") or {}).get("veto")), None)
        if not best:
            return False
        first = next((x for x in cands if x.get("_chk") and not x["_chk"][1][0]), None)
        t1 = (rot.get("top1_by_market") or {}).get(self.m)
        if t1 and best is first:                 # 다시 점검한 후보 1위가 확실하면 교체 기준을 조금 넓힘 (예: 한국가스공사 놓친 경우)
            rot = {**rot, **t1}
        weak = []
        for p in have:
            if p.state != "open" or not p.extra.get("sw") or p.extra.get("sw_kind") in extra or p.extra.get("etf_long"):
                continue
            rv = p.extra.get("review") or {}
            st = self.syms.get(p.symbol)
            last = (st.last if st and st.last else None) or px.get(p.symbol)
            if rv.get("fs") is None or not last or time.time() - float(rv.get("t") or 0) > 3 * 3600:
                continue
            gain = (last / p.entry - 1) * 100
            if (int(p.extra.get("sw_days", 0)) >= rot.get("min_days", 3) and rot.get("min_gain", -3) <= gain <= rot.get("max_gain", 1.5)
                    and rv["fs"] <= rot.get("max_held_score", 5) and not p.extra.get("pt_done")):
                weak.append((rv["fs"], gain, p, last))
        if not weak:
            return False
        weak.sort(key=lambda t: (t[0], t[1]))
        fs_w, gain, p, last = weak[0]
        if (best.get("_fscore") or 0) - fs_w < rot.get("gap", 5):
            return False
        why = (f"교체 — {int(p.extra.get('sw_days', 0))}일째 {gain:+.1f}% 제자리에 점수 {fs_w:+d}로 약해짐 → "
               f"더 확실한 {best['name']}(점수 {best.get('_fscore', 0):+d}, AI 긍정)으로 바꿈")
        self.broker.flatten(p.symbol, why, last)
        self._rot_day = today.isoformat()
        self.notify(f"🔁 {p.name} 팔고 {best['name']} 사요 — {why}")
        return True

    def setup_cfg(self, p=None) -> dict:
        """그 종목을 산 방식의 규칙 (방식별 예외 exit_by_setup 를 덮어씀) — 마감 점검과 같은 기준."""
        c = dict(self.cfg["daily_swing"])
        if p is not None:
            c.update((c.get("exit_by_setup") or {}).get(p.extra.get("sw_kind")) or {})
        return c

    def review_verdict(self, fs, loss, p=None) -> tuple:
        """로봇 규칙 그대로의 보유 판정 → ('정리 예정'|'주의'|'유지', 한 줄 설명). 앱 표시·알림이 같은 기준을 씀."""
        c = Bot.setup_cfg(self, p)
        if p is not None and p.extra.get("sw_kind") in set(c.get("rebound_setups") or []):
            return "유지", f"급락 뒤 반등 방식 — 손절선과 최대 {c.get('max_hold', 15)}거래일 보유 규칙으로만 관리해요 (점수로 팔지 않음)"
        lr = c.get("loss_review") or {}
        xs = c.get("exit_forward_score")
        k, pct = lr.get("max_score", 2), lr.get("pct", 3)
        if fs is None:
            return "주의", "점수를 계산하지 못했어요"
        if xs is not None and fs <= xs:
            return "정리 예정", f"점수 {fs:+d} — 기준 {xs:+d} 이하라 마감 15분 전 점검 때 정리해요"
        if lr.get("enabled") and loss <= -pct and fs <= k:
            return "정리 예정", f"손실 {loss:+.1f}% + 점수 {fs:+d}(기준 {k:+d} 이하) — 마감 15분 전 점검 때도 이대로면 손절선까지 안 기다리고 정리해요"
        if lr.get("enabled") and loss <= -pct:
            return "주의", f"손실 {loss:+.1f}%지만 점수 {fs:+d}로 버틸 근거가 있어 보유 (점수가 {k:+d} 이하로 내려가면 정리)"
        if fs <= k + 1:
            return "주의", f"점수 {fs:+d} — 약해지는 중 (손실 -{pct:g}% 넘고 점수 {k:+d} 이하면 정리)"
        return "유지", f"점수 {fs:+d} — 계속 보유"

    def set_review(self, p, fs, fw, loss, selling=False):
        g, txt = Bot.review_verdict(self, fs, loss, p)
        if selling:
            g = "정리 예정"
        p.extra["review"] = {"t": time.time(), "fs": fs, "loss": round(loss, 2), "grade": g, "txt": txt,
                             "why": [w for w in (fw or []) if w[:1] in "＋－"][:8]}
        return g, txt

    def loss_watch(self, p, last):
        """장중 실시간: 손실이 -3% 를 처음 넘는 순간 지금 점수로 검토해서 알림 (파는 결정은 마감 15분 전 점검에서 —
        장중에 바로 파는 방식은 백테스트에서 수익이 줄었음: 장중 저점에서 팔고 반등을 놓침)."""
        c = Bot.setup_cfg(self, p)                 # 방식별 규칙 (반등 방식은 손실 검토를 안 함)
        lr = c.get("loss_review") or {}
        if not lr.get("enabled") or p.state != "open" or not p.extra.get("sw") or not last or not p.entry:
            return
        loss = (last / p.entry - 1) * 100
        today = now(self.m).date().isoformat()
        if loss > -lr.get("pct", 3) or p.extra.get("lr_day") == today:
            return
        p.extra["lr_day"] = today

        def work():
            try:
                fs, fw, _ = Bot.hold_score(self, p, last)
            except Exception as e:
                self.log(f"📉 손실 검토 오류 {p.name}: {e}")
                return
            g, txt = Bot.set_review(self, p, fs, fw, loss)
            neg = " ".join(w for w in fw if w.startswith("－"))[:100]
            pos_ = " ".join(w for w in fw if w.startswith("＋"))[:100]
            self.notify(f"📉 {p.name} 손실 {loss:+.1f}% (-{lr.get('pct', 3):g}% 넘음) — 손실 검토: {g}\n{txt}"
                        + (f"\n약한 점: {neg}" if neg and g != "유지" else "") + (f"\n버티는 근거: {pos_}" if pos_ and g != "정리 예정" else "")
                        + f"\n손절선 {money(self.m, p.sl)}은 그대로 걸려 있어요.")
        Bot._bg_run(self, work)

    def review_tick(self):
        """30분마다 보유 스윙 종목을 로봇 규칙(현재·미래 점수)으로 다시 검토해 앱에 보여 줄 판정을 갱신 (알림 없음)."""
        if time.time() - getattr(self, "_rv_t", 0) < self.cfg["daily_swing"].get("review_min", 30) * 60:
            return
        self._rv_t = time.time()
        todo = []
        for sym, p in list(self.broker.pos.items()):
            st = self.syms.get(sym)
            if p.state == "open" and p.extra.get("sw") and st and st.last:
                todo.append((p, st.last))

        def work():
            for p, last in todo:
                try:
                    fs, fw, _ = Bot.hold_score(self, p, last)
                    Bot.set_review(self, p, fs, fw, (last / p.entry - 1) * 100 if p.entry else 0)
                except Exception as e:
                    self.log(f"보유 검토 오류 {p.name}: {e}")
        if todo:
            Bot._bg_run(self, work)

    def _bg_run(self, fn):
        """인터넷 조회가 낀 검토는 뒤에서 — 매매 판단(메인 루프)을 멈추지 않게. 시험(sync_bg)에서는 바로 실행."""
        if getattr(self, "sync_bg", False):
            fn()
            return
        import threading
        threading.Thread(target=fn, daemon=True).start()

    def profit_take(self, p, last) -> bool:
        """+pct% 넘게 오르면 1/3(2주면 1주) 익절 — 종목당 한 번. be=true 면 나머지 손절선을 본전 위로(1주면 손절선만).
        장중 15초마다(실시간 가격) + 마감 점검 때 확인. (사용자 설정: +4%·본전 이동)"""
        c = self.cfg["daily_swing"]
        pt = c.get("profit_take") or {}
        if not pt.get("enabled") or p.extra.get("pt_done") or p.state != "open" or not p.extra.get("sw") or not last:
            return False
        if p.extra.get("partial_oid") or last < p.entry * (1 + pt.get("pct", 4) / 100):
            return False
        fee = self.cfg["fees"][self.m]
        cost = (fee["buy_pct"] + fee["sell_pct"] + fee["sell_tax_pct"]) / 100
        be = round_tick(self.m, p.entry * (1 + cost), "up")
        move = pt.get("be", True)                        # 익절하면서 손절선을 본전으로 올릴지
        gain = (last / p.entry - 1) * 100
        try:
            q = max(1, int(p.qty * pt.get("frac", 1 / 3))) if p.qty >= 2 else 0
            if q and hasattr(self.broker, "sell_part"):
                if self.broker.sell_part(p, q, last, f"+{gain:.1f}% 수익 — 일부 익절", be if move and be > p.sl else None):
                    p.extra["pt_done"] = True
                    p.extra["pt_day"] = now(self.m).date().isoformat()
                    self.notify(f"💰 {p.name} {gain:+.1f}% → {q}주 익절, 나머지 {p.qty}주는 손절선 {money(self.m, p.sl)}"
                                + ("(본전 이상)" if move else "") + "으로 계속 보유")
                    return True
            elif not move:
                p.extra["pt_done"] = True                # 1주 + 본전 이동 안 함 → 할 일 없음
            elif be > p.sl:                              # 1주: 팔 수 없으니 손절선만 본전으로
                if hasattr(self.broker, "_modify"):
                    self.broker._modify(p, p.qty, be)
                p.sl = be
                p.extra["pt_done"] = True
                self.notify(f"🔒 {p.name} {gain:+.1f}% → 1주라 팔지 않고 손절선을 본전({money(self.m, be)})으로 올림")
                return True
        except Exception as e:
            self.log(f"🧭 +{pt.get('pct', 4)}% 익절 오류 {p.name}: {e}")
        return False

    def profit_lock(self, p, last) -> bool:
        """오른 만큼 이익을 잠금 (장중 15초마다, 실시간 가격). 손절선은 올리기만 하고 절대 내리지 않음.
        steps [[오른 %, 잠글 %], ...]: 최고 +7% 찍으면 손절선을 +3%로, +10% 면 +5%로 …
        trail_from % 넘게 오르면 그 뒤로는 '최고가 − trail_pct %'를 따라 올라감.
        take2_pct % 에 닿으면 남은 수량의 take2_frac 를 한 번 더 익절."""
        c = self.cfg["daily_swing"]
        pl = c.get("profit_lock") or {}
        if not pl.get("enabled") or p.state != "open" or not p.extra.get("sw") or not last or not p.entry or p.extra.get("partial_oid"):
            return False
        pk = max(float(p.extra.get("pl_peak") or 0), float(last))
        p.extra["pl_peak"] = pk
        g = (pk / p.entry - 1) * 100
        lock = None
        for up, lk in pl.get("steps") or []:
            if g >= up:
                lock = lk if lock is None else max(lock, lk)
        new = p.entry * (1 + lock / 100) if lock is not None else None
        if pl.get("trail_from") is not None and g >= pl["trail_from"]:
            tr = pk * (1 - pl.get("trail_pct", 5) / 100)
            new = tr if new is None else max(new, tr)
        did = False
        try:
            t2 = pl.get("take2_pct")
            if t2 and not p.extra.get("pl_t2") and last >= p.entry * (1 + t2 / 100) and p.qty >= 2 and hasattr(self.broker, "sell_part"):
                q = max(1, int(p.qty * pl.get("take2_frac", 0.5)))
                stp = round_tick(self.m, new, "down") if new else None
                if self.broker.sell_part(p, q, last, f"+{(last / p.entry - 1) * 100:.1f}% — 두 번째 익절", stp if stp and stp > p.sl else None):
                    p.extra["pl_t2"] = True
                    did = True
                    self.notify(f"💰💰 {p.name} {(last / p.entry - 1) * 100:+.1f}% → {q}주 더 익절, 나머지 {p.qty}주는 손절선 {money(self.m, p.sl)}으로 이익 잠금")
                    return True
            if new is None:
                return did
            new = round_tick(self.m, new, "down")
            if new <= p.sl or new >= last * 0.998:
                return did
            if hasattr(self.broker, "_modify"):
                self.broker._modify(p, p.qty, new)
            old = p.sl
            p.sl = new
            if (new / p.entry - 1) * 100 - ((p.extra.get("pl_note") or -99)) >= 1 or old < p.entry <= new:
                p.extra["pl_note"] = round((new / p.entry - 1) * 100, 1)
                self.notify(f"🔒 {p.name} 최고 {g:+.1f}%까지 올라서 손절선을 {money(self.m, new)}({(new / p.entry - 1) * 100:+.1f}%)로 올림 — "
                            f"여기까지 내려와도 이익으로 팔려요")
            return True
        except Exception as e:
            self.log(f"🔒 이익 잠금 오류 {p.name}: {e}")
        return did

    def earn_presell(self, p, close) -> bool:
        """내일이 실적 반응일인데, 살 때 실적을 감안해 줄여 사지 않았고 아직 +1R 수익 쿠션이 없으며
        실적 갭이 나면 손실이 한도를 넘을 만큼 크면 → 오늘 장 마감 전에 정리."""
        ec = self.cfg["daily_swing"].get("earnings") or {}
        r = p.extra.get("earn_react")
        if not ec.get("enabled") or not r or p.extra.get("earn_hold"):
            return False
        try:
            rd = dt.date.fromisoformat(r)
        except ValueError:
            return False
        if rd != SW.next_biz(now(self.m).date()):
            return False
        R = max(p.entry - (p.extra.get("sw_stop0") or p.sl), p.entry * 0.005)
        gap = float(p.extra.get("earn_gap") or ec.get("default_gap", 0.07))
        risk = p.qty * close * gap * ec.get("gap_mult", 2) - max(0.0, (close - p.entry) * p.qty)
        lim = (self.risk.capital or 0) * self.cfg["daily_swing"].get("risk_pct", 1) / 100 * ec.get("presell_x", 1.5)
        if (close - p.entry) >= R or not lim or risk <= lim:
            return False
        why = f"내일 실적 발표 — 갭이 나면 손실이 한도를 넘을 수 있어 미리 정리 (평균 실적 갭 {gap * 100:.0f}%)"
        self.notify(f"🧭 스윙 정리 {p.name} ({(close / p.entry - 1) * 100:+.1f}%) — {why}")
        self.broker.flatten(p.symbol, f"스윙 정리: {why}", close)
        return True

    def load_stopped(self):
        """최근 N일 안에 손실로 판 종목 → 다시 사지 않음 (같은 종목에 연달아 손절 방지, 예: 코세스 이틀 연속)."""
        c = self.cfg.get("reentry_block", {})
        self.stopped = {}
        self.recent_sold = {}
        if not c.get("enabled"):
            return
        from .stats import load, merge
        recs = merge(load(ROOT, self.m, max(c.get("days", 5), c.get("any_days", 3)), self.mode if self.mode in ("live", "paper") else "live"))
        cut_any = (now(self.m).date() - dt.timedelta(days=c.get("any_days", 3))).isoformat()
        cut_loss = (now(self.m).date() - dt.timedelta(days=c.get("days", 5))).isoformat()
        for r in recs:
            if r.get("kind") != "exit":
                continue
            if float(r.get("pnl") or 0) < 0 and r["_day"] >= cut_loss:
                self.stopped[r.get("symbol")] = r["_day"]
            if r["_day"] >= cut_any and (r.get("extra") or {}).get("sw"):
                self.recent_sold[r.get("symbol")] = r["_day"]
        if self.stopped:
            self.log(f"⛔ 최근 {c.get('days', 5)}일 안에 손실로 판 종목 {len(self.stopped)}개는 다시 안 사요")

    def week_guard(self):
        """이번 주(월요일부터) 실전 손실이 투자금의 N% 를 넘으면 이번 주는 새로 안 삼 (다음 주 월요일에 자동 재개)."""
        pct = self.cfg["risk"][self.m].get("weekly_loss_pct")
        cap = getattr(self.risk, "capital", None)
        if not pct or not cap:
            return
        from .stats import load, merge
        today = now(self.m).date()
        mon = today - dt.timedelta(days=today.weekday())
        tot = sum(float(r.get("pnl") or 0) for r in merge(load(ROOT, self.m, 7, "live")) if r["_day"] >= mon.isoformat())
        if tot <= -cap * pct / 100:
            self.risk.halted = (f"이번 주 손실 {money(self.m, tot)} 가 투자금의 {pct:g}% 를 넘어 이번 주는 새로 안 사요 "
                                f"(보유 종목 관리는 계속, 다음 주 월요일 자동 재개)")
            self.log("⛔ " + self.risk.halted)
            self.notify("⛔ " + self.risk.halted)

    def perf_size(self):
        """최근 실전 성적이 마이너스면 새 매수 크기를 줄임 (나아지면 자동으로 원래대로)."""
        c = self.cfg.get("perf_size", {})
        if not c.get("enabled") or self.mode != "live":
            return
        from .stats import recent_pnl
        n, tot = recent_pnl(ROOT, self.m, c.get("days", 10))
        if n >= c.get("min_trades", 5) and tot < 0:
            self.risk.perf_mult = c.get("mult", 0.5)
            msg = (f"📉 최근 {c.get('days', 10)}일 실전 {n}건 합계 {money(self.m, tot)} → 성적이 회복될 때까지 "
                   f"새 매수 크기를 {self.risk.perf_mult:g}배로 줄여요")
            self.log(msg)
            self.notify(msg)

    def setup_gate(self):
        g = self.cfg.get("setup_gate", {})
        if not g.get("enabled"):
            return
        from .stats import SETUP_NAMES, load, setup_of
        from .stats import merge
        recs = merge(load(ROOT, self.m, g.get("days", 30), "live")) + merge(load(ROOT, self.m, g.get("days", 30), "paper"))
        by: dict = {}
        for r in recs:
            by.setdefault(setup_of(r), []).append(float(r.get("pnl") or 0))
        off = []
        for name, v in by.items():
            if name in ("?", "etf") or str(name).startswith("sw_"):
                continue                       # 스윙은 며칠~몇 주 단위라 단타 기준으로 끄지 않음
            win, loss = sum(x for x in v if x > 0), -sum(x for x in v if x <= 0)
            pf = win / loss if loss else 9.9
            rk = getattr(self, "risk", None)
            cap = (getattr(rk, "capital", None) or self.cfg["risk"][self.m].get("daily_capital") or 0)
            one_r = cap * self.cfg["risk"][self.m].get("risk_per_trade_pct", 1.0) / 100
            # 3번 다 잃었고 합쳐서 1R 넘게 잃었으면 바로 (본전 근처 장 마감 정리 같은 자잘한 손실만으론 안 끔)
            allbad = (len(v) >= g.get("all_loss_trades", 3) and not any(x > 0 for x in v)
                      and -sum(v) >= one_r * g.get("all_loss_min_R", 1.0))
            if len(v) < g.get("min_trades", 4) and not allbad:
                continue
            if (pf < g.get("min_pf", 0.7) or allbad) and sum(v) < 0:
                if name == "orb":
                    self.cfg["strategy"]["orb_enabled"] = False
                elif name in self.cfg["strategy"].get("setups", {}):
                    self.cfg["strategy"]["setups"][name]["enabled"] = False
                off.append(f"{SETUP_NAMES.get(name, name)}({len(v)}건, 이익합/손실합 {pf:.2f})")
        st = load_settings()
        st.setdefault("gated", {})[self.m] = off
        save_settings(st)
        if off:
            msg = "🚫 최근 성적이 나빠서 이번 장에서 끈 매수 방식: " + ", ".join(off)
            self.log(msg)
            self.notify(msg)

    # ───────── 손절 판단 재료: 거래량·평균 단가·시장 흐름·악재 ─────────
    def market_move(self):
        """시장 흐름: 후보 종목들의 최근 15분 평균 등락률(%). 종목이 적으면 None. 30초마다 다시 계산."""
        t = time.time()
        if t - getattr(self, "_mm_t", 0) < 30:
            return getattr(self, "_mm", None)
        vals = []
        try:
            for sym in list(self.universe)[:150]:
                st = self.syms.get(sym)
                if not st or not st.last:
                    continue
                bars = session_bars(st, self.open_t)
                if len(bars) < 16 or not bars[-16].c:
                    continue
                vals.append((st.last / bars[-16].c - 1) * 100)
        except RuntimeError:                           # 실시간 스레드가 분봉 고치는 중
            return getattr(self, "_mm", None)
        self._mm = sum(vals) / len(vals) if len(vals) >= 15 else None
        self._mm_t = t
        return self._mm

    def stop_info(self, sym):
        out = {}
        st = self.syms.get(sym)
        if st:
            try:
                bars = session_bars(st, self.open_t)
            except RuntimeError:
                bars = []
            out["vwap"] = vwap(bars) if bars else None
            if len(bars) >= 8:
                recent, base = bars[-3:], bars[-23:-3]
                avg = sum(b.v for b in base) / len(base) if base else 0
                if avg > 0 and recent[-1].c < recent[0].o:   # 내려가면서 거래량이 많은지(파는 힘)
                    out["vol_x"] = sum(b.v for b in recent) / len(recent) / avg
        out["market"] = self.market_move()
        d = (self.ctx.get("daily") or {}).get(sym) or {}
        nb = (self.ctx.get("news_bad") or {}).get(sym)
        if sym in self.ctx.get("earn_bad", ()):
            out["bad"] = "실적 실망 종목"
        elif nb:
            out["bad"] = "악재 뉴스(" + nb.get("title", "")[:30] + ")"
        elif d.get("label") == "주의":
            out["bad"] = "일봉 차트가 '주의'"
        return out

    def pre_close_alert(self, flatten_t):
        """정리 예정 종목 설명: 왜 파는지 + 들고 가도 될지 판단 재료 + 로봇 의견."""
        fmt = lambda v: money(self.m, v)
        mk = self.market_move()
        for sym, p in list(self.broker.pos.items()):
            if p.state != "open" or p.extra.get("locked") or p.extra.get("sw"):
                continue
            st = self.syms.get(sym)
            last = st.last if st else None
            if not last:
                continue
            try:
                sb = session_bars(st, self.open_t)
            except RuntimeError:
                sb = []
            vw = vwap(sb) if sb else None
            hi = max((b.h for b in sb), default=last)
            day = (last / st.prev_close - 1) * 100 if st.prev_close else None
            strong = bool(day is not None and day >= 3 and last >= hi * 0.98 and (not vw or last >= vw))
            why = self.broker.carry_check(p, last, vw, strong)
            if why is None and sym in self.ctx.get("earn_block", ()) and sym not in self.ctx.get("earn_good", {}):
                why = "실적 발표가 오늘·내일이라 밤사이 위험"
            prof = (last / p.entry - 1) * 100
            if why is None:
                continue                                          # 들고 가는 종목은 알림 안 함(장 마감 요약에 나옴)
            good, bad = [], []
            if day is not None:
                if day >= 3:
                    good.append(f"오늘 {day:+.1f}% 강세")
                elif day < 0:
                    bad.append(f"오늘 {day:+.1f}% 약세")
            if vw:
                (good if last >= vw else bad).append("오늘 평균 단가 " + ("위" if last >= vw else "아래"))
            (good if last >= hi * 0.98 else bad).append("고점 근처에서 마감 중" if last >= hi * 0.98 else f"오늘 고점({fmt(hi)})에서 {(last / hi - 1) * 100:.1f}% 밀림")
            d = st.daily or {}
            if d.get("ma20"):
                (good if d["close"] >= d["ma20"] else bad).append("일봉 20일선 " + ("위(상승 추세)" if d["close"] >= d["ma20"] else "아래(하락 추세)"))
            lab = (self.ctx.get("daily", {}).get(sym) or {}).get("label")
            if lab:
                if lab == "관심":
                    good.append("일봉 차트 '관심'")
                elif lab == "주의":
                    bad.append("일봉 차트 '주의'")
            if sym in (self.ctx.get("news_good") or {}):
                good.append("오늘 호재 뉴스")
            if sym in (self.ctx.get("news_bad") or {}):
                bad.append("오늘 악재 뉴스")
            if sym in self.ctx.get("earn_block", ()):
                bad.append("실적 발표 임박")
            if mk is not None:
                (bad if mk < -0.3 else good).append(f"시장 최근 15분 {mk:+.1f}%")
            score = len(good) - len(bad)
            opinion = ("들고 가도 괜찮은 편 (강하게 마감 중)" if score >= 3 else
                       "정리 추천 (약하게 마감 중)" if score <= -1 else "애매해요 (반반)")
            msg = (f"⏰ 장 마감 정리 예정 ({flatten_t.strftime('%H:%M')}) — {p.name} {p.qty}주\n"
                   f"산 값 {fmt(p.entry)} → 지금 {fmt(last)} ({prof:+.1f}%) · 손절선 {fmt(p.sl)}\n"
                   f"파는 이유: {why}\n"
                   f"👍 " + (", ".join(good) or "없음") + "\n👎 " + (", ".join(bad) or "없음") + "\n"
                   f"로봇 의견: {opinion}\n"
                   f"→ 들고 가려면 정리 전에 「잠금 {p.name}」이라고 보내세요 (안 보내면 자동으로 팔아요). "
                   f"잠그면 밤사이 갭하락 시 손절선보다 낮게 팔릴 수 있어요.")
            self.log(msg)
            self.notify(msg)

    def same_group(self, sym) -> str | None:
        """이 종목과 같이 움직이는(오늘 1분 수익률 상관 0.7+) 보유 종목이 이미 max_same 개 이상이면 그 이름들."""
        c = self.cfg.get("diversify", {})
        if not c.get("enabled") or not getattr(self, "broker", None):
            return None
        st = self.syms.get(sym)
        hits = []
        for p in self.broker.pos.values():
            o = self.syms.get(p.symbol)
            if not st or not o or p.symbol == sym:
                continue
            try:
                r = corr(st, o, self.open_t, c.get("bars", 60))
            except RuntimeError:
                r = None
            if r is not None and r >= c.get("corr", 0.7):
                hits.append(p.name)
        return ", ".join(hits) if len(hits) >= c.get("max_same", 1) else None

    # ───────── 본 루프 ─────────
    def run(self):
        """같은 시장 로봇이 동시에 두 개 돌면(같은 주문이 두 번 나감) 안 되므로 시장별 잠금 파일로 하나만 돌게 함."""
        lockf = None
        try:
            import fcntl
            (ROOT / "logs").mkdir(exist_ok=True)
            lockf = open(ROOT / "logs" / f"run-{self.m}-{self.mode}.lock", "w")
            try:
                fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lockf.close()
                self.locked_out = True
                self.log("⚠️ 같은 시장 로봇이 이미 하나 돌고 있어서 두 번째는 켜지 않았어요 (같은 주문이 두 번 나가는 걸 막음)")
                self.phase = "종료됨"
                return None
        except ImportError:                      # 윈도우: 잠금 없이
            lockf = None
        try:
            return self._run()
        finally:
            if lockf:
                try:
                    lockf.close()
                except Exception:
                    pass

    def _run(self):
        live = self.mode == "live"
        self.phase = "시작 중"
        if live and not self.cfg["live"].get("enabled"):
            self.log("config.json 의 live.enabled 가 false 라 실전 모드를 시작하지 않아요. 연습 모드로 2주 이상 확인한 뒤 바꾸세요.")
            return
        self.log(f"===== 토스 단타 로봇 시작: {self.m} · {'실전' if live else '연습'} 모드 =====")
        if self.risk.capital:
            rc = self.cfg["risk"][self.m]
            self.log(f"오늘 투자금 {money(self.m, self.risk.capital)} 안에서 나눠 사요 · 한 종목 최대 {rc.get('max_position_pct', 40)}% · "
                     f"동시 {rc['max_positions']}종목 · 하루 {rc['max_trades_per_day']}번 · 하루 손실 한도 {money(self.m, self.risk.loss_limit)}")
        self.pick_account()
        if not self.session_times():
            return
        s = self.cfg["session"][self.m]
        no_entry_t, flatten_t = self.at(s["last_entry"]), self.at(s["flatten"])
        n = now(self.m)
        if n >= self.close_t:
            self.log("오늘 장이 이미 끝났어요.")
            return
        lead = max(3, int(self.cfg.get("premarket", {}).get("lead_min", 8)) if self.cfg.get("premarket", {}).get("enabled") else 3)
        if n < self.open_t - dt.timedelta(minutes=lead):
            w = (self.open_t - dt.timedelta(minutes=lead) - n).total_seconds()
            self.log(f"장 시작까지 {int(w // 3600)}시간 {int(w % 3600 // 60)}분 기다려요. 이 창(화면)을 켜 둔 채로 두세요.")
            self.phase = f"장 시작 기다리는 중 ({self.open_t.strftime('%m/%d %H:%M')} {'한국' if self.m == 'KR' else '뉴욕'} 시간 시작)"
            end = time.time() + max(0, w)
            next_pv = 0.0
            while time.time() < end:
                if self.stop_req:
                    self.log("장 시작 전에 종료했어요.")
                    return
                pmr = (self.cfg.get("daily_swing", {}).get("premarket_review") or {})
                if (pmr.get("enabled", True) and getattr(self, "_pm_done", "") != self.open_t.date().isoformat()
                        and now(self.m) >= self.open_t - dt.timedelta(minutes=pmr.get("lead_min", 25))):
                    try:
                        self.swing_premarket()
                    except Exception as e:
                        self.log(f"🌅 프리장 점검 오류: {e}")
                if time.time() >= next_pv:                   # 다음 장 후보가 나오면 판단표를 미리 보냄 (10분마다 확인)
                    next_pv = time.time() + 600
                    try:
                        self.swing_preview()
                    except Exception as e:
                        self.log(f"🧭 판단표 미리 보기 오류: {e}")
                time.sleep(min(1.0, end - time.time()) if end > time.time() else 0)

        self.broker = (LiveBroker(self.api, self.m, self.cfg, self.dir, self.log, self.notify) if live
                       else PaperBroker(self.m, self.cfg, self.dir, self.log, self.notify))
        self.broker.on_closed = self.on_closed
        self.shadow = None
        if not self.cfg["strategy"].get("intraday", True) and self.cfg.get("shadow", {}).get("enabled"):
            # 단타 '그림자': 실제로 사지는 않고, 단타 신호대로 샀다면 어땠을지 연습 장부에만 기록 (나중에 다시 켤지 판단용)
            sd = ROOT / "logs" / "shadow" / self.m
            sd.mkdir(parents=True, exist_ok=True)
            self.shadow = PaperBroker(self.m, self.cfg, sd, lambda msg: None, lambda msg: None)
            self.shadow_risk = Risk({**self.cfg, "market": self.m}, ROOT / "STOP_SHADOW_NEVER")
            self.shadow_risk.capital = self.risk.capital
            self._sh_t = 0.0
        self.broker.judge = self.stop_info
        self.broker.restore()
        if live:
            self.broker.verify_carried()
            try:
                self.adopt_holdings()
            except Exception as e:
                self.log(f"🧭 계좌 보유 이어받기 오류: {e}")
        self.setup_gate()
        self.perf_size()
        self.week_guard()
        self.load_stopped()
        try:
            self.swing_load()
        except Exception as e:
            self.log(f"🧭 스윙 후보 불러오기 오류: {e}")
        try:
            Bot.loop_notice(self)
            Bot.real_review_weekly(self)
        except Exception as e:
            self.log(f"🔁 점검 루프 알림 오류: {e}")
        self.dd_check(flatten=True)
        for p in self.broker.pos.values():
            self.syms.setdefault(p.symbol, SymState(p.symbol)).name = p.name

        cash = None
        if live:
            try:
                cash = f((self.api.buying_power("KRW" if self.m == "KR" else "USD") or {}).get("cashBuyingPower"))
                self.log(f"매수 가능 금액: {money(self.m, cash or 0)}")
                need = self.risk.capital or self.cfg["risk"][self.m].get("budget_per_trade", 0)
                if self.m == "KR" and cash is not None and self.risk.capital and cash < self.risk.capital:
                    msg = f"계좌의 매수 가능 금액({money(self.m, cash)})이 투자금 설정({money(self.m, self.risk.capital)})보다 적어요 → 그 안에서만 사요"
                    self.log(msg)
                    if cash < self.risk.capital * 0.5:
                        self.notify("⚠️ " + msg + ". 현금이 모자라면 신호가 나도 건너뛰어요 (몇 주짜리 매수는 안 함).")
                if self.risk.capital:
                    need = min(need, self.risk.available(self.broker.invested()) or 0)   # 이미 산(들고 온) 금액은 빼고 비교
                if self.m == "US" and (cash or 0) < need:
                    self.log(f"달러 잔고({money(self.m, cash or 0)})가 지금 더 살 수 있는 금액({money(self.m, need)})보다 적어요. 토스의 '원화로 바로 주문(자동 환전)'이 켜져 있으면 그대로 주문되고, "
                             "아니면 주문이 거절돼요 → 토스 앱에서 환전하거나 자동 환전을 켜 주세요.")
                    cash = None
            except TossError as e:
                self.log(f"매수 가능 금액 조회 실패: {e}")
        self._cash_track = cash is not None          # 현금을 실제 계좌로 맞춰 가며 쓰는지 (미장 자동 환전이면 안 씀)
        self._cash_t = time.time()

        self.build_universe()
        self.refresh_bars(list(dict.fromkeys(self.universe + list(self.broker.pos))))
        from .stream import MultiStream
        self.stream = MultiStream(self.api, self.m, self.on_trade, self.broker.on_order_event if live else None, self.log,
                                  n=self.cfg["universe"].get("stream_connections", 2))
        self.stream.start()
        time.sleep(2)
        self.stream.set_codes(list(dict.fromkeys(list(self.broker.pos) + self.universe)))
        if self.broker.carried():
            self.notify("📦 전날부터 들고 있는 종목: " + ", ".join(
                f"{p.name} {p.qty}주(손절선 {money(self.m, p.sl)})" for p in self.broker.carried()))
        self.notify(f"▶️ 로봇 시작 ({self.m}, {'실전' if live else '연습'}) · 후보 {len(self.universe)}개")

        last_status, last_full = 0.0, 0.0
        self._bg_stop = False
        import threading
        inline_bg = os.getenv("TOSSBOT_NO_BG") == "1"     # 가짜 시계 시험: 같은 일을 메인 루프에서 1초마다
        last_bg = 0.0
        if not inline_bg:
            threading.Thread(target=self._bg_loop, args=(no_entry_t,), daemon=True).start()
            threading.Thread(target=self._news_loop, args=(no_entry_t,), daemon=True).start()
        try:
            while True:
                n = now(self.m)
                self.beat = time.time()                      # 멈춤 감시용 (app 의 감시 스레드가 봄)
                today = n.date().isoformat()
                self.phase = ("장중 매매" if n < no_entry_t else "새 진입 없이 보유분 관리" if n < flatten_t else "마감 정리")
                self.loop_on = True
                if self.stop_req and self.handle_stop(today):
                    break
                etc = self.cfg.get("etf_long", {})
                if (etc.get("enabled") and getattr(self, "etf_done", "") != today and not self.stop_req and n < no_entry_t
                        and n >= self.open_t + dt.timedelta(minutes=etc.get("delay_min", 10))):
                    self.etf_done = today
                    try:
                        if etc.get("mode", "advise") == "auto":
                            cash = self.fresh_cash(cash, force=True)
                            spent = self.etf_buy(n, cash)
                            if cash is not None:
                                cash -= spent
                        else:
                            self.etf_advise(n)
                    except Exception as e:
                        self.log(f"🏦 ETF 오류: {e}")
                swc = self.cfg.get("daily_swing", {})
                if (self.sw_picks and self.sw_done != today and not self.stop_req and n < no_entry_t
                        and n >= self.open_t + dt.timedelta(minutes=swc.get("entry_delay_min", 1)
                                                            + swc.get("open_entry_window_min", 45))):
                    self.sw_done = today              # 로봇이 늦게 켜졌으면 장 시작 매수는 건너뜀 → 그 후보는 마감 15분 전에 다시 봄
                    self.log("🧭 장 시작 매수 시간이 지나서 켜짐 — 장 시작 후보는 마감 15분 전에 다시 점검")
                if (self.sw_picks and self.sw_done != today and not self.stop_req and n < no_entry_t
                        and n >= self.open_t + dt.timedelta(minutes=swc.get("entry_delay_min", 1))):
                    self.sw_done = today
                    cash = self.fresh_cash(cash, force=True)
                    try:
                        spent = self.swing_enter(cash)
                        if cash is not None:
                            cash -= spent
                    except Exception as e:
                        self.log(f"🧭 스윙 매수 오류: {e}")
                # 아침 전 종목 다시 고르기(장 시작 뒤 오늘 흐름으로 전체 종목을 다시 본 후보)가 도착하면 확신 종목을 한 번 더 봄
                if (self.sw_picks and self.sw_done == today and getattr(self, "sw_am_done", "") != today and not self.stop_req
                        and n < no_entry_t and n >= self.open_t + dt.timedelta(minutes=30) and swc.get("morning_rescan", True)
                        and time.time() >= getattr(self, "_am_next", 0)):
                    self._am_next = time.time() + 300
                    if n >= self.open_t + dt.timedelta(minutes=swc.get("morning_rescan_max_min", 150)):
                        self.sw_am_done = today
                    elif Bot.live_refresh(self, max_age=2.5, after_open=True):
                        self.sw_am_done = today
                        cash = self.fresh_cash(cash, force=True)
                        try:
                            spent = self.swing_enter(cash)
                            if cash is not None:
                                cash -= spent
                        except Exception as e:
                            self.log(f"🧭 아침 다시 고르기 매수 오류: {e}")
                while self.buy_reqs:
                    xb = self.buy_reqs.pop(0)
                    if n >= no_entry_t:
                        self.notify(f"🧭 {xb['name']} 봇 매수 요청 — 새 진입 시간이 지나서 안 샀어요")
                        continue
                    cash = self.fresh_cash(cash, force=True)
                    try:
                        spent = Bot.manual_buy(self, xb, cash)
                        if cash is not None:
                            cash -= spent
                    except Exception as e:
                        self.notify(f"🧭 {xb['name']} 봇 매수 오류: {e}")
                while self.sell_reqs:
                    sym = self.sell_reqs.pop(0)
                    st = self.syms.get(sym)
                    if sym in self.broker.pos and self.broker.pos[sym].state == "open":
                        self.broker.flatten(sym, "직접 매도 (텔레그램 보유종목)", st.last if st else None, force=True)
                undecided = [p for p in self.broker.pos.values() if p.extra.get("carry_done") != today]
                # 장 마감 무렵 매수가 아직 안 끝났으면 기다림 (보유 종목이 하나도 없는 날 마감 매수를 건너뛰던 문제 고침)
                close_pending = (self.sw_picks and getattr(self, "sw_close_done", "") != today and not self.stop_req
                                 and n < flatten_t + dt.timedelta(minutes=10)
                                 and any(v == "close" for v in (swc.get("entry_at") or {}).values()))
                if n >= self.close_t or (n >= flatten_t and not undecided and not close_pending):
                    break
                if self.risk.kill_switch() and self.broker.open_count():
                    for sym in list(self.broker.pos):
                        self.broker.flatten(sym, "긴급 정지", self.syms.get(sym).last if sym in self.syms else None)
                if inline_bg and time.time() - last_bg >= 1.0:
                    self._bg_tick(no_entry_t)
                    last_bg = time.time()
                # 진입
                if self.open_t <= n < no_entry_t and not self.stop_req and self.cfg["strategy"].get("intraday", True):
                    why = self.risk.can_enter(self.broker.open_count(), self.broker.invested())
                    if why is None:
                        cands = []
                        # 방금 체결이 들어온 종목은 바로(0.2초마다), 나머지는 3초마다 전체 점검
                        changed, self.dirty = self.dirty, set()
                        full = time.time() - last_full > 3
                        if full:
                            last_full = time.time()
                        for sym in (self.universe if full else [x for x in self.universe if x in changed]):
                            if sym in self.broker.pos or sym not in self.syms or sym in self.stopped:
                                continue
                            try:
                                sig = entry_signal(self.syms[sym], n, self.open_t, self.cfg, self.ctx)
                            except RuntimeError:          # 실시간 스레드가 분봉을 고치는 중 → 다음 차례에
                                sig = None
                            if sig:
                                cands.append((sig["score"], sym, sig))
                        mk = self.market_move()
                        weak = self.cfg.get("market_filter", {}).get("weak_pct", -0.7)
                        if mk is not None and mk <= weak and cands:
                            # 시장 전체가 빠지는 중 → 돌파 매수는 잘 실패함. 실적 서프라이즈만 예외
                            if time.time() - getattr(self, "_weak_log", 0) > 600:
                                self._weak_log = time.time()
                                self.log(f"🌧 시장 전체가 약해요(최근 15분 평균 {mk:+.1f}%) → 실적 서프라이즈 말고는 새로 안 사요")
                            cands = [x for x in cands if x[1] in self.ctx.get("earn_good", {})]
                        cands.sort(key=lambda x: -x[0])          # 여러 종목이 동시에 신호 → 가장 센 것부터
                        if cands:
                            cash = self.fresh_cash(cash)
                        for _, sym, sig in cands:
                            st = self.syms[sym]
                            same = self.same_group(sym)
                            if same:
                                if time.time() - st.__dict__.get("_grp_log", 0) > 900:
                                    st._grp_log = time.time()
                                    self.log(f"🔗 {st.name or sym} 은 보유 중인 {same} 와 같이 움직여서 안 사요 (한 테마에 몰빵 방지)")
                                continue
                            est = add_ticks(self.m, st.last, self.cfg["live"]["buy_slip_ticks"] if live else 1)
                            tp, sl, R, a = exit_levels(self.m, est, sig, self.cfg, st, self.open_t)
                            sl_exec = add_ticks(self.m, sl, -2 if live else -1)
                            fee = self.cfg["fees"][self.m]
                            w = self.risk.strength(sig)
                            if not self.risk.grade_ok(sig):
                                if time.time() - st.__dict__.get("_grade_log", 0) > 900:
                                    st._grade_log = time.time()
                                    self.log(f"🙅 {st.name or sym} 신호는 났지만 확신도 {sig.get('grade')}(기준 {self.risk.c.get('min_grade', 'B')} 이상)라 안 사요 · {sig['reason'][:40]}")
                                continue
                            if getattr(st, "etf", False):
                                w = min(w, self.cfg["universe"].get("etf_weight", 0.6))   # 레버리지 ETF 는 작게
                            qty = self.risk.qty_for(est, sl_exec, cash, (fee["buy_pct"] + fee["sell_pct"] + fee["sell_tax_pct"]) / 100,
                                                    self.broker.invested(), w)
                            if qty < 1:
                                if getattr(self.risk, "last_skip", None) == "cash" and getattr(self, "_cash_note", "") != today:
                                    self._cash_note = today
                                    self.notify(f"💸 {st.name or sym} 매수 신호가 났지만 계좌 매수 가능 금액({money(self.m, cash or 0)})이 "
                                                f"모자라 몇 주밖에 못 사서 건너뛰었어요. 현금을 채우거나 '투자금' 설정을 실제 현금에 맞춰 주세요.")
                                continue
                            why = (f"확신도 {sig['grade']}(비중 {w:g}배) · " if sig.get("grade") else "") + sig["reason"]
                            p = self.broker.enter(sym, st.name or sym, st.last, qty, tp, sl, why, r=R, atr=a)
                            if not p and sym in getattr(self.broker, "rejected", {}):
                                st.excluded = f"주문 불가({self.broker.rejected[sym]})"   # 교육·예탁금 등 → 오늘은 다시 안 시도
                            if p:
                                p.extra["setup"], p.extra["grade"] = sig.get("setup"), sig.get("grade")
                                if sig.get("setup") == "gapdown" and self.cfg["strategy"]["setups"].get("gapdown", {}).get("hold_eod", True):
                                    p.extra["hold_eod"] = True
                                if getattr(st, "etf", False):
                                    p.extra["etf"] = True
                                self.mark_swing(p, sig)
                                st.entries_today += 1
                                self.risk.trades += 1
                                if cash is not None:
                                    cash -= p.entry * p.qty
                                break  # 한 번에 한 종목씩
                if self.shadow is not None:
                    try:
                        self.shadow_tick(n, no_entry_t, flatten_t, today)
                    except Exception as e:
                        if time.time() - getattr(self, "_sh_err", 0) > 600:
                            self._sh_err = time.time()
                            self.log(f"👻 단타 그림자 기록 오류: {e}")
                # 물타기(시험용, 기본 꺼짐): 차트 추세가 좋고 시장이 안정적인데 살짝 밀렸다가 반등하는 봉이 나오면 1번만 소량 추가
                ad = self.cfg.get("average_down", {})
                if ad.get("enabled") and self.open_t <= n < no_entry_t and not self.stop_req and not self.risk.halted:
                    mk = self.market_move()
                    for sym, p in list(self.broker.pos.items()):
                        st = self.syms.get(sym)
                        if (not st or not st.last or p.state != "open" or p.extra.get("avgdown") or p.extra.get("adds")
                                or p.extra.get("etf") or p.sl >= p.entry):
                            continue
                        R = p.extra.get("R") or (p.entry - p.sl)
                        if not (p.entry - R * ad.get("max_drop_R", 0.8) <= st.last <= p.entry - R * ad.get("drop_R", 0.5)):
                            continue                                  # 산 값보다 0.5~0.8R 밀린 구간에서만
                        if mk is not None and mk < ad.get("min_market", -0.2):
                            continue                                  # 시장 전체가 약하면 안 함
                        d = st.daily or {}
                        if ad.get("need_uptrend", True) and not (d.get("ma20") and d.get("close", 0) >= d["ma20"]):
                            continue                                  # 일봉 추세가 좋은 종목만
                        bars = session_bars(st, self.open_t)
                        cur = n.replace(second=0, microsecond=0)
                        done = [b for b in bars if b.t < cur]
                        if len(done) < 2 or not (done[-1].c > done[-1].o and done[-1].c > done[-2].c):
                            continue                                  # 반등하는 양봉이 나와야
                        q = max(1, int((p.extra.get("q0") or p.qty) * ad.get("ratio", 0.5)))
                        cap = self.risk.capital or 0
                        max_loss = cap * self.cfg["risk"][self.m].get("risk_per_trade_pct", 1.0) / 100 * ad.get("loss_mult", 1.6)
                        while q > 0 and ((p.entry * p.qty + st.last * q) / (p.qty + q) - p.sl) * (p.qty + q) > max_loss:
                            q -= 1
                        budget = self.risk.available(self.broker.invested()) or 0
                        q = min(q, int(budget // st.last))
                        if q >= 1:
                            p.extra["avgdown"] = 1
                            self.broker.add(p, q, st.last, f"물타기(산 값 {(st.last / p.entry - 1) * 100:+.1f}%, 추세 좋음·시장 안정·반등 봉)")
                # 추가 매수(불타기): 잃지 않는 상태 + 같은 종목에 새 강한 신호
                if (self.open_t <= n < no_entry_t and not self.stop_req and not self.risk.kill_switch() and not self.risk.halted
                        and self.cfg["strategy"].get("intraday", True)):
                    for sym, p in list(self.broker.pos.items()):
                        st = self.syms.get(sym)
                        if not st or not st.last or not self.broker.can_add(p, st.last):
                            continue
                        sig = entry_signal(st, n, self.open_t, self.cfg, self.ctx, adding=True)
                        if not sig or sig.get("setup") not in self.cfg.get("pyramid", {}).get("setups", ["orb", "hod", "vwap", "open"]):
                            continue
                        w = self.risk.strength(sig)
                        if w < self.cfg["pyramid"].get("min_weight", 1.0):
                            continue                     # 확신도 B 이상일 때만
                        cap = self.risk.capital or 0
                        max_loss = cap * min(self.cfg["risk"][self.m].get("risk_per_trade_pct", 1.0) * w,
                                             self.cfg["risk"][self.m].get("risk_per_trade_pct_cap", 1.8)) / 100 if cap else \
                            self.cfg["risk"][self.m].get("max_loss_per_trade", 0)
                        budget = self.risk.available(self.broker.invested()) if cap else self.cfg["risk"][self.m].get("budget_per_trade", 0)
                        if cash is not None:
                            budget = min(budget, cash * 0.95)
                        q = self.broker.add_qty(p, st.last, max_loss, budget or 0)
                        if q >= 1:
                            self.broker.add(p, q, st.last, f"확신도 {sig.get('grade', '')} · {sig['reason']}")
                            if cash is not None:
                                cash -= st.last * q
                # 장 마감 정리 15분 전: 정리될 종목마다 이유·판단 재료를 알려 주고 '잠금' 여부를 고를 수 있게
                pre_min = self.cfg.get("carry", {}).get("pre_alert_min", 15)
                if pre_min and flatten_t - dt.timedelta(minutes=pre_min) <= n < flatten_t and getattr(self, "_pre_alert", "") != today:
                    self._pre_alert = today
                    try:
                        self.pre_close_alert(flatten_t)
                    except Exception as e:
                        self.log(f"장 마감 전 알림 오류: {e}")
                # 저점 매수 등 '장 마감 무렵 매수' 방식: 그날 장중 흐름을 본 뒤 정리 시각에 삼
                if (self.sw_picks and getattr(self, "sw_close_done", "") != today and not self.stop_req and n >= flatten_t
                        and n < flatten_t + dt.timedelta(minutes=10)
                        and any(v == "close" for v in (swc.get("entry_at") or {}).values())):
                    self.sw_close_done = today
                    cash = self.fresh_cash(cash, force=True)
                    try:
                        Bot.live_refresh(self)
                    except Exception as e:
                        self.log(f"🧭 장중 다시 고르기 반영 오류: {e}")
                    try:
                        spent = self.swing_enter(cash, "close")
                        if cash is not None:
                            cash -= spent
                    except Exception as e:
                        self.log(f"🧭 스윙 마감 매수 오류: {e}")
                # 장 마감 전 정리
                if n >= flatten_t:
                    for sym, p in list(self.broker.pos.items()):
                        if p.extra.get("carry_done") == today:
                            continue
                        st = self.syms.get(sym)
                        last = st.last if st else None
                        if p.extra.get("etf_long"):
                            p.extra["carry_done"] = today       # 장기 ETF: 그대로 보유
                            continue
                        if p.state == "open" and p.extra.get("sw"):
                            try:
                                self.swing_daily(p, last)
                            except Exception as e:
                                self.log(f"🧭 스윙 점검 오류 {p.name}: {e}")
                            p.extra["carry_done"] = today
                            self.broker.save_state()
                            continue
                        if p.state == "open" and p.extra.get("locked"):
                            self.broker.carry(p, last or p.entry)        # 잠금: 장 마감 정리 안 함(손절 주문은 유지·연장)
                            p.extra["carry_done"] = today
                            self.broker.save_state()
                            continue
                        if p.state == "open":
                            vw = vwap(session_bars(st, self.open_t)) if st else None
                            strong = False
                            try:
                                sb = session_bars(st, self.open_t) if st else []
                                if sb and st.prev_close and last:
                                    hi = max(b.h for b in sb)
                                    c = self.cfg.get("carry", {})
                                    strong = ((last / st.prev_close - 1) * 100 >= c.get("strong_day_pct", 3.0)
                                              and last >= hi * (1 - c.get("strong_near_high_pct", 2.0) / 100)
                                              and (not vw or last >= vw))
                            except RuntimeError:
                                pass
                            why = self.broker.carry_check(p, last, vw, strong)
                            if why is None and sym in self.ctx.get("earn_block", ()) and sym not in self.ctx.get("earn_good", {}):
                                why = "실적 발표가 오늘·내일이라 밤사이 위험 (결과에 따라 크게 튈 수 있음)"
                            if why is None:
                                self.broker.carry(p, last)
                                p.extra["carry_done"] = today
                                self.broker.save_state()
                                continue
                            self.broker.flatten(sym, f"장 마감 전 정리 ({why})", last)
                        else:
                            self.broker.flatten(sym, "장 마감 전 정리", last)
                # 1분마다 실제 계좌와 맞춰 보기(직접 판 종목 반영)
                if live and time.time() - getattr(self, "_rc_t", 0) > 60:
                    self._rc_t = time.time()
                    try:
                        self.broker.reconcile()
                    except Exception as e:
                        self.log(f"계좌 맞춰 보기 오류: {e}")
                    # 매수 가능 금액도 실제 계좌에서 다시 읽음 — 예전엔 장 시작 때 한 번 읽고 살 때마다 빼기만 해서
                    # 팔아서 돌아온 돈이 반영되지 않았음 (10/30 오후 삼성중공업 '계획 2주'의 원인)
                    if cash is not None:
                        try:
                            fresh = f((self.api.buying_power("KRW" if self.m == "KR" else "USD") or {}).get("cashBuyingPower"))
                            if fresh is not None:
                                cash = fresh
                        except TossError:
                            pass
                # 거래가 없어도(상한가에 묶임 등) 15초마다 보유 종목의 손절선·이익 잠금을 다시 계산
                if time.time() - getattr(self, "_mg_t", 0) > 15:
                    self._mg_t = time.time()
                    for sym, p in list(self.broker.pos.items()):
                        stx = self.syms.get(sym)
                        if p.state == "open" and stx and stx.last:
                            try:
                                self.broker.on_price(sym, stx.last, n)
                            except Exception as e:
                                self.log(f"보유 점검 오류 {sym}: {e}")
                            try:
                                Bot.profit_take(self, p, stx.last)      # (켜져 있으면) +N% 찍을 때 일부 익절
                                Bot.profit_lock(self, p, stx.last)      # 오른 만큼 손절선을 올려 이익 잠금
                                Bot.loss_watch(self, p, stx.last)       # -3% 를 처음 넘으면 바로 검토해서 알림
                            except Exception as e:
                                self.log(f"익절·손실 점검 오류 {sym}: {e}")
                    try:
                        Bot.review_tick(self)                           # 30분마다 앱에 보여 줄 보유 판정 갱신
                    except Exception as e:
                        self.log(f"보유 검토 오류: {e}")
                if self.shadow is not None and time.time() - getattr(self, "_shp_t", 0) > 15:
                    self._shp_t = time.time()
                    for sym, p in list(self.shadow.pos.items()):
                        stx = self.syms.get(sym)
                        if stx and stx.last:
                            try:
                                self.shadow.on_price(sym, stx.last, n)
                            except Exception:
                                pass
                self.broker.poll(n, {k: v.last for k, v in self.syms.items()})
                if time.time() - last_status > 300:
                    self.status()
                    last_status = time.time()
                time.sleep(0.2)
        except KeyboardInterrupt:
            self.log("Ctrl+C 로 멈춤")
            if self.broker.open_count():
                self.log(f"⚠️ 아직 보유 중 {self.broker.open_count()}건 — 실전이면 토스 서버의 익절·손절 조건주문은 그대로 살아 있어요.")
        finally:
            self.loop_on = False
            if self.stream:
                self._bg_stop = True
                self.stream.stop()
            self.summary()
            self.phase = "종료됨"

    # ───────── 시작/종료 버튼 처리 ─────────
    def handle_stop(self, today: str) -> bool:
        """종료 요청 처리. 끝내도 되면 True."""
        if self.stop_req == "keep":
            for p in list(self.broker.pos.values()):
                if p.state == "pending":
                    self.broker.flatten(p.symbol, "종료", None)
                elif p.state == "open" and p.extra.get("hold_done") != today:
                    self.broker.hold(p)
                    p.extra["hold_done"] = today
            pend = [p for p in self.broker.pos.values() if p.state in ("pending", "closing")]
            if pend and time.time() - self.__dict__.setdefault("_stop_t", time.time()) < 60:
                return False
            self.log("⏹ 종료: 보유 종목은 토스 서버의 시장가 손절 조건주문을 걸어 둔 채로 끝내요.")
            return True
        # 전부 팔고 끝
        if not self.__dict__.get("_sell_sent"):
            self._sell_sent = time.time()
            self.log("⏹ 종료: 보유 종목을 전부 시장가로 팔고 끝내요.")
            for sym in list(self.broker.pos):
                st = self.syms.get(sym)
                self.broker.flatten(sym, "종료 버튼 — 전부 정리", st.last if st else None)
        if not [p for p in self.broker.pos.values() if not p.extra.get("etf_long")]:
            return True
        if time.time() - self._sell_sent > 90:
            self.log("⚠️ 90초 안에 다 팔리지 않았어요. 토스 앱에서 직접 확인하세요.")
            return True
        return False

    def snapshot(self) -> dict:
        """화면에 보여 줄 현재 상태."""
        b = getattr(self, "broker", None)
        pos = []
        if b:
            for p in b.pos.values():
                st = self.syms.get(p.symbol)
                last = st.last if st and st.last else None
                pos.append({"symbol": p.symbol, "name": p.name, "qty": p.qty, "entry": p.entry, "last": last,
                            "pct": (last / p.entry - 1) * 100 if last else None, "sl": p.sl,
                            "sl_pct": (p.sl / p.entry - 1) * 100, "state": p.state,
                            "carry": bool(p.extra.get("carry")), "locked": bool(p.extra.get("locked")), "days": len(p.extra.get("days_held", [])),
                            "partial": p.partial_done})
        closed = [{"name": p.name, "qty": p.qty, "entry": p.entry, "exit": p.exit_price, "pnl": p.pnl,
                   "pct": (p.exit_price / p.entry - 1) * 100 if p.exit_price else None, "why": p.exit_reason}
                  for p in (b.closed if b else [])]
        return {"mode": self.mode, "market": self.m, "phase": self.phase, "stop_req": self.stop_req,
                "positions": pos, "closed": closed, "realized": self.risk.realized, "trades": self.risk.trades,
                "halted": self.risk.halted, "capital": self.risk.capital,
                "invested": b.invested() if b else 0, "available": self.risk.available(b.invested() if b else 0),
                "loss_limit": self.risk.loss_limit, "universe": len(getattr(self, "universe", []) or []),
                "ws": bool(self.stream and getattr(self.stream, "connected", True)), "log": list(self.log.recent)[-80:]}

    def status(self):
        ps = ", ".join(f"{p.name} {p.qty}주({p.state})" for p in self.broker.pos.values()) or "없음"
        self.log(f"[상태] 보유 {ps} · 오늘 매매 {self.risk.trades}회 · 확정 손익 {money(self.m, self.risk.realized)}"
                 + (f" · 멈춤: {self.risk.halted}" if self.risk.halted else ""))

    def summary(self):
        c = self.broker.closed if hasattr(self, "broker") else []
        win = sum(1 for p in c if p.pnl > 0)
        txt = (f"📊 오늘 결과 ({self.m}, {'실전' if self.mode == 'live' else '연습'}): 매매 {len(c)}회 · 익절 {win} · 손절 {len(c) - win} · "
               f"손익 {money(self.m, sum(p.pnl for p in c))}")
        for p in c:
            txt += f"\n- {p.name} {(p.exit_price / p.entry - 1) * 100:+.2f}% {money(self.m, p.pnl)} ({p.exit_reason})"
        hold = self.broker.carried() if hasattr(self, "broker") else []
        if hold:
            txt += "\n🌙 들고 가는 종목:"
            for p in hold:
                st = self.syms.get(p.symbol)
                last = st.last if st and st.last else p.entry
                txt += (f"\n- {p.name} {p.qty}주 · 산 가격 {money(self.m, p.entry)} · 지금 {(last / p.entry - 1) * 100:+.1f}% · "
                        f"손절선 {money(self.m, p.sl)} · {len(p.extra.get('days_held', []))}일째")
        self.log(txt)
        self.notify(txt)
        out = ROOT / "logs" / f"summary-{self.m}-{now(self.m).date().isoformat()}-{self.mode}.json"
        out.write_text(json.dumps({"mode": self.mode, "market": self.m, "trades": len(c), "wins": win,
                                   "pnl": sum(p.pnl for p in c),
                                   "items": [{"symbol": p.symbol, "name": p.name, "qty": p.qty, "entry": p.entry,
                                              "exit": p.exit_price, "pnl": p.pnl, "reason": p.reason,
                                              "exit_reason": p.exit_reason} for p in c]},
                              ensure_ascii=False, indent=1), encoding="utf-8")


def check():
    load_env()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    api = TossAPI(os.getenv("TOSS_CLIENT_ID", ""), os.getenv("TOSS_CLIENT_SECRET", ""), ROOT / ".token.json")
    print("1) 토큰 발급 …", end=" ")
    try:
        api.token()
        print("OK")
    except TossError as e:
        print(f"실패 {e}")
        if e.status == 403:
            print("   → 허용 IP 에 이 PC 의 인터넷 주소가 없어요. 토스증권 WTS 설정 > Open API > 허용 IP 관리 에서 추가하세요.")
        else:
            print("   → .env 의 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 를 확인하세요.")
        return
    print("2) 계좌 …", end=" ")
    accs = api.accounts() or []
    print(", ".join(f"{a.get('accountType')} ****{str(a.get('accountNo',''))[-4:]} (seq {a.get('accountSeq')})" for a in accs) or "없음")
    if accs:
        api.account_seq = int(accs[0]["accountSeq"])
    for m in markets_of(cfg):
        sym = "005930" if m == "KR" else "AAPL"
        print(f"\n[{FLAG[m]} {'국내' if m == 'KR' else '미국'}]")
        print(f"3) 현재가 {sym} …", (api.prices([sym]) or [{}])[0].get("lastPrice"))
        cal = api.calendar(m) or {}
        print("4) 오늘 장 …", "열림" if ((cal.get("today") or {}).get("integrated") if m == "KR" else (cal.get("today") or {}).get("regularMarket")) else "휴장",
              "· 다음 영업일", (cal.get("nextBusinessDay") or {}).get("date"))
        try:
            bp = api.buying_power("KRW" if m == "KR" else "USD")
            print("5) 매수 가능 금액 …", bp.get("cashBuyingPower"), "원" if m == "KR" else "달러")
        except TossError as e:
            print("5) 매수 가능 금액 조회 실패", e)
        try:
            r = api.rankings("MARKET_TRADING_AMOUNT", m, "realtime", 5)
            print("6) 거래대금 상위 …", [x.get("symbol") for x in r.get("rankings", [])])
        except TossError as e:
            print("6) 랭킹 조회 실패", e)
    print("\n✅ 연결 확인 끝 (주문은 하지 않았어요)")


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "paper").lower()
    if mode == "check":
        check()
    elif mode in ("paper", "live"):
        # 화면 없이 실행: 시장을 두 번째 인자로 (예: python -m bot.main live US). 없으면 설정의 모든 시장을 차례로
        import threading
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        ms = [sys.argv[2].upper()] if len(sys.argv) > 2 else markets_of(cfg)
        ts = [threading.Thread(target=lambda mk=mk: Bot(mode, mk).run()) for mk in ms]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    else:
        print("사용법: python -m bot.main [check|paper|live]")
