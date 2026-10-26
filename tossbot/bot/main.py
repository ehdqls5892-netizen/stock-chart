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
from .strategy import SymState, entry_signal, exit_levels, session_bars, vwap
from .util import TZ, add_ticks, f, money, now, parse_hm, parse_ts

ROOT = Path(__file__).resolve().parent.parent
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
        self.ctx: dict = {"plans": {}, "daily": {}, "earn_block": set(), "watched": set()}
        self.stream = None
        self.stop_req: str | None = None   # None / "keep"(보유 유지하고 끝) / "sell"(전부 팔고 끝)
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
        if not self.cfg.get("swing", {}).get("enabled"):
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

    def load_desk(self):
        """주식비서 데스크 자료(GitHub docs/data/desk-<m>-*.json)를 읽는다. 없으면 조용히 건너뜀.
        - report: 마감 리포트의 1순위·픽·관심종목 가격 계획, 테마·이슈 종목
        - live:   장중 알림의 '지금 1순위'(더 최신이라 report 보다 우선)
        - earn:   실적 발표 일정과 발표 직후 결과(좋음/나쁨)
        """
        ctx = self.ctx
        ctx["plans"], ctx["earn_block"], ctx["earn_good"], ctx["watched"] = {}, set(), {}, set()
        base = self.cfg.get("desk_feed_url")
        if not base:
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
            pick = None
            # 미장은 토스 일정이 한국 날짜 기준이라, 한국 자정 이후엔 지금 열린 장이 previousBusinessDay 에 있다
            for key in ("previousBusinessDay", "today", "nextBusinessDay"):
                r = reg_of(cal.get(key) or {})
                if r and n < r[1]:
                    pick = r
                    break
            # 미장: 토스 일정의 'today' 는 한국 날짜 기준이라, 한국 자정이 지나면 지금 열려 있는
            # 뉴욕 오늘 장이 목록에서 빠진다 → 뉴욕 오늘 장이 진행 중이면 그것을 쓴다(실제 시세로 확인).
            if self.m == "US" and (not pick or pick[0] > n):
                o = dt.datetime.combine(n.date(), parse_hm(s["open"]), tz)
                c = dt.datetime.combine(n.date(), parse_hm(s["close"]), tz)
                if n.weekday() < 5 and o <= n < c and self.us_trading_now(n):
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
        front = list(self.ctx["earn_good"]) + list(self.ctx["plans"]) + [c for c in self.ctx["watched"] if c not in self.ctx["plans"]]
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
                if info.get("securityType") not in ("STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPT"):
                    continue
                if self.m == "KR" and info.get("isCommonShare") is False:
                    continue
                nm = info.get("name") or sym
                if any(w in nm for w in ("스팩", "SPAC")):
                    continue
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
            st = self.syms[sym]
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
        self.universe = [s for s in keep if not self.syms[s].excluded]
        ex = [f"{self.syms[s].name}({self.syms[s].excluded})" for s in keep if self.syms[s].excluded]
        self.log(f"후보 {len(self.universe)}개: " + ", ".join(self.syms[s].name or s for s in self.universe[:40])
                 + (f" … 제외 {len(ex)}개" if ex else ""))

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
        t = parse_ts(ts, self.m) or now(self.m)
        st.on_trade(price, vol, t)
        self.broker.on_price(sym, price, t)

    def on_closed(self, p):
        self.risk.record_exit(p.pnl)
        st = self.syms.get(p.symbol)
        if st and p.pnl < 0:
            st.cooldown_until = now(self.m) + dt.timedelta(minutes=self.cfg["strategy"]["cooldown_min"])

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
        if n < self.open_t - dt.timedelta(minutes=3):
            w = (self.open_t - dt.timedelta(minutes=3) - n).total_seconds()
            self.log(f"장 시작까지 {int(w // 3600)}시간 {int(w % 3600 // 60)}분 기다려요. 이 창(화면)을 켜 둔 채로 두세요.")
            self.phase = f"장 시작 기다리는 중 ({self.open_t.strftime('%m/%d %H:%M')} {'한국' if self.m == 'KR' else '뉴욕'} 시간 시작)"
            end = time.time() + max(0, w)
            while time.time() < end:
                if self.stop_req:
                    self.log("장 시작 전에 종료했어요.")
                    return
                time.sleep(min(1.0, end - time.time()) if end > time.time() else 0)

        self.broker = (LiveBroker(self.api, self.m, self.cfg, self.dir, self.log, self.notify) if live
                       else PaperBroker(self.m, self.cfg, self.dir, self.log, self.notify))
        self.broker.on_closed = self.on_closed
        self.broker.restore()
        if live:
            self.broker.verify_carried()
        for p in self.broker.pos.values():
            self.syms.setdefault(p.symbol, SymState(p.symbol)).name = p.name

        cash = None
        if live:
            try:
                cash = f((self.api.buying_power("KRW" if self.m == "KR" else "USD") or {}).get("cashBuyingPower"))
                self.log(f"매수 가능 금액: {money(self.m, cash or 0)}")
                need = self.risk.capital or self.cfg["risk"][self.m].get("budget_per_trade", 0)
                if self.m == "KR" and cash is not None and self.risk.capital and cash < self.risk.capital:
                    self.log(f"계좌의 매수 가능 금액이 오늘 투자금보다 적어요 → {money(self.m, cash)} 안에서만 사요")
                if self.m == "US" and (cash or 0) < need:
                    self.log("달러 잔고가 1회 매수 금액보다 적어요. 토스의 '원화로 바로 주문(자동 환전)'이 켜져 있으면 그대로 주문되고, "
                             "아니면 주문이 거절돼요 → 토스 앱에서 환전하거나 자동 환전을 켜 주세요.")
                    cash = None
            except TossError as e:
                self.log(f"매수 가능 금액 조회 실패: {e}")

        self.build_universe()
        self.refresh_bars(list(dict.fromkeys(self.universe + list(self.broker.pos))))
        from .stream import Stream
        self.stream = Stream(self.api, self.m, self.on_trade, self.broker.on_order_event if live else None, self.log)
        self.stream.start()
        time.sleep(2)
        self.stream.set_codes(list(dict.fromkeys(list(self.broker.pos) + self.universe)))
        if self.broker.carried():
            self.notify("📦 전날부터 들고 있는 종목: " + ", ".join(
                f"{p.name} {p.qty}주(손절선 {money(self.m, p.sl)})" for p in self.broker.carried()))
        self.notify(f"▶️ 로봇 시작 ({self.m}, {'실전' if live else '연습'}) · 후보 {len(self.universe)}개")

        last_refresh, last_universe, last_status = 0.0, time.time(), 0.0
        rr = 0
        try:
            while True:
                n = now(self.m)
                today = n.date().isoformat()
                self.phase = ("장중 매매" if n < no_entry_t else "새 진입 없이 보유분 관리" if n < flatten_t else "마감 정리")
                if self.stop_req and self.handle_stop(today):
                    break
                undecided = [p for p in self.broker.pos.values() if p.extra.get("carry_done") != today]
                if n >= self.close_t or (n >= flatten_t and not undecided):
                    break
                if self.risk.kill_switch() and self.broker.open_count():
                    for sym in list(self.broker.pos):
                        self.broker.flatten(sym, "긴급 정지", self.syms.get(sym).last if sym in self.syms else None)
                # 분봉 정확도 보정: 1초에 몇 종목씩 돌아가며 REST 캔들로 다시 맞춤
                watch = list(dict.fromkeys(list(self.broker.pos) + self.universe))
                if time.time() - last_refresh > 1.0 and watch:
                    batch = watch[rr:rr + 4] or watch[:4]
                    rr = (rr + 4) % max(1, len(watch))
                    for sym in batch:
                        try:
                            self.syms[sym].load_candles(self.api.candles(sym, "1m", 5), self.m)
                        except Exception:
                            pass
                    last_refresh = time.time()
                # 30분마다 후보 갱신 (새로 거래대금이 몰린 종목 반영)
                if time.time() - last_universe > self.cfg["universe"]["refresh_min"] * 60 and n < no_entry_t:
                    old = set(self.universe)
                    self.build_universe()
                    new = [s for s in self.universe if s not in old]
                    if new:
                        self.refresh_bars(new)
                    self.stream.set_codes(list(dict.fromkeys(self.universe + list(self.broker.pos))))
                    last_universe = time.time()
                # 진입
                if self.open_t <= n < no_entry_t and not self.stop_req:
                    why = self.risk.can_enter(self.broker.open_count(), self.broker.invested())
                    if why is None:
                        cands = []
                        for sym in self.universe:
                            if sym in self.broker.pos:
                                continue
                            sig = entry_signal(self.syms[sym], n, self.open_t, self.cfg, self.ctx)
                            if sig:
                                cands.append((sig["score"], sym, sig))
                        cands.sort(key=lambda x: -x[0])          # 여러 종목이 동시에 신호 → 가장 센 것부터
                        for _, sym, sig in cands:
                            st = self.syms[sym]
                            est = add_ticks(self.m, st.last, self.cfg["live"]["buy_slip_ticks"] if live else 1)
                            tp, sl, R, a = exit_levels(self.m, est, sig, self.cfg, st, self.open_t)
                            sl_exec = add_ticks(self.m, sl, -2 if live else -1)
                            fee = self.cfg["fees"][self.m]
                            w = self.risk.strength(sig)
                            qty = self.risk.qty_for(est, sl_exec, cash, (fee["buy_pct"] + fee["sell_pct"] + fee["sell_tax_pct"]) / 100,
                                                    self.broker.invested(), w)
                            if qty < 1:
                                continue
                            why = (f"확신도 {sig['grade']}(비중 {w:g}배) · " if sig.get("grade") else "") + sig["reason"]
                            p = self.broker.enter(sym, st.name or sym, st.last, qty, tp, sl, why, r=R, atr=a)
                            if p:
                                self.mark_swing(p, sig)
                                st.entries_today += 1
                                self.risk.trades += 1
                                if cash is not None:
                                    cash -= p.entry * p.qty
                                break  # 한 번에 한 종목씩
                # 추가 매수(불타기): 잃지 않는 상태 + 같은 종목에 새 강한 신호
                if self.open_t <= n < no_entry_t and not self.stop_req and not self.risk.kill_switch() and not self.risk.halted:
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
                # 장 마감 전 정리
                if n >= flatten_t:
                    for sym, p in list(self.broker.pos.items()):
                        if p.extra.get("carry_done") == today:
                            continue
                        st = self.syms.get(sym)
                        last = st.last if st else None
                        if p.state == "open":
                            vw = vwap(session_bars(st, self.open_t)) if st else None
                            why = self.broker.carry_check(p, last, vw)
                            if why is None:
                                self.broker.carry(p, last)
                                p.extra["carry_done"] = today
                                self.broker.save_state()
                                continue
                            self.broker.flatten(sym, f"장 마감 전 정리 ({why})", last)
                        else:
                            self.broker.flatten(sym, "장 마감 전 정리", last)
                self.broker.poll(n, {k: v.last for k, v in self.syms.items()})
                if time.time() - last_status > 300:
                    self.status()
                    last_status = time.time()
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.log("Ctrl+C 로 멈춤")
            if self.broker.open_count():
                self.log(f"⚠️ 아직 보유 중 {self.broker.open_count()}건 — 실전이면 토스 서버의 익절·손절 조건주문은 그대로 살아 있어요.")
        finally:
            if self.stream:
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
        if not self.broker.pos:
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
                            "carry": bool(p.extra.get("carry")), "days": len(p.extra.get("days_held", [])),
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
