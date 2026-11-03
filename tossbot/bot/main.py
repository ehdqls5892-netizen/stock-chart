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

    def swing_load(self):
        if not self.swing_on() or os.getenv("TOSSBOT_OFFLINE") == "1":
            return
        c = self.cfg["daily_swing"]
        d = SW.load_picks(c["picks_url"], self.m, self.open_t.date(), self.log)
        if not d:
            return
        on = set(self.swing_on())
        below = set(c.get("below_setups", []))
        # 상승 국면엔 추세 방식만, 하락 국면엔 저점 매수 방식만
        d["picks"] = [x for x in d.get("picks", []) if x.get("setup") in on
                      and ((x.get("setup") in below) != bool(d.get("regime")))]
        self.sw_picks = d
        self.sw_regime = d.get("regime")                 # 추가 매수 판단용 (시장 좋을 때만)
        ix = d.get("index") or {}
        name = "코스피" if self.m == "KR" else "S&P500(SPY)"
        if not d.get("regime") and not d["picks"]:
            msg = (f"🧭 스윙: {name} 가 50일선 아래(하락 국면 · {ix.get('close')} < {ix.get('ma50')}) → 오늘은 새로 안 사요. "
                   f"보유분 손절·점검만 해요." + (" (하락장 저점 매수 신호도 없음)" if below & on else ""))
        elif not d["picks"]:
            msg = f"🧭 스윙: {d['date']} 신호 종목 없음 (시장 국면 좋음 · {name} 50일선 위)"
        else:
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
        return (c.get("entry_at") or {}).get(x.get("setup"), "open")

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
        on, below = set(self.swing_on()), set(c.get("below_setups", []))
        d["picks"] = [x for x in d.get("picks", []) if x.get("setup") in on and ((x.get("setup") in below) != bool(d.get("regime")))]
        old_b, old_p = getattr(self, "broker", None), self.sw_picks
        tmp = PaperBroker(self.m, self.cfg, self.dir, lambda *a: None, lambda *a: None)
        tmp.restore()
        try:
            self.broker, self.sw_picks = tmp, d
            self.load_stopped()
            txt = self.swing_table_text() if d["picks"] else (
                f"🧭 {d['date']} 종가 기준 새로 살 후보 없음 (시장 {'좋음' if d.get('regime') else '나쁨'})")
            self._pv_table = {r[0]["code"]: r[1] for r in getattr(self, "sw_table", [])}
        finally:
            self.broker, self.sw_picks = old_b, old_p
        self._pv_sent = key
        when = self.open_t.strftime("%m/%d %H:%M")
        self.notify(f"📋 [미리 보기] 다음 장({when} {'한국' if self.m == 'KR' else '뉴욕'} 시간) 매수 판단표\n{txt}\n"
                    f"→ 장 시작 몇 분 전에 최종 판단표를 다시 보내요. 그사이 바뀌는 건 보유 종목 변화(손절 등)뿐이에요.")

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
               f"{ix.get('close')} vs 50일선 {ix.get('ma50')}) — 후보 {len(d.get('picks', []))}개"]
        for st, icon in (("매수", "✅ 매수 예정"), ("예비", "⏭ 예비 (예정 종목이 시가 조건에 걸리면 순서대로)")):
            g = [r for r in rows if r[1] == st]
            if g:
                out.append(icon)
                for x, _, why, q in g:
                    out.append(f"- {x['name']} ≈{q}주 × {money(self.m, x['close'])} · 손절 {money(self.m, x['stop'])} "
                               f"· {when.get(Bot.swing_timing(self, x, d), '')} · {x['reason']}" + (f" · {why}" if why else ""))
        g = [r for r in rows if r[1] == "탈락"]
        if g:
            out.append("❌ 안 삼")
            out += [f"- {x['name']}: {why}" for x, _, why, q in g[:12]]
            if len(g) > 12:
                out.append(f"- 그 밖 {len(g) - 12}개")
        out.append("당일엔 시가가 손절선 아래·10% 넘게 갭, (마감 매수는) 장중 손절선 닿음, 현금 부족일 때만 결과가 달라져요.")
        return "\n".join(out)

    def swing_enter(self, cash, when: str = "open", dry: bool = False) -> float:
        """when: open(장 시작 직후) / close(장 마감 15분 전, 그날 흐름을 보고) — 방식별로 config entry_at.
        dry: 주문 없이 어제 종가로 같은 판단을 해서 판단표(self.sw_table)만 만듦 — 장 전에 미리 알리는 용도."""
        d, c = self.sw_picks or {}, self.cfg["daily_swing"]
        table = self.sw_table = []

        def skip(x, why, loud=False):
            if dry:
                table.append((x, "탈락", why, 0))
            elif loud:
                self.notify(f"🧭 {x['name']} 건너뜀 — {why}")
            else:
                self.log(f"🧭 {x['name']} 안 사요 — {why}")
        if not d.get("picks"):
            return 0.0
        if self.risk.kill_switch() or self.risk.halted:
            self.log(f"🧭 스윙 매수 건너뜀: {self.risk.halted or '긴급 정지'}")
            return 0.0
        have = [p for p in self.broker.pos.values() if p.extra.get("sw") and p.state in ("pending", "open", "closing")]
        slots = c.get("max_pos", 6) - len(have)
        below = set(c.get("below_setups", []))
        ec = c.get("earnings") or {}
        today = now(self.m).date()
        cands = []
        for x in d["picks"]:
            if (x.get("setup") in below) == bool(d.get("regime")):
                continue
            if x["code"] in self.broker.pos:
                skip(x, "이미 보유 중")
            elif x["code"] in self.stopped:
                skip(x, "최근 손절한 종목 (며칠 재매수 금지)")
            else:
                cands.append(x)
        later = [] if dry else [x for x in cands if Bot.swing_timing(self, x, d) != when]
        cands = cands if dry else [x for x in cands if Bot.swing_timing(self, x, d) == when]
        if when == "open" and later and slots > 0:
            self.notify(f"🧭 스윙 후보 {len(later)}개({', '.join(x['name'] for x in later[:5])})는 장 시작 직후 말고 "
                        f"장 마감 15분 전에 오늘 흐름을 보고 살게요. 장중에 손절선까지 밀리면 안 사요.")
        # 실적이 보유 기간 안에 없는 종목 먼저 (점수 순서는 유지)
        cands.sort(key=lambda x: SW.earn_plan(x, today, 100.0, 90.0, ec, self.m)[0] != "ok")
        cands = cands if dry else cands[: max(0, slots) * 3]
        sec_n = {}
        for p0 in have:
            if p0.extra.get("sector"):
                sec_n[p0.extra["sector"]] = sec_n.get(p0.extra["sector"], 0) + 1
        if (slots <= 0 and not dry) or not cands:
            return 0.0
        if dry:
            px, cash = {x["code"]: float(x["close"]) for x in cands}, None
        else:
            try:
                px = {x.get("symbol"): f(x.get("lastPrice")) for x in (self.api.prices([x["code"] for x in cands]) or [])}
            except TossError as e:
                self.log(f"🧭 스윙 시세 조회 실패: {e}")
                return 0.0
        cap = self.risk.capital or 0
        spent = 0.0
        alt = 0
        mgap = None                      # 기록용: 지수(SPY) 개장 갭 — 1월 점검 때 '지수 급등 출발 날 신고가 매수 건너뛰기' 검증
        if not dry and self.m == "US" and (d.get("index") or {}).get("close"):
            try:
                sp = f(((self.api.prices(["SPY"]) or [{}])[0]).get("lastPrice"))
                mgap = round(sp / float(d["index"]["close"]) - 1, 4) if sp else None
            except Exception:
                mgap = None
        for x in cands:
            if slots <= 0 and not dry:
                break
            sym, e, stop = x["code"], px.get(x["code"]), float(x["stop"])
            if not e:
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
            if q < 1 and plan == "hold":
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
            if dry:
                if slots > 0:
                    table.append((x, "매수", etxt, q))
                    slots -= 1
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
                           sector=sec, earn_react=ev.get("react"), earn_gap=ev.get("gap_avg"), earn_hold=plan == "hold",
                           mkt_gap=mgap, entry_when=when)
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
            slots -= 1
            self.notify(f"🧭 스윙 매수 {x['name']} {q}주 ≈ {money(self.m, e)} · 손절 {money(self.m, sl)} "
                        f"(-{(1 - sl / e) * 100:.1f}%, 손실 한도 투자금의 {rp:g}%)\n산 이유: {x['reason']}"
                        + SW.ev_text(ev, etxt))
        return spent

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
        if not why:
            try:
                Bot.swing_add_on(self, p, bars, close)
            except Exception as e:
                self.log(f"🧭 추가 매수 판단 오류 {p.name}: {e}")
        if why:
            self.notify(f"🧭 스윙 정리 {p.name} ({held + 1}거래일째, {(close / p.entry - 1) * 100:+.1f}%) — {why}")
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
                    f"· 손절 {money(self.m, p.sl)}" + (f" · {note}" if note else "") + " · 계속 보유")

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
        if not c.get("enabled"):
            return
        from .stats import load, merge
        for r in merge(load(ROOT, self.m, c.get("days", 5), self.mode if self.mode in ("live", "paper") else "live")):
            if float(r.get("pnl") or 0) < 0 and r.get("kind") == "exit":
                self.stopped[r.get("symbol")] = r["_day"]
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
                        and n >= self.open_t + dt.timedelta(minutes=swc.get("entry_delay_min", 1))):
                    self.sw_done = today
                    cash = self.fresh_cash(cash, force=True)
                    try:
                        spent = self.swing_enter(cash)
                        if cash is not None:
                            cash -= spent
                    except Exception as e:
                        self.log(f"🧭 스윙 매수 오류: {e}")
                while self.sell_reqs:
                    sym = self.sell_reqs.pop(0)
                    st = self.syms.get(sym)
                    if sym in self.broker.pos and self.broker.pos[sym].state == "open":
                        self.broker.flatten(sym, "직접 매도 (텔레그램 보유종목)", st.last if st else None, force=True)
                undecided = [p for p in self.broker.pos.values() if p.extra.get("carry_done") != today]
                if n >= self.close_t or (n >= flatten_t and not undecided):
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
