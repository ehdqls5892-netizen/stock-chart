"""매수 방식 6가지 시험: 가짜 1분봉으로 각 방식이 제대로 잡히는지 (python -m tests.test_setups)."""
import datetime as dt
import json
from pathlib import Path

from bot.strategy import Bar, SymState, entry_signal
from bot.util import TZ

KR = TZ["KR"]
OPEN = dt.datetime(2026, 10, 27, 9, 0, tzinfo=KR)
cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
cfg["market"] = "KR"


def make(path, prev_close=10000, upto=None):
    """path(m) -> (o,h,l,c,v). upto 분까지 봉을 만들고, 그 다음 분 시각을 '지금'으로."""
    st = SymState("TEST", "시험주", prev_close=prev_close)
    for m in range(upto):
        o, h, l, c, v = path(m)
        t = OPEN + dt.timedelta(minutes=m)
        st.bars[t] = Bar(t, o, h, l, c, v)
    now = OPEN + dt.timedelta(minutes=upto, seconds=5)
    st.last = st.bars[OPEN + dt.timedelta(minutes=upto - 1)].c
    st.last_ts = now.timestamp()
    return st, now


def bar(c, v=1000, up=True, w=10):
    o = c - w if up else c + w
    return (o, max(o, c) + 5, min(o, c) - 5, c, v)


def check(name, st, now, ctx=None, expect=None):
    sig = entry_signal(st, now, OPEN, cfg, ctx or {})
    got = sig["setup"] if sig else None
    ok = got == expect
    print(f"{'✅' if ok else '❌'} {name}: {got} {('· ' + sig['reason']) if sig else ''}")
    assert ok, f"{name}: 기대 {expect}, 결과 {got}"


# ① 시초 돌파: 15분 10000~10100 → 20분째 10130 거래량 3배
def orb(m):
    if m < 15: return bar(10050 + (50 if m % 2 else -40))
    if m < 19: return bar(10080)
    return bar(10125, 3200)
check("시초 돌파", *make(orb, upto=20), expect="orb")

# ② VWAP 눌림 반등: 아침 +4.5% 급등 → VWAP 근처까지 천천히 하락 → 쉬다가 → 양봉 반등
from bot.strategy import session_bars, vwap
def morning(m):
    if m < 15: return bar(10000 + m * 32, 1000)                 # 10000 → 10450
    return bar(10450 - (m - 15) * 9, 800, up=False)             # 서서히 내려옴
st, _ = make(morning, upto=40)
v0 = vwap(session_bars(st, OPEN))
for k in range(4):                                                # VWAP 바로 위·아래에서 쉼
    t = OPEN + dt.timedelta(minutes=40 + k)
    st.bars[t] = Bar(t, v0 * 1.001, v0 * 1.002, v0 * 0.998, v0 * 0.9995, 600)
t = OPEN + dt.timedelta(minutes=44)
st.bars[t] = Bar(t, v0, v0 * 1.006, v0 * 0.999, v0 * 1.005, 1500)   # 반등 양봉
st.last = v0 * 1.005
now = OPEN + dt.timedelta(minutes=45, seconds=5); st.last_ts = now.timestamp()
check("VWAP 눌림 반등", st, now, expect="vwap")

# ③ 오늘 고점 재돌파: 오전 고점 10300 → 10분 동안 10250~10290 좁게 → 10310 돌파 거래량 2배
def hod(m):
    if m < 15: return bar(10000 + m * 5, 1500)
    if m < 30: return bar(10075 + (m - 15) * 15, 1500)   # → 10300 근처
    if m < 50: return bar(10200, 800, up=False)
    if m < 60: return (10260, 10285, 10250, 10270, 700)    # 고점 아래 받침
    return bar(10312, 3000)
check("오늘 고점 재돌파", *make(hod, upto=61), expect="hod")

# ④ 지지선 반등: 일봉 지지선 9600 에 닿고 반등
def sup(m):
    if m < 30: return bar(9900 - m * 10, 1000, up=False)  # 9900 → 9610
    if m < 34: return (9620, 9630, 9590, 9605, 900)
    return bar(9665, 2500)
check("지지선 반등", *make(sup, upto=35), {"daily": {"TEST": {"support": 9600, "label": "관망"}}}, expect="support")

# ⑤ 레드→그린: 어제 10000, 오늘 9800 시작 → 10010 회복 (거래량 3배)
def r2g(m):
    if m < 15: return bar(9800 + m * 8, 1000)
    if m < 25: return bar(9930 + (m - 15) * 5, 1000)
    return bar(10012, 3500)
cfg["strategy"]["orb_enabled"] = False      # 이 경로는 시초 돌파도 같이 성립 → 레드→그린만 따로 확인
check("레드→그린 (단독)", *make(r2g, upto=26), expect="r2g")
cfg["strategy"]["orb_enabled"] = True
check("레드→그린 + 시초 돌파 동시 → 더 센 쪽", *make(r2g, upto=26), expect="orb")

