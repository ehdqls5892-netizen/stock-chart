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

# 데스크 자료·검색·종목 상세 (네트워크 없이)
FAKE = {"report": {"date": "2026-11-04", "plans": {"006400": {"name": "삼성SDI", "src": "픽", "score": 70, "stop": 1, "entryLow": 2, "entryHigh": 3, "t1": 4},
                                                  "000660": {"name": "SK하이닉스", "src": "1순위", "score": 60, "stop": 1}}},
        "live": {"plans": {}, "watch": ["005930"]}, "earn": {"upcoming": {"005930": "2026-11-20", "123456": "2026-11-21"}, "good": {"000660": {"text": "좋음"}},
                 "events": {"005930": {"name": "삼성전자", "bias": "기대 우세", "time": "장 시작 전"}},
                 "analyst": {"005930": {"avg": 120, "up": 25.0}}},
        "chart": {"asOf": "2026-11-06", "scan": {"breakout": ["005930"]}, "stocks": {"005930": {"name": "삼성전자", "close": 1, "pct": 2, "signals": ["돌파"]}}},
        "names": {"005930": "삼성전자", "000660": "SK하이닉스"}}


def fake_json(url, ttl=300, get=None):
    for k in ("report", "live", "earn"):
        if f"-{k}.json" in url:
            return FAKE[k]
    if "chart-" in url:
        return FAKE["chart"]
    if "names.json" in url:
        return FAKE["names"]
    if "picks.json" in url:
        return {"picks": [{"code": "005930", "name": "삼성전자", "setup": "fs", "close": 100, "ev": {"ai": {"view": "긍정", "adj": 1}}}]}
    return None


MB._get_json = fake_json
dk = MB.desk("KR")
assert [p["name"] for p in dk["report"]["plans"]] == ["SK하이닉스", "삼성SDI"]          # 1순위 먼저
assert dk["live"]["watch"][0]["name"] == "삼성전자" and dk["scan"]["breakout"][0]["signals"] == ["돌파"]
e0 = dk["earn"]["upcoming"][0]
assert e0["name"] == "삼성전자" and e0["time"] == "장 시작 전" and e0["bias"] == "호재" and e0["tgt"] == "+25%", e0
assert "실적 발표 기대가 커요" in e0["why"] and "AI가 좋게 봐요" in e0["why"], e0
assert dk["earn"]["upcoming"][1]["name"] == "123456" and dk["earn"]["upcoming"][1]["bias"] == "정보 적음"   # 이름 못 찾으면 코드
assert dk["earn"]["good"][0]["bias"] == "호재"
ctx = MB.context("KR", {"bad": {"9": {}}, "upcoming": {}}, {"impact": {"8": {"impact": "호재", "title": "수주"}, "9": {"impact": "호재"}}}, {"8": {"label": "주의"}})
assert MB.tags("KR", "8", ctx)["bias"] == "엇갈림" and MB.tags("KR", "9", ctx)["bias"] == "악재"     # 실적 나쁨은 악재 우선
# 두산처럼: 목표가는 +200% 이지만 1년 고점 대비 -74% 장기 하락 → 목표가를 호재로 안 셈, 악재
ctx["sc"] = {"7": {"ld": True, "dd": -0.74, "c": -4, "close": 100}}
ctx["an"] = {"7": {"up": 205.0}}
t7 = MB.tags("KR", "7", ctx)
assert t7["bias"] == "악재" and any("오래 내리는 중" in w for w in t7["why"]) and any("믿기 어려워요" in w for w in t7["why"]), t7
ctx["sc"]["6"] = {"c": 1, "d20": 0.2}
ctx["an"]["6"] = {"up": 5.0}
assert MB.tags("KR", "6", ctx)["bias"] == "악재"                              # 많이 올랐고 목표가까지 남은 게 적음
assert MB.search("삼성")[0]["code"] == "005930" and MB.search("123456")[0]["market"] == "KR" and MB.search("nvda")[0]["code"] == "NVDA"
bb = [(f"2026-10-{i:02d}", 100 + i, 102 + i, 99 + i, 101 + i, 1000.0) for i in range(1, 31)]
A.quotes = lambda syms: {}
MB.est = lambda mk, code: {}
sd = MB.stock("KR", "005930", get_bars=lambda c: bb)
assert sd["name"] == "삼성전자" and sd["score"] is not None and sd["stop"] < sd["prev"] and sd["ai"]["view"] == "긍정"
assert sd["_x"]["setup"] == "fs" and sd["_x"]["close"] == sd["prev"]
assert "장중" in MB.buy("KR", "005930")                                              # 로봇이 장 밖이면 안 받음

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

# 오늘 살 종목 예상: 봇 규칙(AI 부정·손익비·업종 2종목·빈자리·손실 한도) 그대로
pkd = {"date": "2026-11-06", "picks": [
    {"code": "A", "name": "가", "score": 9, "close": 100, "stop": 92, "sector": "S", "ai": {"view": "긍정"}},
    {"code": "B", "name": "나", "score": 8, "close": 100, "stop": 92, "sector": "S", "ai": {"view": "중립"}},
    {"code": "C", "name": "다", "score": 8, "close": 100, "stop": 92, "sector": "S", "ai": {}},
    {"code": "D", "name": "라", "score": 7, "close": 100, "stop": 92, "sector": "T", "ai": {"view": "부정"}},
    {"code": "E", "name": "마", "score": 7, "close": 100, "stop": 92, "sector": "T", "no_buy": "손익비 1 미만", "ai": {}},
    {"code": "F", "name": "바", "score": 2, "close": 100, "stop": 92, "sector": "U", "ai": {}}]}
pl = MB.plan("US", hold=[], cap=5000, pk=pkd)
assert [x["name"] for x in pl["buy"]] == ["가", "나"] and pl["buy"][0]["qty"] == 6 and pl["buy"][1]["small"] and pl["buy"][1]["qty"] == 3, pl["buy"]
why = {x["name"]: x["why"] for x in pl["skip"]}
assert "업종" in why["다"] and "부정" in why["라"] and "손익비" in why["마"] and "기준" in why["바"], why
assert MB.next_buy_time("KR").endswith("+00:00")
print("✅ 오늘 살 종목 시험 통과")
