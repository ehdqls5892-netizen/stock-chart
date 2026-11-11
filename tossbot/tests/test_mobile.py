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
_cfg0 = MB._cfg
MB._cfg = lambda: (lambda d: (d["daily_swing"].update(need_target=False, skip_spike=False, min_chart_q=-99, stop_by_market={}, risk_pct=1.0, risk_by_grade={"A": 1.0, "B": 0.6}, strong={"enabled": False}), d)[1])(_cfg0())   # 시험 종목엔 목표가 없음
pl = MB.plan("US", hold=[], cap=5000, pk=pkd)
assert [x["name"] for x in pl["buy"]] == ["가", "나"] and pl["buy"][0]["qty"] == 6 and pl["buy"][1]["small"] and pl["buy"][1]["qty"] == 3, pl["buy"]
why = {x["name"]: x["why"] for x in pl["skip"]}
assert "업종" in why["다"] and "부정" in why["라"] and "손익비" in why["마"] and "기준" in why["바"], why
assert MB.next_buy_time("KR").endswith("+00:00")
assert pl["pause"] is None                                                       # 미장은 시장 나쁨이어도 그대로
# 국장: 지수가 50일선 아래(시장 나쁨)면 봇이 새로 안 사니, 앱도 '쉼'으로 보여 줌
pk2 = {**pkd, "regime": False, "index": {"close": 5018.0, "ma50": 6357.0, "ma_n": 50}}
assert MB.plan("KR", hold=[], cap=10_000_000, pk=pk2)["buy"]                     # 기본: 시장 나쁨이어도 삼 (사용자 설정)
_cfg1 = MB._cfg
MB._cfg = lambda: (lambda d: (d["daily_swing"].update(any_regime_by_market={"KR": [], "US": ["fs"]}), d)[1])(_cfg1())   # 막는 설정을 켰을 때만 쉼
pl2 = MB.plan("KR", hold=[], cap=10_000_000, pk=pk2)
assert not pl2["buy"] and not pl2["wait"] and "50일선 아래" in pl2["pause"], pl2
assert any("시장 하락 추세" in x["why"] for x in pl2["skip"]), pl2["skip"]
assert MB.plan("KR", hold=[], cap=10_000_000, pk={**pkd, "regime": True})["buy"]
MB._cfg = _cfg1
print("✅ 오늘 살 종목 시험 통과")

# 범위 밖 종목도 토스 일봉으로 판정 (정보 적음 안 뜨게), 보유·오늘 살 종목이 악재로 바뀌면 알림
up_bars = [(f"2026-10-{i:02d}", 100 + i, 102 + i, 99 + i, 101 + i, 1000.0) for i in range(1, 31)]
dn_bars = [(f"2026-10-{i:02d}", 200 - i * 3, 201 - i * 3, 196 - i * 3, 197 - i * 3, 1000.0) for i in range(1, 31)]
ctx0 = MB.context("US", {"upcoming": {}}, {}, {})
ctx0["sc"] = {}
MB.LIVE[("US", "RKLB")] = MB.live_stats("US", "RKLB", get_bars=lambda c: up_bars)
assert MB.tags("US", "RKLB", ctx0)["bias"] != "정보 적음", MB.tags("US", "RKLB", ctx0)
MB.watch_codes = lambda mk: {"HOLD": True}
MB.name_map = lambda mk, codes, hint=None: {c: "보유종목" for c in codes}
MB.context = lambda mk, *a: ctx0
alerts = []
MB.LAST_BIAS[("US", "HOLD")] = "호재"
MB.refresh_once("US", get_bars=lambda c: dn_bars, notify=lambda *a: alerts.append(a))
assert alerts and "호재→악재" in alerts[0][1], (alerts, MB.LAST_BIAS)
print("✅ 계속 다시 보기 시험 통과")

# 업데이트 칸: 마지막 업데이트 시각·최신 여부
import bot.mobile as _MB  # noqa: E402
_MB._get_json = lambda url, ttl=300, get=None: get(url)
_MB._CACHE.clear()
_v = _MB.version(get=lambda u: [{"sha": "abcdef123", "commit": {"committer": {"date": "2000-01-01T00:00:00Z"}, "message": "새 기능\n자세히"}}])
assert _v["latest_sha"] == "abcdef1" and _v["up_to_date"] is True and _v["latest_msg"] == "새 기능", _v
_MB._CACHE.clear()
_v = _MB.version(get=lambda u: [{"sha": "abcdef123", "commit": {"committer": {"date": "2999-01-01T00:00:00Z"}, "message": "x"}}])
assert _v["up_to_date"] is False
_MB._CACHE.clear()
print("✅ 버전 표시 시험 통과")

# 오늘 새로 사지 않기
import bot.main as _M  # noqa: E402
_st = {}
_M.load_settings, _M.save_settings = (lambda: _st), (lambda d: _st.update(d))
_r = _MB.post("/api/m/nobuy", {"market": "KR", "on": True})
assert "새로 안 사요" in _r["msg"] and _MB.nobuy_state()["KR"] is True and _MB.nobuy_state()["US"] is False, _r
_MB.post("/api/m/nobuy", {"market": "KR", "on": False})
assert _MB.nobuy_state()["KR"] is False
print("✅ 오늘 새로 사지 않기 시험 통과")
_r = _MB.post("/api/m/exclude", {"market": "KR", "symbol": "068270", "on": True})
assert "안 사요" in _r["msg"] and _st["exclude"]["KR"]["codes"] == ["068270"], _st
_MB.post("/api/m/exclude", {"market": "KR", "symbol": "068270", "on": False})
assert _st["exclude"]["KR"]["codes"] == []
print("✅ 종목 빼기 시험 통과")

# 봇 판매 수익: 월별 합계 · 국장 원 · 미장 달러 그대로 · 일부 매도 포함
import json as _json, tempfile as _tf  # noqa: E402
from pathlib import Path as _P  # noqa: E402
_kr, _us = _P(_tf.mkdtemp()), _P(_tf.mkdtemp())
(_kr / "trades-2026-11-03.jsonl").write_text("\n".join(_json.dumps(r, ensure_ascii=False) for r in [
    {"t": "2026-11-03T15:20:00", "kind": "exit", "symbol": "005930", "name": "삼성전자", "qty": 10, "entry": 100, "exit_price": 110, "pnl": 1000, "exit_reason": "익절"},
    {"t": "2026-11-03T09:00:00", "kind": "entry", "symbol": "000660", "name": "SK하이닉스", "qty": 1, "entry": 100, "pnl": 0},
    {"t": "2026-10-30T15:20:00", "kind": "exit", "symbol": "000660", "name": "SK하이닉스", "qty": 1, "entry": 100, "exit_price": 90, "pnl": -10, "exit_reason": "손절"}]), encoding="utf-8")
(_us / "trades-2026-11-09.jsonl").write_text(_json.dumps({"t": "2026-11-09T15:45:00", "kind": "partial", "symbol": "SWK", "name": "Stanley", "qty": 3,
                                                         "entry": 119.17, "exit_price": 118.86, "pnl": -1.64, "exit_reason": "일부"}), encoding="utf-8")
_p = _MB.profits("2026-11", dirs={"KR": _kr, "US": _us})
assert _p["cur"]["KR"] == 1000 and _p["cur"]["US"] == -1.64 and _p["cur"]["n"] == 2, _p["cur"]
assert _p["trades"][0]["part"] and _p["trades"][0]["mk"] == "US"
assert [m["month"] for m in _p["months"]] == ["2026-11", "2026-10"] and _p["months"][1]["KR"] == -10
print("✅ 봇 판매 수익 시험 통과")
