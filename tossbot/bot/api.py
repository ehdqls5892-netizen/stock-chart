"""토스증권 Open API REST 클라이언트.

- 토큰은 파일(.token.json)에 저장해 재사용한다. 토스는 클라이언트당 토큰이 1개만 유효해서
  매번 새로 받으면 다른 프로그램의 토큰이 무효가 된다.
- API 그룹별 초당 한도를 넘지 않게 간격을 둔다.
- 429·5xx 는 잠깐 기다렸다 다시 시도한다.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import requests

BASE = "https://openapi.tossinvest.com"

# 문서의 그룹별 초당 한도보다 조금 느리게 (여유분)
GROUP_RATE = {
    "AUTH": 2, "ACCOUNT": 0.8, "ASSET": 4, "STOCK": 4, "STOCK_ALL": 0.8,
    "MARKET_INFO": 2, "MARKET_DATA": 12, "MARKET_DATA_CHART": 15, "RANKING": 4,
    "ORDER": 6, "ORDER_HISTORY": 4, "ORDER_INFO": 2.5,
    "CONDITIONAL_ORDER": 4, "CONDITIONAL_ORDER_HISTORY": 8,
}


class TossError(Exception):
    def __init__(self, status: int, code: str, message: str, data=None):
        super().__init__(f"[{status} {code}] {message}")
        self.status, self.code, self.message, self.data = status, code, message, data


class TossAPI:
    def __init__(self, client_id: str, client_secret: str, token_file: str | Path = ".token.json",
                 account_seq: int | None = None, log=print):
        if not client_id or not client_secret:
            raise ValueError(".env 에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 없어요.")
        self.cid, self.secret = client_id, client_secret
        self.token_file = Path(token_file)
        self.account_seq = account_seq
        self.log = log
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "toss-daytrade-bot/1.0"
        self._tok: dict | None = None
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    # ───────── 토큰 ─────────
    def token(self) -> str:
        with self._lock:
            if self._tok is None and self.token_file.exists():
                try:
                    t = json.loads(self.token_file.read_text())
                    if t.get("cid") == self.cid:
                        self._tok = t
                except Exception:
                    pass
            if self._tok and self._tok.get("exp", 0) - time.time() > 600:
                return self._tok["access_token"]
            self._wait("AUTH")
            r = self.s.post(f"{BASE}/oauth2/token", timeout=15, data={
                "grant_type": "client_credentials", "client_id": self.cid, "client_secret": self.secret})
            if r.status_code != 200:
                raise TossError(r.status_code, "token", _err_text(r))
            j = r.json()
            self._tok = {"cid": self.cid, "access_token": j["access_token"],
                         "exp": time.time() + int(j.get("expires_in", 86400))}
            try:
                self.token_file.write_text(json.dumps(self._tok))
                os.chmod(self.token_file, 0o600)
            except Exception:
                pass
            return self._tok["access_token"]

    def drop_token(self):
        with self._lock:
            self._tok = None
            try:
                self.token_file.unlink()
            except FileNotFoundError:
                pass

    # ───────── 공통 요청 ─────────
    def _wait(self, group: str):
        rate = GROUP_RATE.get(group, 3)
        gap = 1.0 / rate
        last = self._last.get(group, 0)
        d = last + gap - time.time()
        if d > 0:
            time.sleep(d)
        self._last[group] = time.time()

    def req(self, method: str, path: str, group: str, params=None, body=None, account=False, retries=3):
        for attempt in range(retries + 1):
            self._wait(group)
            h = {"Authorization": f"Bearer {self.token()}"}
            if account:
                if self.account_seq is None:
                    raise ValueError("계좌(accountSeq)가 아직 정해지지 않았어요.")
                h["X-Tossinvest-Account"] = str(self.account_seq)
            try:
                r = self.s.request(method, BASE + path, params=params, json=body, headers=h, timeout=15)
            except requests.RequestException as e:
                if attempt < retries:
                    time.sleep(1 + attempt)
                    continue
                raise TossError(0, "network", str(e))
            if r.status_code == 401 and attempt < retries:
                self.drop_token()
                continue
            if r.status_code == 429 and attempt < retries:
                time.sleep(float(r.headers.get("Retry-After", 1)) + 0.2 * attempt)
                continue
            if r.status_code >= 500 and attempt < retries and method == "GET":
                time.sleep(1 + attempt)
                continue
            if r.status_code >= 400:
                code, msg, data = "http", _err_text(r), None
                try:
                    e = r.json().get("error", {})
                    code, msg, data = e.get("code", code), e.get("message", msg), e.get("data")
                except Exception:
                    pass
                raise TossError(r.status_code, code, msg, data)
            if not r.content:
                return None
            j = r.json()
            return j.get("result", j) if isinstance(j, dict) else j
        raise TossError(0, "retry", "재시도 횟수 초과")

    # ───────── 시세·정보 ─────────
    def accounts(self):
        return self.req("GET", "/api/v1/accounts", "ACCOUNT")

    def prices(self, symbols: list[str]):
        out = []
        for i in range(0, len(symbols), 200):
            out += self.req("GET", "/api/v1/prices", "MARKET_DATA", {"symbols": ",".join(symbols[i:i + 200])}) or []
        return out

    def orderbook(self, symbol: str):
        return self.req("GET", "/api/v1/orderbook", "MARKET_DATA", {"symbol": symbol})

    def candles(self, symbol: str, interval="1m", count=60):
        r = self.req("GET", "/api/v1/candles", "MARKET_DATA_CHART",
                     {"symbol": symbol, "interval": interval, "count": count})
        return (r or {}).get("candles", [])

    def stocks(self, symbols: list[str]):
        out = []
        for i in range(0, len(symbols), 100):
            out += self.req("GET", "/api/v1/stocks", "STOCK", {"symbols": ",".join(symbols[i:i + 100])}) or []
        return out

    def warnings(self, symbol: str):
        return self.req("GET", f"/api/v1/stocks/{symbol}/warnings", "STOCK") or []

    def rankings(self, type_: str, market: str, duration: str, count=50, exclude_caution=True):
        return self.req("GET", "/api/v1/rankings", "RANKING", {
            "type": type_, "marketCountry": market, "duration": duration, "count": count,
            "excludeInvestmentCaution": str(exclude_caution).lower()}) or {}

    def calendar(self, market: str):
        return self.req("GET", f"/api/v1/market-calendar/{market}", "MARKET_INFO")

    # ───────── 계좌 ─────────
    def holdings(self):
        return self.req("GET", "/api/v1/holdings", "ASSET", account=True)

    def buying_power(self, currency="KRW"):
        return self.req("GET", "/api/v1/buying-power", "ORDER_INFO", {"currency": currency}, account=True)

    def sellable(self, symbol: str):
        return self.req("GET", "/api/v1/sellable-quantity", "ORDER_INFO", {"symbol": symbol}, account=True)

    # ───────── 주문 ─────────
    def create_order(self, symbol, side, quantity, order_type="LIMIT", price=None, client_id=None):
        body = {"clientOrderId": client_id or uuid.uuid4().hex[:32], "symbol": symbol, "side": side,
                "orderType": order_type, "quantity": str(quantity)}
        if order_type == "LIMIT":
            body["price"] = str(price)
        return self.req("POST", "/api/v1/orders", "ORDER", body=body, account=True, retries=0)

    def cancel_order(self, order_id):
        return self.req("POST", f"/api/v1/orders/{order_id}/cancel", "ORDER", body={}, account=True, retries=0)

    def get_order(self, order_id):
        return self.req("GET", f"/api/v1/orders/{order_id}", "ORDER_HISTORY", account=True)

    def list_orders(self, status="OPEN", symbol=None):
        p = {"status": status}
        if symbol:
            p["symbol"] = symbol
        r = self.req("GET", "/api/v1/orders", "ORDER_HISTORY", p, account=True)
        return r.get("orders", r) if isinstance(r, dict) else (r or [])

    # ───────── 조건주문 (토스 서버가 감시) ─────────
    def create_oco_exit(self, symbol, quantity, tp_trigger, tp_price, sl_trigger, sl_price, expire, client_id=None):
        """보유 수량에 익절·손절을 한 번에 건다. 둘 중 하나가 체결되면 다른 하나는 자동 취소."""
        body = {"symbol": symbol, "type": "OCO", "quantity": str(quantity), "orderType": "LIMIT",
                "expireDate": expire, "clientOrderId": client_id or uuid.uuid4().hex[:32],
                "first": {"orderSide": "SELL", "triggerPrice": str(tp_trigger), "orderPrice": str(tp_price)},
                "second": {"orderSide": "SELL", "triggerPrice": str(sl_trigger), "orderPrice": str(sl_price)}}
        return self.req("POST", "/api/v1/conditional-orders", "CONDITIONAL_ORDER", body=body, account=True, retries=0)

    def modify_oco_exit(self, cond_id, quantity, tp_trigger, tp_price, sl_trigger, sl_price, expire):
        body = {"type": "OCO", "quantity": str(quantity), "orderType": "LIMIT", "expireDate": expire,
                "first": {"orderSide": "SELL", "triggerPrice": str(tp_trigger), "orderPrice": str(tp_price)},
                "second": {"orderSide": "SELL", "triggerPrice": str(sl_trigger), "orderPrice": str(sl_price)}}
        return self.req("POST", f"/api/v1/conditional-orders/{cond_id}/modify", "CONDITIONAL_ORDER",
                        body=body, account=True, retries=0)

    def create_stop_market(self, symbol, quantity, trigger, expire, client_id=None):
        """손절 조건주문: 가격이 trigger 이하로 오면 '시장가'로 판다 (토스 서버가 감시).
        시장가라서 아침에 갭하락으로 시작해도 바로 팔린다. PC 가 꺼져 있어도 작동."""
        body = {"symbol": symbol, "type": "SINGLE", "quantity": str(quantity), "orderType": "MARKET",
                "expireDate": expire, "clientOrderId": client_id or uuid.uuid4().hex[:32],
                "first": {"orderSide": "SELL", "triggerPrice": str(trigger)}}
        return self.req("POST", "/api/v1/conditional-orders", "CONDITIONAL_ORDER", body=body, account=True, retries=0)

    def modify_stop_market(self, cond_id, quantity, trigger, expire):
        """정정: 토스는 정정하면 옛 조건주문을 없애고 '새 번호'로 다시 만든다 → 새 번호를 돌려준다."""
        body = {"type": "SINGLE", "orderType": "MARKET", "expireDate": expire,
                "first": {"orderSide": "SELL", "triggerPrice": str(trigger), "quantity": str(quantity)}}
        r = self.req("POST", f"/api/v1/conditional-orders/{cond_id}/modify", "CONDITIONAL_ORDER",
                     body=body, account=True, retries=0) or {}
        return r

    def replace_stop_market(self, cond_id, symbol, quantity, trigger, expire):
        """손절선 바꾸기 (안전한 순서). 반환: (새 조건주문 번호, 방법)
        1) 정정 시도 → 성공하면 새 번호 사용
        2) 정정이 안 되면: 새 손절 주문을 '먼저' 걸고 → 옛 것을 취소 (손절이 비는 순간이 없게)
        새 주문도 못 걸면 TossError 를 그대로 올린다 (부르는 쪽에서 옛 주문이 살아 있는지 확인)."""
        try:
            r = self.modify_stop_market(cond_id, quantity, trigger, expire)
            return str(r.get("conditionalOrderId") or cond_id), "정정"
        except TossError as e:
            first_err = e
        r = self.create_stop_market(symbol, quantity, trigger, expire)
        new_id = str(r["conditionalOrderId"])
        try:
            self.cancel_conditional(cond_id)
        except TossError:
            pass  # 정정 실패 과정에서 이미 없어졌을 수 있음
        return new_id, f"새로 걸고 옛 것 취소 (정정 오류: {first_err.code})"

    def is_alive(self, cond_id) -> bool:
        try:
            return (self.get_conditional(cond_id) or {}).get("status") in ("WATCHING", "PAUSED")
        except TossError:
            return False

    def open_stops_for(self, symbol):
        try:
            return [c for c in self.list_conditional("OPEN") if str(c.get("symbol")) == str(symbol)]
        except TossError:
            return []

    def cancel_conditional(self, cond_id):
        return self.req("DELETE", f"/api/v1/conditional-orders/{cond_id}", "CONDITIONAL_ORDER", account=True, retries=0)

    def get_conditional(self, cond_id):
        return self.req("GET", f"/api/v1/conditional-orders/{cond_id}", "CONDITIONAL_ORDER_HISTORY", account=True)

    def list_conditional(self, status="OPEN"):
        r = self.req("GET", "/api/v1/conditional-orders", "CONDITIONAL_ORDER_HISTORY", {"status": status}, account=True)
        return r.get("conditionalOrders", r.get("items", [])) if isinstance(r, dict) else (r or [])


def _err_text(r) -> str:
    try:
        return r.text[:300]
    except Exception:
        return ""