# ⑥ 데스크 가격 계획: 진입 구간 9900~10000, 손절 9600
def plan(m):
    if m < 20: return bar(10100 - m * 8, 1000, up=False)
    return bar(9960, 1600)
check("데스크 계획", *make(plan, upto=21), {"plans": {"TEST": {"stop": 9600, "entryLow": 9900, "entryHigh": 10000, "src": "1순위"}}}, expect="plan")

# ⑦ 장초반 출발: 데스크·차트가 고른 종목이 갭 +1.5% 로 시작해 첫 2분 고점을 바로 돌파 (15분 안 기다림)
def opn(m):
    return [(10150, 10200, 10140, 10190, 20000), (10190, 10205, 10180, 10200, 9000), (10200, 10240, 10198, 10232, 9000)][m]
watched = {"watched": {"TEST"}, "daily": {"TEST": {"vol_min": 1000, "scan": "돌파"}}}
check("장초반 출발 (3분째)", *make(opn, upto=3), watched, expect="open")
check("장초반 출발 — 고르지 않은 종목은 안 삼", *make(opn, upto=3), {"daily": {"TEST": {"vol_min": 1000}}}, expect=None)
check("장초반 출발 — 데스크 1차 목표 근처면 안 삼", *make(opn, upto=3),
      {**watched, "plans": {"TEST": {"stop": 9800, "entryLow": 9900, "entryHigh": 10000, "t1": 10300}}}, expect=None)
def gap_big(m):
    return [(10900, 10950, 10880, 10940, 20000), (10940, 10960, 10930, 10950, 9000), (10950, 11000, 10948, 10990, 9000)][m]
check("장초반 출발 — 갭 +9% 는 너무 올라서 안 삼", *make(gap_big, upto=3), watched, expect=None)

# ⑧ 실적 서프라이즈: 갭 +9% 라도 실적이 좋게 나온 종목이면 장초반 출발로 삼
check("실적 서프라이즈 (갭 +9%)", *make(gap_big, upto=3),
      {"earn_good": {"TEST": {"date": "2026-10-26", "text": "예상보다 좋음"}}, "daily": {"TEST": {"vol_min": 1000}}}, expect="open")

# ⑨ 프리마켓 갭 종목: 데스크가 안 고른 종목이라도 장 전에 오른 종목이면 장초반 출발로 확인
check("프리마켓 갭 종목 (고르지 않았어도)", *make(opn, upto=3), {"pre": {"TEST": 2.4}, "daily": {"TEST": {"vol_min": 1000}}}, expect="open")
check("프리마켓 갭 +9% 도 10% 까지는", *make(gap_big, upto=3), {"pre": {"TEST": 8.8}, "daily": {"TEST": {"vol_min": 1000}}}, expect="open")
check("프리마켓 갭 종목 — 장 열린 뒤 거래량 약하면 안 삼", *make(opn, upto=3), {"pre": {"TEST": 2.4}, "daily": {"TEST": {"vol_min": 9000}}}, expect=None)

# ⑨ 갭하락 반등(미장 전용): −5% 갭으로 시작 → 더 무너지지 않고 시가 근처에서 버팀
cfg_us = json.loads(json.dumps(cfg)); cfg_us["market"] = "US"; cfg_us["strategy"]["setups"]["gapdown"]["enabled"] = True   # 기본은 꺼짐(백테스트 손실)
cfg_us["strategy"]["price_range"]["US"] = [1, 100000]
def gd(m):
    return [(9500, 9520, 9440, 9480, 3000), (9480, 9530, 9470, 9520, 2500), (9520, 9560, 9510, 9550, 2500)][m]
st, now = make(gd, upto=3)
sig = entry_signal(st, now, OPEN, cfg_us, {})
print("갭하락 반등(미장):", sig and sig["setup"], sig and sig["reason"])
assert sig and sig["setup"] == "gapdown"
assert entry_signal(*make(gd, upto=3), OPEN, cfg, {}) is None, "국장에선 갭하락 반등을 안 함"
def gd_fall(m):
    return [(9500, 9510, 9400, 9420, 3000), (9420, 9425, 9300, 9310, 2500), (9310, 9320, 9250, 9260, 2500)][m]
assert entry_signal(*make(gd_fall, upto=3), OPEN, cfg_us, {}) is None, "계속 무너지는데 삼"

# 신호 없음 · 실적 발표 임박
check("옆으로만(신호 없음)", *make(lambda m: bar(10000, 1000), upto=40), expect=None)
check("실적 임박이면 안 삼", *make(orb, upto=20), {"earn_block": {"TEST"}}, expect=None)
print("\n✅ 매수 방식 시험 통과")
# 시장별 켜고 끄기: 장초반 출발은 국장만 (미장 백테스트에서 손실)
cfg_us2 = json.loads(json.dumps(cfg)); cfg_us2["market"] = "US"; cfg_us2["strategy"]["price_range"]["US"] = [1, 100000]
assert entry_signal(*make(opn, upto=3), OPEN, cfg_us2, watched) is None, "미장인데 장초반 출발로 삼"
print("✅ 시장별 방식 켜기·끄기 확인")
