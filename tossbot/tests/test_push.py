"""휴대폰 앱 알림: 웹 푸시 암호화(RFC 8291 시험값), 쉬운 알림 글, 알림함, 텔레그램 짧게, 급등 감시."""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot.push as P  # noqa: E402

P.DIR = Path(tempfile.mkdtemp())
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ec import derive_private_key  # noqa: E402

# 1) RFC 8291 부록 A 시험값 그대로 나와야 함
as_priv = derive_private_key(int.from_bytes(P.ub64("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
out = P.encrypt(b"When I grow up, I want to be a watermelon",
                "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4", "BTBZMqHH6r4Tts7J_aSIgg",
                salt=P.ub64("DGv6ra1nlYgDCS1FRnbzlw"), eph=as_priv)
assert P.b64u(out) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")
assert P.vapid_header("https://web.push.apple.com/abc").startswith("vapid t=") and len(P.ub64(P.public_key())) == 65

# 2) 쉬운 알림 글
s = P.simplify("🇺🇸 🔴 매도(손실) Corpay(CPAY) 3주 $300.00 → $281.00 (-6.33%) 손익 -$57.00 · 스윙 정리: 현재·미래 근거가 약해짐 (점수 -1): －음봉")
assert s["title"] == "🇺🇸 🔻 Corpay 팔았어요 · 손실 $57.00" and s["code"] == "CPAY" and "오를 근거가 약해져서" in s["body"], s
assert P.simplify("🇰🇷 ✅ 체결 NHN 10주 @ 59,200원 · 손절 49,100원")["title"] == "🇰🇷 🛒 NHN 샀어요"
assert P.simplify("🇺🇸 🧭 스윙 점검 Viatris 2거래일째 · $12.30 (+1.7%) · 계속 보유")["level"] == "low"
pl = P.simplify("🇰🇷 📋 [미리 보기] 판단표\n✅ 매수 예정\n- LG유플러스 ≈63주 × 15,800원\n   근거\n- 펌텍코리아 ≈10주\n⏭ 예비\n- 기타")
assert pl["body"] == "살 예정: LG유플러스, 펌텍코리아", pl

# 3) 알림함·받을 알림 고르기·구독·보내기(가짜 서버)
assert "올바르지" in P.subscribe({"endpoint": "http://x"})
ua = ec.generate_private_key(ec.SECP256R1())
from cryptography.hazmat.primitives import serialization  # noqa: E402
pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
P.subscribe({"endpoint": "https://web.push.apple.com/a", "keys": {"p256dh": P.b64u(pub), "auth": P.b64u(b"0123456789abcdef")}})
P.subscribe({"endpoint": "https://fcm.googleapis.com/dead", "keys": {"p256dh": P.b64u(pub), "auth": P.b64u(b"0123456789abcdef")}})
calls = []


def fake_post(url, data=None, headers=None, timeout=None):
    calls.append((url, headers))
    return SimpleNamespace(status_code=410 if "dead" in url else 201)


assert P.send_push("t", "b", post=fake_post) == 1 and len(P.subs()) == 1                 # 만료된 기기는 지움
assert calls[0][1]["Content-Encoding"] == "aes128gcm" and calls[0][1]["Authorization"].startswith("vapid t=")
P.handle("🇰🇷 🧭 스윙 점검 A 1거래일째 · 100원 (+1.0%)", "KR")
assert P.inbox(1)[0]["level"] == "low" and P.wanted(P.simplify("🇰🇷 ✅ 체결 A 1주 @ 1원")) is True
P.set_prefs({"trade": False, "telegram": "off"})
assert P.wanted(P.simplify("🇰🇷 ✅ 체결 A 1주 @ 1원")) is False and P.prefs()["telegram"] == "off"
P.set_prefs({"trade": True, "telegram": "short"})

# 4) 텔레그램은 짧게 (자잘한 건 안 보냄)
import bot.notify as N  # noqa: E402
n = N.Notifier.__new__(N.Notifier)
n.token, n.chat, n.prefix = "t", "c", "🇺🇸 "
import queue  # noqa: E402
n.q = queue.Queue()
n("🟢 매도(이익) Corpay(CPAY) 3주 $300.00 → $320.00 (+6.67%) 손익 $60.00 · 손절선 도달")
n("🧭 스윙 점검 Viatris 2거래일째 · $12.30 (+1.7%) · 계속 보유")
got = [n.q.get_nowait() for _ in range(n.q.qsize())]
assert got == ["🇺🇸 💰 Corpay 팔았어요 · 이익 $60.00\n+6.67% · 손절선 도달"], got

# 5) 급등 감시: 단계마다 하루 한 번, 큰 단계 먼저면 작은 단계는 안 보냄
import bot.alerts as AL  # noqa: E402
assert AL.crossed(6.1, AL.LEVELS_HOLD) == 5 and AL.crossed(11, AL.LEVELS_HOLD) == 10 and AL.crossed(-9, AL.LEVELS_HOLD) == -8
assert AL.crossed(2, AL.LEVELS_HOLD) is None
fired = []
assert AL._fire("k", 10, AL.LEVELS_HOLD, lambda: fired.append(10))
assert not AL._fire("k", 5, AL.LEVELS_HOLD, lambda: fired.append(5)) and fired == [10]
from bot import app as A  # noqa: E402
import datetime as dt  # noqa: E402
pos = SimpleNamespace(symbol="H1", name="보유1", qty=10, entry=100.0, sl=93.0, state="open")
A.STATE["bots"] = {"KR": SimpleNamespace(loop_on=True, broker=SimpleNamespace(pos={"H1": pos}))}
AL.watch_list = lambda mk: {"W1": ("후보1", "후보 종목 · 점수 A +8")}
bars = lambda s: [("2026-11-06", 1, 1, 1, 100.0, 1)]
pushed = []
P.push_event = lambda kind, title, body, mk=None, code=None, full="": pushed.append((kind, title, code))
AL.check("KR", quotes=lambda syms: {"H1": 106.0, "W1": 111.0, "M1": 130.0}, bars=bars,
         rankings=lambda: {"rankings": [{"symbol": "M1"}, {"symbol": "H1"}]}, names=lambda c: {"M1": "시장1"},
         now_fn=lambda mk: dt.datetime(2026, 11, 9, 10, 0))
kinds = {k: (t, c) for k, t, c in pushed}
assert set(kinds) == {"move_hold", "move_watch", "move_market"}, pushed
assert "+6.0%" in kinds["move_hold"][0] and "+11.0%" in kinds["move_watch"][0] and kinds["move_market"][1] == "M1"
pushed.clear()
AL._last_rank.clear()
AL.check("KR", quotes=lambda syms: {"H1": 106.5, "W1": 112.0, "M1": 131.0}, bars=bars, rankings=lambda: {"rankings": [{"symbol": "M1"}]},
         names=lambda c: {}, now_fn=lambda mk: dt.datetime(2026, 11, 9, 10, 2))
assert pushed == [], pushed                                                        # 같은 날 같은 단계는 다시 안 옴
print("✅ 앱 알림 시험 통과")
