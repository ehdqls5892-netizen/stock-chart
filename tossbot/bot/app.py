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
