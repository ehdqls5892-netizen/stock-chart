"""휴대폰 앱 화면: 텔레그램 명령을 화면용으로 받기, 후보 정리, 버튼 처리, 다른 사이트 요청 막기."""
import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import app as A, mobile as MB  # noqa: E402
from bot.telegram_ctl import TelegramControl  # noqa: E402

st = {"running": False, "mode": None, "markets": {"KR": {"capital": 1000000}}, "positions": [], "closed": [], "log": []}
w = MB.make(TelegramControl(lambda *a: "시작", lambda *a: "끔", lambda: st, lambda mk, v: f"{mk} {v:g}"))
assert any("투자금" in t for t in w.run("투자금")), w.out
assert "버튼" in MB.post("/api/m/cmd", {"text": "실전 시작 확인"})["msg"]            # 위험한 명령은 글로 못 함
d = MB.picks("KR", get=lambda u: {"date": "2026-11-06", "regime": True, "picks": [
    {"code": "1", "name": "가", "setup": "fs", "close": 100, "stop": 93, "score": 5, "reason": "r",
     "ev": {"ai": {"view": "부정", "adj": -3, "veto": "희석", "final": True, "x": 1}, "news": ["a"] * 9}},
    {"code": "2", "name": "나", "setup": "hi52", "close": 1, "stop": 1, "score": 1}]})
assert [x["name"] for x in d["picks"]] == ["가"] and d["picks"][0]["grade"] == "A" and d["picks"][0]["ai"]["veto"] == "희석"
assert len(d["picks"][0]["news"]) == 5 and "x" not in d["picks"][0]["ai"]

A.holdings = lambda: []
A.status = lambda: dict(st)
A.capitals = lambda: {"KR": 1000000, "US": 1000}
srv = ThreadingHTTPServer(("127.0.0.1", 0), A.H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"
h = json.loads(urllib.request.urlopen(base + "/api/m/home").read())
assert h["holdings"] == [] and h["capitals"]["KR"] == 1000000
assert b"<title>" in urllib.request.urlopen(base + "/m").read()
assert urllib.request.urlopen(base + "/apple-touch-icon.png").read()[:4] == b"\x89PNG"


def post(path, origin=None, xreq=True):
    r = urllib.request.Request(base + path, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    if origin:
        r.add_header("Origin", origin)
    if xreq:
        r.add_header("X-Req", "1")
    try:
        return urllib.request.urlopen(r).status
    except urllib.error.HTTPError as e:
        return e.code


assert post("/api/m/lockall", "https://evil.example") == 403                    # 다른 사이트에서 온 요청
assert post("/api/m/lockall", "https://bot.tail123.ts.net", xreq=False) == 403  # 앱이 보낸 표시 없음
assert post("/api/m/lockall", "https://bot.tail123.ts.net") == 200              # 내 Tailscale 주소의 앱
srv.shutdown()
print("✅ 휴대폰 앱 시험 통과")
