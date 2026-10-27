"""백테스트: 최근 실제 1분봉으로 '지금 로봇 코드 그대로' 하루씩 다시 돌려 본다 (연습 모드, 가짜 시계).

  python tools/backtest.py us --data ../data-us --days 10 --top 150
  python tools/backtest.py kr --data ../data-kr --days 10 --top 200

- 시세: 야후 1분봉(미장은 프리마켓 포함). 한 분봉 안에서는 시가 → (양봉이면 저가, 음봉이면 고가) → 반대 극값 → 종가 순서로
  15초마다 체결이 들어온 것처럼 흘려 보낸다. 지금 진행 중인 분봉의 종가·거래량은 미리 보여 주지 않는다(미래 정보 금지).
- 후보: 그날 '전날 거래대금 상위 + 전날 상승률 상위' (실제 로봇의 거래대금·상승률 랭킹 흉내).
- 데스크(주식비서) 계획·실적 일정·뉴스는 과거 자료가 없어 빠진다 → 실전보다 매수 기회가 적게 나올 수 있다.
- 수수료·세금 포함, 투자금은 settings 와 같게(국장 100만 원, 미장 $1,000) 또는 --capital.
결과: out/backtest-<m>.json + 화면 요약 (GitHub Actions 요약에도 씀)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["TOSSBOT_NO_BG"] = "1"
os.environ["TOSSBOT_OFFLINE"] = "1"
for k in ("TG_TOKEN", "TG_CHAT"):
    os.environ.pop(k, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot.main as M          # noqa: E402
import bot.stream as BS       # noqa: E402
import bot.util as U          # noqa: E402
from bot import stats         # noqa: E402

STEP = 15  # 초


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ─────────────────────────── 자료 ───────────────────────────

def pick_symbols(m: str, data_dir: Path, top: int) -> dict:
    """data-<m> 브랜치의 전 종목 일봉에서 최근 20일 평균 거래대금 상위 top 개. code → {name, market, yf}"""
    idx = json.loads((data_dir / "index.json").read_text(encoding="utf-8"))
    rows = []
    for it in idx["items"]:
        code, name, mk = it[0], it[1], it[2] if len(it) > 2 else ""
        f = data_dir / "s" / f"{code}.json"
        if not f.exists():
            continue
        try:
            bars = json.loads(f.read_text(encoding="utf-8")).get("bars") or []
        except ValueError:
            continue
        tv = [b[4] * b[5] for b in bars[-20:] if len(b) > 5 and b[4] and b[5]]
        if len(tv) < 10:
            continue
        rows.append((sum(tv) / len(tv), code, name, mk))
    rows.sort(reverse=True)
    out = {}
    for _, code, name, mk in rows[:top]:
        yf_sym = (code + (".KQ" if "코스닥" in mk else ".KS")) if m == "kr" else code.replace(".", "-")
        out[code] = {"name": name, "market": mk, "yf": yf_sym}
    return out


def download(m: str, syms: dict, days: int) -> dict:
    """code → {날짜: [(분 시각, o, h, l, c, v)], ...}  (정규장 + 미장은 프리마켓)"""
    import yfinance as yf
    tz = U.TZ[m.upper()]
    end = dt.datetime.now(dt.timezone.utc)
    chunks = []
    span = max(1, (days * 7 // 5 + 3 + 6) // 7)
    for i in range(min(span, 4)):                  # 야후 1분봉은 최근 30일 안에서 7일씩
        e = end - dt.timedelta(days=7 * i)
        chunks.append((e - dt.timedelta(days=7), e))
    data: dict = {c: {} for c in syms}
    codes = list(syms)
    for s, e in chunks:
        for i in range(0, len(codes), 60):
            part = codes[i:i + 60]
            ys = [syms[c]["yf"] for c in part]
            try:
                df = yf.download(ys, start=s, end=e, interval="1m", prepost=(m == "us"), group_by="ticker",
                                 auto_adjust=False, progress=False, threads=True)
            except Exception as ex:
                log("받기 실패", ex)
                continue
            for c, y in zip(part, ys):
                try:
                    h = (df[y] if len(ys) > 1 else df).dropna(subset=["Close"])
                except Exception:
                    continue
                for t, r in h.iterrows():
                    t = t.tz_convert(tz).to_pydatetime().replace(second=0, microsecond=0)
                    data[c].setdefault(t.date(), {})[t] = (float(r["Open"]), float(r["High"]), float(r["Low"]),
                                                           float(r["Close"]), float(r["Volume"] or 0))
            time.sleep(1)
    out = {}
    for c, byday in data.items():
        out[c] = {d: sorted((t, *v) for t, v in mins.items()) for d, mins in byday.items()}
    return out


# ─────────────────────────── 가짜 시계·토스 서버 ───────────────────────────

class Clock:
    t: dt.datetime = None

    def now(self, market=None):
        return self.t


CLK = Clock()


class Market:
    """하루치 시세 재생기."""

    def __init__(self, m: str, data: dict, names: dict, day: dt.date, prev_day: dt.date, uni: list):
        self.m, self.data, self.names, self.day, self.prev_day, self.uni = m, data, names, day, prev_day, uni
        tz = U.TZ[m.upper()]
        hm = ((9, 0), (15, 30)) if m == "kr" else ((9, 30), (16, 0))
        self.open = dt.datetime.combine(day, dt.time(*hm[0]), tz)
        self.close = dt.datetime.combine(day, dt.time(*hm[1]), tz)
        self.cache = {}

    def bars(self, sym, day=None):
        return self.data.get(sym, {}).get(day or self.day, [])

    def mins(self, sym):
        if sym not in self.cache:
            self.cache[sym] = {b[0]: b for b in self.bars(sym)}
        return self.cache[sym]

    def prev_close(self, sym):
        b = [x for x in self.bars(sym, self.prev_day) if self.open.time() <= x[0].time() < self.close.time()]
        return b[-1][4] if b else None

    def price(self, sym):
        t = CLK.t
        k = t.replace(second=0, microsecond=0)
        b = self.mins(sym).get(k)
        if b is None:
            prev = [x for x in self.bars(sym) if x[0] < k]
            return prev[-1][4] if prev else self.prev_close(sym)
        _, o, h, l, c, v = b
        s = t.second
        up = c >= o
        return o if s < 15 else (l if up else h) if s < 30 else (h if up else l) if s < 45 else c

    def candles_1m(self, sym, count):
        t = CLK.t
        k = t.replace(second=0, microsecond=0)
        out = []
        for b in self.bars(sym):
            if b[0] < self.open:
                continue
            if b[0] < k:
                tt, o, h, l, c, v = b
            elif b[0] == k:                            # 진행 중인 분봉: 지금까지 보인 것만
                _, o, h, l, c, v = b
                s = t.second
                up = c >= o
                seen = [o] + ([l if up else h] if s >= 15 else []) + ([h if up else l] if s >= 30 else []) + ([c] if s >= 45 else [])
                tt, h, l, c, v = b[0], max(seen), min(seen), seen[-1], v * min(1.0, (s + STEP) / 60)
            else:
                break
            out.append({"timestamp": tt.isoformat(), "openPrice": o, "highPrice": h, "lowPrice": l, "closePrice": c, "volume": v})
        return out[-count:]


class FakeAPI:
    account_seq = None
    MK: Market = None

    def __init__(self, *a, **k):
        pass

    def token(self):
        return "tok"

    def accounts(self):
        return [{"accountNo": "1", "accountSeq": 1, "accountType": "BROKERAGE"}]

    def calendar(self, m):
        x = self.MK
        reg = {"regularMarket": {"startTime": x.open.isoformat(), "endTime": x.close.isoformat()}}
        day = {"date": x.day.isoformat(), **({"integrated": reg} if x.m == "kr" else reg)}
        return {"today": day}

    def rankings(self, *a, **k):
        return {"rankings": [{"symbol": s} for s in self.MK.uni]}

    def stocks(self, syms):
        return [{"symbol": s, "name": self.MK.names.get(s, s), "securityType": "STOCK", "isCommonShare": True}
                for s in syms if s in self.MK.data]

    def warnings(self, s):
        return []

    def prices(self, syms):
        return [{"symbol": s, "lastPrice": str(self.MK.price(s))} for s in syms if s in self.MK.data and self.MK.price(s)]

    def price(self, sym):
        return self.MK.price(sym)

    def candles(self, sym, interval="1m", count=60):
        if interval == "1d":
            pc = self.MK.prev_close(sym)
            return [{"timestamp": dt.datetime.combine(self.MK.prev_day, dt.time(16, 0), U.TZ[self.MK.m.upper()]).isoformat(),
                     "closePrice": str(pc)}] if pc else []
        return self.MK.candles_1m(sym, count)

    def orderbook(self, sym):
        p = self.MK.price(sym) or 0
        tk = U.kr_tick(p) if self.MK.m == "kr" else 0.01
        return {"asks": [{"price": str(p + tk)}], "bids": [{"price": str(p)}]}

    def buying_power(self, c):
        return {"cashBuyingPower": "1000000000"}


class FakeStream:
    def __init__(self, *a, **k):
        self.codes = []

    def start(self):
        pass

    def set_codes(self, c):
        self.codes = c

    def stop(self):
        pass


# ─────────────────────────── 실행 ───────────────────────────

def run(m: str, data: dict, names: dict, capital: float, uni_n: int, last_days: int):
    mk = m.upper()
    tz = U.TZ[mk]
    o_t, c_t = ((9, 0), (15, 30)) if m == "kr" else ((9, 30), (16, 0))
    days = sorted({d for byday in data.values() for d, bars in byday.items()
                   if d.weekday() < 5 and sum(1 for b in bars if dt.time(*o_t) <= b[0].time() < dt.time(*c_t)) > 100})
    days = [d for d in days if sum(1 for c in data if d in data[c]) > len(data) * 0.5]
    if len(days) < 2:
        raise SystemExit("1분봉 거래일이 2일 미만이에요")
    days = days[-(last_days + 1):]
    tmp = Path(tempfile.mkdtemp())
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["markets"] = [mk]
    cfg["github_chart_url"] = ""
    cfg["desk_feed_url"] = ""
    cfg["news"]["enabled"] = False
    (tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    (tmp / "settings.json").write_text(json.dumps({"capital": {mk: capital}}), encoding="utf-8")
    M.ROOT = tmp
    M._API = None
    M.TossAPI = FakeAPI
    M.now = CLK.now
    U.now = CLK.now
    BS.Stream = FakeStream
    import time as T
    real_sleep, real_time = T.sleep, T.time
    trades = []
    for prev, day in zip(days, days[1:]):
        # 그날 후보: 전날 거래대금 상위 + 전날 상승률 상위 (실제 랭킹 흉내)
        stat = []
        for c, byday in data.items():
            b = [x for x in byday.get(prev, []) if dt.time(*o_t) <= x[0].time() < dt.time(*c_t)]
            if len(b) < 50 or day not in byday:
                continue
            tv = sum(x[4] * x[5] for x in b)
            stat.append((tv, (b[-1][4] / b[0][1] - 1) if b[0][1] else 0, c))
        by_tv = [c for _, _, c in sorted(stat, reverse=True)][:uni_n]
        by_up = [c for _, _, c in sorted(stat, key=lambda x: -x[1])][:max(10, uni_n // 4)]
        uni = list(dict.fromkeys(by_tv + by_up))
        mkt = Market(m, data, names, day, prev, uni)
        FakeAPI.MK = mkt
        CLK.t = mkt.open - dt.timedelta(minutes=9)
        holder = {}

        def fake_sleep(s):
            CLK.t += dt.timedelta(seconds=STEP)
            b = holder.get("b")
            if b is not None and getattr(b, "universe", None) and getattr(b, "broker", None) and CLK.t >= mkt.open:
                for sym in dict.fromkeys(list(b.broker.pos) + list(b.universe)):
                    p = mkt.price(sym)
                    if p:
                        b.on_trade(sym, p, 1, CLK.t.isoformat())

        M.time.sleep = fake_sleep
        M.time.time = lambda: CLK.t.timestamp()
        t0 = real_time()
        try:
            bot = M.Bot("paper", mk)
            holder["b"] = bot
            bot.run()
        finally:
            M.time.sleep, M.time.time = real_sleep, real_time
        got = [p for p in bot.broker.closed] if getattr(bot, "broker", None) else []
        for p in got:
            trades.append({"_day": day.isoformat(), "kind": "exit", "name": p.name, "symbol": p.symbol, "qty": p.qty,
                           "entry": p.entry, "exit_price": p.exit_price, "pnl": p.pnl, "reason": p.reason,
                           "exit_reason": p.exit_reason, "extra": {k: p.extra.get(k) for k in ("setup", "grade")}})
        dp = sum(p.pnl for p in got)
        log(f"{day} 후보 {len(uni)} · 매매 {len(got)} · 손익 {dp:,.2f} · 들고 감 {len(bot.broker.carried()) if getattr(bot, 'broker', None) else 0}"
            f" · {real_time() - t0:.0f}초")
    return days[1:], trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["kr", "us"])
    ap.add_argument("--data", default="")
    ap.add_argument("--load", default="", help="저장해 둔 1분봉(data-<m>.pkl.gz)으로 다시 돌리기")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--top", type=int, default=150)
    ap.add_argument("--uni", type=int, default=100)
    ap.add_argument("--capital", type=float)
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    cap = a.capital or (1_000_000 if a.market == "kr" else 1000)
    import gzip
    import pickle
    if a.load:
        syms, data = pickle.load(gzip.open(a.load, "rb"))
    else:
        syms = pick_symbols(a.market, Path(a.data), a.top)
        log(f"종목 {len(syms)}개 1분봉 받는 중…")
        data = download(a.market, syms, a.days)
        data = {c: v for c, v in data.items() if v}
        Path(a.out).mkdir(parents=True, exist_ok=True)
        with gzip.open(Path(a.out) / f"data-{a.market}.pkl.gz", "wb") as f:   # 다시 돌려 볼 때 쓰려고 저장
            pickle.dump((syms, data), f)
    log(f"받음 {len(data)}개")
    names = {c: syms[c]["name"] for c in syms}
    days, trades = run(a.market, data, names, cap, a.uni, a.days)
    mk = a.market.upper()
    text = stats.summarize(trades, mk, cap) if trades else "매매 없음"
    verdict = stats.verdict(trades, cap) if trades else ""
    pnl_by_day = {}
    for t in trades:
        pnl_by_day[t["_day"]] = pnl_by_day.get(t["_day"], 0) + t["pnl"]
    res = {"market": a.market, "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
           "days": [d.isoformat() for d in days], "capital": cap, "symbols": len(data), "summary": text,
           "verdict": verdict, "byDay": pnl_by_day, "trades": trades}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"backtest-{a.market}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    md = f"## 백테스트 {mk} ({days[0]} ~ {days[-1]}, {len(days)}일, 투자금 {cap:,.0f})\n\n```\n{text}\n{verdict}\n```\n\n일별: " + \
         ", ".join(f"{d} {v:+,.0f}" for d, v in pnl_by_day.items())
    print(md)
    log(md)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md + "\n")


if __name__ == "__main__":
    main()
