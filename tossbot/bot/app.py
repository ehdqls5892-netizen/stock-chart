"""시작·종료 버튼이 있는 화면 (내 PC 브라우저에서 열림).

실행: python -m bot.app   → 브라우저에 http://127.0.0.1:8777 이 열려요.
- [연습 시작] / [실전 시작]: 지금부터 자동매매 시작 (장 전이면 장 시작까지 기다렸다 시작)
- [종료 · 보유 유지]: 새로 사지 않고 끝. 보유 종목은 토스 서버의 '시장가 손절' 조건주문을 걸어 둔 채로 둠
- [종료 · 전부 팔기]: 보유 종목을 전부 시장가로 팔고 끝
휴대폰으로 조작하는 방법 두 가지
1) 같은 와이파이: .env 에 APP_LAN=1, APP_PIN=숫자4~8자리 → 휴대폰 브라우저에서 http://PC주소:8777 (PIN 입력)
2) 어디서나: 텔레그램 봇 (.env 에 TG_TOKEN, TG_CHAT) → 봇 채팅방의 버튼으로 상태 보기·시작·종료
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import main as M

PORT = 8777
STATE = {"bots": {}, "threads": {}, "last": {}, "errors": {}, "stop_all": False, "repeat": True, "mode": None}
LOCK = threading.Lock()


def running() -> bool:
    return any(t.is_alive() for t in STATE["threads"].values())


def _cfg() -> dict:
    return json.loads((M.ROOT / "config.json").read_text(encoding="utf-8"))


def start(mode: str, repeat: bool = True) -> str:
    with LOCK:
        if running():
            return "이미 실행 중이에요."
        cfg = _cfg()
        if mode == "live" and not cfg["live"].get("enabled"):
            return "config.json 의 live.enabled 가 false 예요."
        mks = M.markets_of(cfg)
        STATE.update(stop_all=False, repeat=repeat, mode=mode, errors={}, bots={}, threads={}, last={})

        def loop(mk):
            import time
            fails = 0
            while True:
                try:
                    b = M.Bot(mode, mk)
                except Exception as e:
                    STATE["errors"][mk] = f"시작 실패: {e}"
                    return
                STATE["bots"][mk] = b
                try:
                    b.run()
                except Exception as e:
                    STATE["errors"][mk] = f"{e}"
                    b.log(f"❌ 오류로 멈춤: {e}")
                    b.log(traceback.format_exc()[-1500:])
                    b.notify(f"❌ 로봇 오류로 멈춤: {e}")
                finally:
                    b.phase = "종료됨"
                    STATE["last"][mk] = b.snapshot()
                if STATE["errors"].get(mk) and not STATE.get("stop_all"):
                    fails += 1
                    if fails > 5:
                        b.notify(f"❌ {mk} 오류가 계속 나서 멈춰요. 확인이 필요해요: {STATE['errors'][mk]}")
                        return
                    b.notify(f"🔁 {mk} 1분 뒤 다시 시작해요 ({fails}/5) — 보유 종목은 이어받고 손절 주문을 다시 확인해요")
                    for _ in range(60):
                        if STATE.get("stop_all"):
                            return
                        time.sleep(1)
                    STATE["errors"].pop(mk, None)
                    continue
                fails = 0
                if STATE.get("stop_all") or not STATE.get("repeat"):
                    return
                for _ in range(60):   # 잠깐 쉬고 다음 장 준비
                    if STATE.get("stop_all"):
                        return
                    time.sleep(1)

        for mk in mks:
            t = threading.Thread(target=loop, args=(mk,), daemon=True)
            STATE["threads"][mk] = t
            t.start()
        st = M.load_settings()
        st["resume"] = mode if repeat else None     # 서버가 재부팅돼도 이어서 켜지게 (APP_RESUME=1 일 때)
        M.save_settings(st)
        names = " · ".join({"KR": "국장", "US": "미장"}[m] for m in mks)
        return f"시작했어요 ({names})." + (" 매일 다음 장도 자동으로 이어서 해요." if repeat else "")


def _watchdog():
    """서버 전용: 장중에 로봇이 3분 넘게 멈춰 있으면(먹통) 알리고 프로그램을 끝냄 → systemd 가 10초 뒤 다시 켜고 이어받음."""
    import os
    import time
    while True:
        time.sleep(30)
        if STATE.get("stop_all"):
            continue
        for mk, b in list(STATE.get("bots", {}).items()):
            beat = getattr(b, "beat", None)
            if beat and getattr(b, "phase", "") in ("장중 매매", "새 진입 없이 보유분 관리", "마감 정리") \
                    and time.time() - beat > 180:
                try:
                    b.notify(f"⚠️ {mk} 로봇이 3분 넘게 멈춰 있어서 다시 켜요 (보유 종목·손절 주문은 이어받아요)")
                    b.log("⚠️ 멈춤 감지 → 프로그램 다시 시작")
                except Exception:
                    pass
                time.sleep(3)
                os._exit(1)


def lock_cmd(text: str) -> str:
    """'잠금 이름' / '잠금 해제 이름' / '잠금'(목록). 잠금 = 장 마감에 정리하지 않고 들고 감, 동시 보유 한도에 안 셈."""
    t = text.replace("🔒", "").strip()
    unlock = "해제" in t
    key = t.replace("잠금", "").replace("해제", "").strip()
    found, locked = [], []
    for mk, b in list(STATE.get("bots", {}).items()):
        br = getattr(b, "broker", None)
        if not br:
            continue
        for p in list(br.pos.values()):
            if p.extra.get("locked"):
                locked.append(f"{p.name} {p.qty}주(손절 {M.money(mk, p.sl)})")
            if key and (key in p.name or key.upper() == p.symbol.upper()):
                found.append((mk, br, p))
    if not key:
        return "🔒 잠금 중: " + (", ".join(locked) if locked else "없음") + \
               "\n잠그기: '잠금 종목이름' · 풀기: '잠금 해제 종목이름'"
    if not found:
        return f"'{key}' 은 로봇이 관리하는 보유 종목에 없어요. (로봇이 산 종목만 잠글 수 있어요)"
    out = []
    for mk, br, p in found:
        if unlock:
            p.extra.pop("locked", None)
            out.append(f"🔓 {p.name} 잠금 해제 — 다시 동시 보유 한도에 포함되고, 장 마감 정리 규칙을 따라요")
        else:
            p.extra["locked"] = True
            out.append(f"🔒 {p.name} {p.qty}주 잠금 — 장 마감에 정리하지 않고 들고 가요. 동시 보유 한도·오늘 투자금에서 빠져요. "
                       f"손절 주문({M.money(mk, p.sl)})은 그대로 지켜요")
        br.save_state()
    return "\n".join(out)


# ───────── 보유종목 칸 (텔레그램): 종목별 잠금·팔기 ─────────
def _state_path(mk: str) -> Path:
    mode = STATE.get("mode") or M.load_settings().get("resume") or "live"
    return M.ROOT / "logs" / ("paper" if mode == "paper" else "live") / mk / "state.json"


def _broker(mk: str):
    b = STATE.get("bots", {}).get(mk)
    return getattr(b, "broker", None) if b is not None else None


def _file_positions(mk: str) -> list:
    """로봇이 장 밖(장 시작 기다림·꺼짐)이라 메모리에 없을 때: 저장된 보유 기록에서 읽음."""
    try:
        st = json.loads(_state_path(mk).read_text(encoding="utf-8"))
    except Exception:
        return []
    return [d for d in st.get("positions", []) if d.get("state") == "open"]


def holdings() -> list:
    """로봇이 관리하는 보유 종목 (국장·미장 함께)."""
    out = []
    for mk in ("KR", "US"):
        br = _broker(mk)
        b = STATE.get("bots", {}).get(mk)
        if br is not None:
            for p in br.pos.values():
                if p.state not in ("open", "closing"):
                    continue
                st = (getattr(b, "syms", None) or {}).get(p.symbol)
                last = getattr(st, "last", None) or None
                out.append({"market": mk, "symbol": p.symbol, "name": p.name, "qty": p.qty, "entry": p.entry, "sl": p.sl,
                            "last": last, "pct": (last / p.entry - 1) * 100 if last else None,
                            "locked": bool(p.extra.get("locked")), "state": p.state})
        else:
            for d in _file_positions(mk):
                ex = d.get("extra") or {}
                out.append({"market": mk, "symbol": d["symbol"], "name": d.get("name") or d["symbol"], "qty": d.get("qty"),
                            "entry": d.get("entry"), "sl": d.get("sl"), "last": None, "pct": None,
                            "locked": bool(ex.get("locked")), "state": "open"})
    miss = [h for h in out if not h["last"]]
    if miss:                                  # 장 밖이거나 아직 체결이 없으면 토스 현재가(마지막 가격)로
        px = quotes([h["symbol"] for h in miss])
        for h in miss:
            h["last"] = px.get(h["symbol"])
    fx = fx_usdkrw() if any(h["market"] == "US" for h in out) else None
    for h in out:
        last, e, q = h["last"], h.get("entry"), h.get("qty") or 0
        h["pct"] = (last / e - 1) * 100 if last and e else None
        h["pnl"] = (last - e) * q if last and e else None
        h["value"] = last * q if last else None
        h["fx"] = fx if h["market"] == "US" else 1.0
    return out


def quotes(syms: list) -> dict:
    """토스 현재가 (장 밖이면 마지막 가격). 키가 없거나 실패하면 빈 값."""
    if not syms:
        return {}
    try:
        M.load_env()
        if not os.getenv("TOSS_CLIENT_ID"):
            return {}
        from .util import f
        return {x.get("symbol"): f(x.get("lastPrice")) for x in (M.shared_api().prices(list(syms)) or [])}
    except Exception:
        return {}


_FX = {"v": None, "t": 0.0}


def fx_usdkrw():
    """원/달러 환율 (10분마다 새로, 실패하면 직전 값). 표시용 대략치."""
    import time
    if _FX["v"] and time.time() - _FX["t"] < 600:
        return _FX["v"]
    try:
        import requests
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/KRW=X", params={"range": "1d", "interval": "1d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=6).json()
        v = float(r["chart"]["result"][0]["meta"]["regularMarketPrice"])
        if 800 < v < 3000:
            _FX.update(v=v, t=time.time())
    except Exception:
        pass
    return _FX["v"]


def set_lock(mk: str, sym: str, on: bool) -> str:
    br = _broker(mk)
    if br is not None:
        p = br.pos.get(sym)
        if not p or p.state != "open":
            return "그 종목은 지금 보유 중이 아니에요 (이미 팔렸을 수 있어요)."
        if on:
            p.extra["locked"] = True
        else:
            p.extra.pop("locked", None)
        br.save_state()
        name = p.name
    else:
        fp = _state_path(mk)
        try:
            st = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return "보유 기록을 읽지 못했어요."
        hit = [d for d in st.get("positions", []) if d.get("symbol") == sym and d.get("state") == "open"]
        if not hit:
            return "그 종목은 지금 보유 중이 아니에요."
        for d in hit:
            ex = d.setdefault("extra", {})
            if on:
                ex["locked"] = True
            else:
                ex.pop("locked", None)
        fp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        name = hit[0].get("name") or sym
    return (f"🔒 {name} 잠금 — 장 마감에 안 팔고 들고 가요 · 동시 보유 한도에서 빠짐 · 손절선 따라 자동 매도는 그대로"
            if on else f"🔓 {name} 잠금 해제 — 다시 보통 규칙(나눠 익절·장 마감 정리)을 따라요")


def sell_one(mk: str, sym: str) -> str:
    """한 종목만 시장가로 팔기 (로봇은 계속 돎). 실제 주문은 로봇 루프가 처리."""
    b = STATE.get("bots", {}).get(mk)
    br = _broker(mk)
    if b is None or br is None or not getattr(b, "loop_on", False):
        return ("지금은 이 시장 장이 열려 있지 않아서(로봇이 장 밖) 팔 수 없어요. "
                "장 중에 다시 누르거나 토스 앱에서 직접 파세요. 서버 손절 주문은 그대로 걸려 있어요.")
    p = br.pos.get(sym)
    if not p or p.state != "open":
        return "그 종목은 지금 보유 중이 아니에요 (이미 팔리는 중이거나 팔렸을 수 있어요)."
    if sym not in b.sell_reqs:
        b.sell_reqs.append(sym)
    return f"💰 {p.name} {p.qty}주 시장가 매도 주문을 넣어요. 체결되면 알려 드릴게요."


def lock_all() -> str:
    hs = [h for h in holdings() if not h["locked"] and h["state"] == "open"]
    if not hs:
        return "잠글 종목이 없어요 (이미 다 잠겨 있거나 보유가 없어요)."
    return "\n".join(set_lock(h["market"], h["symbol"], True) for h in hs)


def resume_dd() -> str:
    """계좌 낙폭 한도 멈춤 풀기: 지금부터 낙폭을 새로 계산."""
    import datetime as dt
    st = M.load_settings()
    locks = st.get("dd_lock") or {}
    if not locks:
        return "멈춘 시장이 없어요."
    names = ", ".join({"KR": "국장", "US": "미장"}.get(k, k) for k in locks)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    for mk in list(locks):
        st.setdefault("dd_base", {})[mk] = stamp
        b = STATE["bots"].get(mk)
        if b is not None and hasattr(b, "risk") and b.risk.halted and "낙폭" in b.risk.halted:
            b.risk.halted = None
    st["dd_lock"] = {}
    M.save_settings(st)
    return f"▶️ {names} 다시 매수할 수 있어요. 낙폭은 지금부터 새로 계산해요."


def _briefing_loop(send):
    """장 시작 전 브리핑: 국장 08:40(한국), 미장 09:15(뉴욕) — 평일."""
    import datetime as dt
    import time
    from zoneinfo import ZoneInfo
    from . import stats
    done = set()
    while True:
        try:
            for mk, hm, tz in (("KR", "08:40", "Asia/Seoul"), ("US", "09:15", "America/New_York")):
                n = dt.datetime.now(ZoneInfo(tz))
                key = (mk, n.date())
                if n.weekday() >= 5 or key in done or n.strftime("%H:%M") < hm or n.strftime("%H:%M") > "10:30":
                    continue
                done.add(key)
                s = status()
                cfg = _cfg()
                if mk not in M.markets_of(cfg):
                    continue
                x = s["markets"].get(mk, {})
                name = {"KR": "🇰🇷 국장", "US": "🇺🇸 미장"}[mk]
                fmt = (lambda v: f"${v:,.2f}") if mk == "US" else (lambda v: f"{v:,.0f}원")
                st = M.load_settings()
                lines = [f"🌅 {name} 장 전 브리핑 ({n.strftime('%m/%d')})",
                         f"투자금 {fmt(x.get('capital') or 0)} · 하루 손실 한도 {fmt(x.get('loss_limit') or 0)}"]
                pos = [p for p in s.get("positions", []) if p.get("market") == mk]
                lines.append("보유: " + (", ".join(f"{p['name']} {p['qty']}주(손절 {fmt(p['sl'])})" for p in pos) if pos else "없음"))
                for mode, title in (("live", "실전"), ("paper", "연습")):
                    recs = stats.load(M.ROOT, mk, 14, mode)
                    if recs:
                        last = max(r["_day"] for r in recs)
                        lp = sum(float(r.get("pnl") or 0) for r in recs if r["_day"] == last)
                        lines.append(f"[{title}] 지난 장({last[5:]}) {fmt(lp)} · 최근 14일: " +
                                     stats.summarize(recs, mk, x.get("capital")).split("\n")[0])
                if (st.get("dd_lock") or {}).get(mk):
                    lines.append(f"🛑 낙폭 한도로 멈춤: {st['dd_lock'][mk]} → '재개'를 보내야 다시 사요")
                if (st.get("gated") or {}).get(mk):
                    lines.append("🚫 꺼진 방식: " + ", ".join(st["gated"][mk]))
                if x.get("halted"):
                    lines.append(f"⛔ {x['halted']}")
                send("\n".join(lines))
        except Exception:
            pass
        time.sleep(30)


def set_capital(mk: str, amount) -> str:
    """오늘 투자금 바꾸기. 실행 중이면 바로 반영 (이미 산 종목은 그대로)."""
    mk = (mk or "").upper()
    if mk not in ("KR", "US"):
        return "시장은 KR(국장) 또는 US(미장) 이에요."
    try:
        v = float(str(amount).replace(",", "").replace("$", "").replace("원", ""))
    except ValueError:
        return "숫자로 적어 주세요."
    lo = 50000 if mk == "KR" else 30
    if v < lo:
        return f"너무 적어요 (최소 {lo:,})."
    st = M.load_settings()
    st.setdefault("capital", {})[mk] = v
    M.save_settings(st)
    b = STATE["bots"].get(mk)
    if b is not None and hasattr(b, "risk"):
        b.risk.capital = v
        if b.risk.halted and "손실 한도" in b.risk.halted and b.risk.realized > -b.risk.loss_limit:
            b.risk.halted = None   # 한도를 늘려서 아직 여유가 생기면 다시 매매
        b.log(f"오늘 투자금을 {M.money(mk, v)} 으로 바꿨어요 (하루 손실 한도 {M.money(mk, b.risk.loss_limit)})")
    return f"{'국장' if mk == 'KR' else '미장'} 투자금을 {M.money(mk, v)} 으로 정했어요."


def capitals() -> dict:
    cfg, st = _cfg(), M.load_settings().get("capital", {})
    return {mk: st.get(mk) or cfg["risk"][mk].get("daily_capital") for mk in ("KR", "US")}


def stop(how: str) -> str:
    if not running():
        return "실행 중이 아니에요."
    STATE["stop_all"] = True
    st = M.load_settings()
    st["resume"] = None
    M.save_settings(st)
    for b in STATE["bots"].values():
        b.stop_req = "sell" if how == "sell" else "keep"
    return ("종료 중이에요. 보유분을 모두 팔고 멈춰요." if how == "sell"
            else "종료 중이에요. 보유 종목은 시장가 손절 조건주문을 걸어 둔 채로 멈춰요.")


def status() -> dict:
    cfg = _cfg()
    out = {"running": running(), "mode": STATE.get("mode"), "repeat": STATE.get("repeat"),
           "live_enabled": cfg["live"].get("enabled"), "markets": {}, "positions": [], "closed": [], "log": []}
    for mk in M.markets_of(cfg):
        b = STATE["bots"].get(mk)
        t = STATE["threads"].get(mk)
        alive = bool(t and t.is_alive())
        snap = (b.snapshot() if b and alive else STATE["last"].get(mk)) or {"phase": "대기", "positions": [], "closed": [], "log": [],
                                                                           "realized": 0, "trades": 0}
        cap = capitals()[mk]
        out["markets"][mk] = {"capital": (b.risk.capital if b and alive and hasattr(b, "risk") else cap),
                              "invested": snap.get("invested", 0) if alive else 0,
                              "available": snap.get("available") if alive else cap,
                              "loss_limit": snap.get("loss_limit") if alive else (cap or 0) * cfg["risk"][mk].get("daily_loss_pct", 3) / 100,
                              "phase": snap.get("phase"), "running": alive, "realized": snap.get("realized", 0),
                              "trades": snap.get("trades", 0), "halted": snap.get("halted"), "universe": snap.get("universe", 0),
                              "ws": snap.get("ws"), "error": STATE["errors"].get(mk), "stop_req": snap.get("stop_req")}
        for p in snap.get("positions", []):
            out["positions"].append({**p, "market": mk})
        for c in snap.get("closed", []):
            out["closed"].append({**c, "market": mk})
        out["log"] += [f"{M.FLAG[mk]} {x}" for x in snap.get("log", [])[-60:]]
    out["log"] = sorted(out["log"], key=lambda x: x[3:11])[-120:]
    return out


PAGE = (Path(__file__).with_name("app.html")).read_text(encoding="utf-8")


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "PC주소"


def pin_ok(given: str | None) -> bool:
    pin = os.getenv("APP_PIN", "")
    return bool(pin) and given is not None and hmac.compare_digest(str(given), pin)


class H(BaseHTTPRequestHandler):
    def _local(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _authed(self) -> bool:
        return self._local() or pin_ok(self.headers.get("X-Pin"))

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/status":
            if not self._authed():
                return self._send(401, json.dumps({"need_pin": True}))
            return self._send(200, json.dumps(status(), ensure_ascii=False, default=str))
        self._send(404, "{}")

    def do_POST(self):
        # 다른 사이트가 이 PC 의 로봇을 조작하지 못하게: 같은 주소에서 온 요청만 받음
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", f"http://{host}"):
            return self._send(403, json.dumps({"msg": "허용되지 않은 요청"}))
        if not self._authed():
            return self._send(401, json.dumps({"msg": "PIN 이 맞지 않아요", "need_pin": True}))
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        if self.path == "/api/start":
            msg = start("live" if body.get("mode") == "live" else "paper", bool(body.get("repeat", True)))
        elif self.path == "/api/capital":
            msg = set_capital(body.get("market"), body.get("amount"))
        elif self.path == "/api/stop":
            msg = stop(body.get("how", "keep"))
        else:
            return self._send(404, "{}")
        self._send(200, json.dumps({"msg": msg}, ensure_ascii=False))


def main():
    M.load_env()
    lan = os.getenv("APP_LAN", "0") == "1"
    if lan and len(os.getenv("APP_PIN", "")) < 4:
        print("⚠️ APP_LAN=1 인데 APP_PIN(4자리 이상)이 없어서 휴대폰 접속은 꺼 둘게요. .env 에 APP_PIN 을 넣으세요.")
        lan = False
    srv = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}"
    print(f"토스 단타 로봇 화면: {url}  (이 창을 닫으면 로봇도 꺼져요)")
    if lan:
        print(f"📱 같은 와이파이의 휴대폰에서: http://{lan_ip()}:{PORT}  (PIN 필요)")
        print("   윈도우 방화벽 창이 뜨면 '개인 네트워크'만 허용을 누르세요.")
    from .telegram_ctl import TelegramControl
    tc = TelegramControl(start, stop, status, set_capital)
    tc.resume_fn = resume_dd
    tc.lock_fn = lock_cmd
    tc.holdings_fn, tc.set_lock_fn, tc.sell_one_fn, tc.lock_all_fn = holdings, set_lock, sell_one, lock_all
    if tc.enabled:
        tc.start()
        threading.Thread(target=_briefing_loop, args=(lambda t: tc.send(t),), daemon=True).start()
        print("💬 텔레그램으로도 조작할 수 있어요 (봇 채팅방에서 '상태' 를 보내 보세요).")
    if os.getenv("APP_HEADLESS", "0") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    if os.getenv("APP_HEADLESS", "0") == "1":
        threading.Thread(target=_watchdog, daemon=True).start()     # 서버: 먹통이면 스스로 다시 켜기
    # 서버용: 꺼지기 전에 실행 중이었으면 다시 켜기 (재부팅·업데이트 뒤 자동 복구)
    if os.getenv("APP_RESUME", "0") == "1":
        mode = M.load_settings().get("resume")
        if mode in ("paper", "live"):
            msg = start(mode, True)
            print("재부팅 전 상태로 다시 시작:", msg)
            if tc.enabled:
                tc.send(f"🔄 서버가 다시 켜져서 {'실전' if mode == 'live' else '연습'} 모드를 이어서 시작했어요. {msg}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
