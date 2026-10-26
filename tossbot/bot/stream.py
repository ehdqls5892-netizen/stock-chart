"""토스증권 실시간 웹소켓 (체결·본인 주문 이벤트).

- 구독은 '선언형': 배열 1개를 보내면 그게 지금 구독 전체가 된다.
- 60초마다 PING 을 보낸다 (180초 동안 클라이언트가 아무것도 안 보내면 서버가 끊음).
- 끊기면 몇 초 뒤 다시 연결하고 같은 구독을 다시 선언한다.
"""
from __future__ import annotations

import json
import threading
import time

import websocket  # websocket-client

WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"


class Stream:
    def __init__(self, api, market: str, on_trade, on_order=None, log=print):
        self.api, self.market, self.on_trade, self.on_order, self.log = api, market.lower(), on_trade, on_order, log
        self.codes: list[str] = []
        self.ws: websocket.WebSocketApp | None = None
        self.alive = False
        self.connected = False
        self.last_msg = 0.0
        self._stop = threading.Event()
        self._n = 0

    # 구독 종목 바꾸기 (최대 95개: 연결당 100개 한도에서 주문 이벤트 몫을 남김)
    def set_codes(self, codes: list[str]):
        self.codes = list(dict.fromkeys(codes))[:95]
        self._declare()

    def _declare(self):
        if not (self.ws and self.connected):
            return
        self._n += 1
        decl = [{"id": f"req-{self._n}"}]
        if self.codes:
            decl.append({"type": f"trade:{self.market}", "codes": self.codes})
        if self.on_order and self.api.account_seq is not None:
            decl.append({"type": "personal:order", "codes": [str(self.api.account_seq)]})
        try:
            self.ws.send(json.dumps(decl))
        except Exception as e:
            self.log(f"[웹소켓] 구독 선언 실패: {e}")

    def _on_open(self, ws):
        self.connected = True
        self.log("[웹소켓] 연결됨")
        self._declare()

    def _on_message(self, ws, msg):
        self.last_msg = time.time()
        if msg == "PING":
            return
        try:
            j = json.loads(msg)
        except ValueError:
            return
        t = j.get("type")
        if t == "message":
            topic = j.get("topic", "")
            data = j.get("data") or {}
            if topic.startswith("trade:"):
                sym = topic.split(":", 2)[2]
                try:
                    self.on_trade(sym, float(data["price"]), float(data.get("volume") or 0), data.get("timestamp"))
                except Exception as e:
                    self.log(f"[웹소켓] 체결 처리 오류 {sym}: {e}")
            elif topic.startswith("personal:order") and self.on_order:
                try:
                    self.on_order(data)
                except Exception as e:
                    self.log(f"[웹소켓] 주문 이벤트 처리 오류: {e}")
        elif t == "subscriptions":
            rej = j.get("rejected") or []
            self.log(f"[웹소켓] 구독 {len(j.get('subscribed') or [])}개" + (f", 거부 {rej}" if rej else ""))
        elif t == "error":
            self.log(f"[웹소켓] 오류 프레임: {j.get('error')}")

    def _on_close(self, ws, code, reason):
        self.connected = False
        self.log(f"[웹소켓] 끊김 ({code} {reason})")

    def _on_error(self, ws, err):
        self.log(f"[웹소켓] 오류: {err}")

    def _pinger(self):
        while not self._stop.is_set():
            self._stop.wait(55)
            if self.ws and self.connected:
                try:
                    self.ws.send("PING")
                except Exception:
                    pass

    def _run(self):
        backoff = 2
        while not self._stop.is_set():
            try:
                tok = self.api.token()
                self.ws = websocket.WebSocketApp(
                    WS_URL, header=[f"Authorization: Bearer {tok}"],
                    on_open=self._on_open, on_message=self._on_message,
                    on_close=self._on_close, on_error=self._on_error)
                self.ws.run_forever(ping_interval=0)
                backoff = 2 if self.last_msg and time.time() - self.last_msg < 120 else min(backoff * 2, 60)
            except Exception as e:
                self.log(f"[웹소켓] 연결 실패: {e}")
                backoff = min(backoff * 2, 60)
            self.connected = False
            if not self._stop.is_set():
                self._stop.wait(backoff)

    def start(self):
        self.alive = True
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._pinger, daemon=True).start()

    def stop(self):
        self._stop.set()
        self.alive = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
