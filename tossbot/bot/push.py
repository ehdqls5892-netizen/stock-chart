"""휴대폰 앱 알림 (웹 푸시) + 알림함 + 알림 글을 쉽게 줄이기.

- 로봇이 보내는 모든 알림(Notifier)이 여기를 거친다:
  1) simplify(): 긴 글 → 제목 한 줄 + 내용 한두 줄 (중학생도 읽기 쉽게)
  2) 알림함(logs/push/inbox.json)에 원래 글까지 저장 → 앱 🔔 에서 자세히 보기
  3) 중요도가 '낮음'이 아니면 앱 푸시 (홈 화면에 추가한 아이폰 16.4+ / 안드로이드 크롬)
- 웹 푸시 암호화(RFC 8291 aes128gcm)와 VAPID 서명(RFC 8292)을 cryptography 로 직접 구현 (설치가 까다로운 라이브러리 없이).
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "logs" / "push"
LOCK = threading.Lock()
CONTACT = "mailto:tossbot@users.noreply.github.com"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def ub64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _rj(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _wj(p: Path, v):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


# ───────── 쉬운 알림 글 ─────────
def _short(s: str, n: int = 110) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


EASY = [("현재·미래 근거가 약해짐", "오를 근거가 약해져서"), ("스윙 정리: ", ""), ("－", ""), ("＋", ""), ("  ", " "), (" :", ":")]


def _why(s: str) -> str:
    """'· 이유' 꼬리에서 사람이 읽을 이유 한 토막."""
    s = re.sub(r"\(.*?\)", "", s)
    for a, b in EASY:
        s = s.replace(a, b)
    return _short(s.split(" · ")[0], 60)


def simplify(text: str) -> dict:
    """로봇 알림 글 → {kind, title, body, level(high|normal|low), code?}"""
    t = (text or "").strip()
    flag = ""
    m0 = re.match(r"^(🇰🇷|🇺🇸)\s*(\[연습\]\s*)?", t)
    if m0:
        flag = m0.group(1) + " "
        t = t[m0.end():]
    first = t.split("\n")[0]
    R = lambda kind, title, body, level="normal", code=None: {"kind": kind, "title": flag + title, "body": _short(body),
                                                              "level": level, "code": code}
    m = re.search(r"(?:✅ 체결|🛒 \[연습\] 매수) (.+?)(?:\((\w[\w.]*)\))? (\d+)주 @ ([^ ·]+)(?:.*?손절 ([^ ·(]+))?", first)
    if m:
        return R("buy", f"🛒 {m.group(1)} 샀어요", f"{m.group(3)}주 · 한 주 {m.group(4)}" + (f" · 손절선 {m.group(5)}" if m.group(5) else ""),
                 "high", m.group(2))
    m = re.search(r"(🟢 매도\(이익\)|🔴 매도\(손실\)) (.+?)\((\w[\w.]*)\) (\d+)주 .*?\(([+-][\d.]+%)\) 손익 ([^ ·]+)(?: · (.*))?", first)
    if m:
        win = "이익" in m.group(1)
        return R("sell", f"{'💰' if win else '🔻'} {m.group(2)} 팔았어요 · {'이익' if win else '손실'} {m.group(6).lstrip('-+')}",
                 f"{m.group(5)} · {_why(m.group(7) or '')}", "high", m.group(3))
    m = re.search(r"🟢 (.+?) (\S+) (\d+)주 .*?\(([+-][\d.]+%)\) 손익 ([^ ·]+)", first)
    if m and "매도" not in m.group(1):
        return R("sell", f"💰 {m.group(2)} 일부 팔았어요", f"{m.group(3)}주 {m.group(4)} · {_why(m.group(1))}", "high")
    m = re.search(r"➕ 추가 매수 (\S+) (\d+)주 @ ([^ ·]+)", first)
    if m:
        return R("buy", f"➕ {m.group(1)} 더 샀어요", f"{m.group(2)}주 · 한 주 {m.group(3)}", "high")
    m = re.search(r"🎯 (\S+) .*목표가 ([^ ]+) 도달", first)
    if m:
        return R("sell", f"🎯 {m.group(1)} 목표가 도달", _short(first, 110), "high")
    m = re.search(r"🧭 스윙 정리 (\S+) .*?— (.*)", first)
    if m:
        return R("sell", f"📤 {m.group(1)} 팔기로 했어요", _why(m.group(2)), "high")
    m = re.search(r"🧭 스윙 점검 (\S+) .*?\(([+-][\d.]+%)\)", first)
    if m:
        return R("hold", f"🧭 {m.group(1)} 보유 점검 {m.group(2)}", "계속 들고 가요 · 자세한 건 알림함", "low")
    m = re.search(r"🧭 (\S+?)(?: 봇 매수 요청)? (?:건너뜀|—) ?—? ?(.*)", first)
    if m and ("건너뜀" in first or "안 샀어요" in first):
        return R("skip", f"⏭ {m.group(1)} 안 샀어요", _why(m.group(2).replace("—", "").strip()), "normal")
    if first.startswith("📋"):
        names = []
        on = False
        for ln in t.split("\n"):
            if ln.startswith("✅"):
                on = True
                continue
            if on and ln.startswith("- "):
                names.append(re.split(r" ≈| ·", ln[2:])[0])
            elif on and not ln.startswith(" "):
                on = False
        return R("plan", "📋 오늘 살 후보가 정해졌어요", ("살 예정: " + ", ".join(names[:4])) if names else "오늘은 살 종목이 없어요", "normal")
    if first.startswith("🚀") or first.startswith("📉"):
        return R("move", _short(first, 60), "\n".join(t.split("\n")[1:2]) or "탭해서 차트 보기", "normal")
    if any(first.startswith(e) or e in first[:4] for e in ("🚨", "❌", "⛔", "🛑")):
        return R("warn", "⚠️ 확인이 필요해요", _short(re.sub(r"^[^\w가-힣]+", "", first)), "high")
    if first.startswith("⚠️"):
        return R("warn", "⚠️ 알려 드려요", _short(first[2:]), "normal")
    if first.startswith(("🤖", "🔄", "🆕", "🔁")):
        return R("system", "🤖 로봇 소식", _short(first[1:]), "low")
    if first.startswith(("📊", "🌅", "🌙")):
        return R("report", _short(first, 40), _short(" ".join(t.split("\n")[1:3]) or first), "low")
    return R("info", "🔔 알림", _short(first), "normal")


# ───────── 알림함 ─────────
def inbox_add(text: str, s: dict, mk: str | None = None):
    with LOCK:
        p = DIR / "inbox.json"
        items = _rj(p, [])
        items.append({"t": time.time(), "mk": mk, **{k: s.get(k) for k in ("kind", "title", "body", "level", "code")}, "full": text[:3000]})
        _wj(p, items[-300:])


def inbox(n: int = 100) -> list:
    return list(reversed(_rj(DIR / "inbox.json", [])[-n:]))


# ───────── 설정 (어떤 알림을 받을지) ─────────
DEFAULT_PREFS = {"trade": True, "plan": True, "move_hold": True, "move_watch": True, "move_market": True, "skip": True,
                 "warn": True, "info": False, "telegram": "short"}


def prefs() -> dict:
    return {**DEFAULT_PREFS, **_rj(DIR / "prefs.json", {})}


def set_prefs(d: dict) -> dict:
    p = prefs()
    for k, v in (d or {}).items():
        if k in DEFAULT_PREFS:
            p[k] = v if k == "telegram" and v in ("short", "full", "off") else bool(v) if k != "telegram" else p[k]
    _wj(DIR / "prefs.json", p)
    return p


def wanted(s: dict) -> bool:
    if s.get("level") == "low":
        return False
    p = prefs()
    k = s.get("kind")
    return {"buy": p["trade"], "sell": p["trade"], "plan": p["plan"], "skip": p["skip"], "warn": p["warn"],
            "move_hold": p["move_hold"], "move_watch": p["move_watch"], "move_market": p["move_market"],
            "move": p["move_watch"]}.get(k, p["info"])


# ───────── VAPID 키 · 구독 ─────────
def _keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    p = DIR / "vapid.pem"
    if p.exists():
        pk = serialization.load_pem_private_key(p.read_bytes(), None)
    else:
        pk = ec.generate_private_key(ec.SECP256R1())
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        os.chmod(p, 0o600)
    pub = pk.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return pk, b64u(pub)


def public_key() -> str | None:
    try:
        return _keys()[1]
    except Exception:
        return None


def subscribe(sub: dict) -> str:
    if not (isinstance(sub, dict) and str(sub.get("endpoint", "")).startswith("https://") and (sub.get("keys") or {}).get("p256dh")):
        return "구독 정보가 올바르지 않아요"
    with LOCK:
        subs = [s for s in _rj(DIR / "subs.json", []) if s.get("endpoint") != sub["endpoint"]]
        subs.append({"endpoint": sub["endpoint"], "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]}, "t": time.time()})
        _wj(DIR / "subs.json", subs[-5:])
    return "이 기기로 알림을 보내요"


def subs() -> list:
    return _rj(DIR / "subs.json", [])


# ───────── 웹 푸시 보내기 (RFC 8291 + 8292) ─────────
def _hkdf(salt: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
    from cryptography.hazmat.primitives import hashes, hmac
    h = hmac.HMAC(salt, hashes.SHA256())
    h.update(ikm)
    prk = h.finalize()
    h = hmac.HMAC(prk, hashes.SHA256())
    h.update(info + b"\x01")
    return h.finalize()[:n]


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None, eph=None) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ua_pub = ub64(p256dh)
    eph = eph or ec.generate_private_key(ec.SECP256R1())
    as_pub = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = _hkdf(ub64(auth), shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + (4096).to_bytes(4, "big") + bytes([len(as_pub)]) + as_pub + ct


def vapid_header(endpoint: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    pk, pub = _keys()
    u = urlparse(endpoint)
    h = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    c = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": CONTACT},
                        separators=(",", ":")).encode())
    r, s = decode_dss_signature(pk.sign(f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256())))
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={h}.{c}.{sig}, k={pub}"


def send_push(title: str, body: str, url: str = "/m#inbox", urgent: bool = False, post=None) -> int:
    """구독한 모든 기기로 보냄 → 보낸 개수. 만료된 구독은 지움."""
    post = post or (lambda *a, **k: requests.post(*a, **k))
    data = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False).encode()
    ok, dead = 0, []
    for s in subs():
        try:
            r = post(s["endpoint"], data=encrypt(data, s["keys"]["p256dh"], s["keys"]["auth"]), timeout=15,
                     headers={"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": "43200",
                              "Urgency": "high" if urgent else "normal", "Authorization": vapid_header(s["endpoint"])})
            if r.status_code in (404, 410):
                dead.append(s["endpoint"])
            elif r.status_code < 300:
                ok += 1
        except Exception:
            pass
    if dead:
        with LOCK:
            _wj(DIR / "subs.json", [s for s in subs() if s.get("endpoint") not in dead])
    return ok


_Q: list = []
_EV = threading.Event()


def _worker():
    while True:
        _EV.wait(5)
        _EV.clear()
        while _Q:
            title, body, url, urgent = _Q.pop(0)
            try:
                send_push(title, body, url, urgent)
            except Exception:
                pass


_started = False


def handle(text: str, mk: str | None = None) -> dict:
    """로봇 알림 한 건: 쉬운 글로 바꿔 알림함에 넣고, 받을 알림이면 앱 푸시. 쉬운 글을 돌려줌(텔레그램용)."""
    global _started
    s = simplify(text)
    try:
        inbox_add(text, s, mk)
    except Exception:
        pass
    if wanted(s) and subs():
        if not _started:
            threading.Thread(target=_worker, daemon=True).start()
            _started = True
        url = f"/m#stock={mk}:{s['code']}" if s.get("code") and mk else "/m#inbox"
        _Q.append((s["title"], s["body"], url, s.get("level") == "high"))
        _EV.set()
    return s


def push_event(kind: str, title: str, body: str, mk: str | None = None, code: str | None = None, full: str = "") -> bool:
    """로봇 글이 아닌 알림(급등 감시 등)을 바로 넣기."""
    s = {"kind": kind, "title": title, "body": _short(body), "level": "normal", "code": code}
    inbox_add(full or f"{title}\n{body}", s, mk)
    if wanted(s) and subs():
        handle_q = (title, s["body"], f"/m#stock={mk}:{code}" if code and mk else "/m#inbox", False)
        global _started
        if not _started:
            threading.Thread(target=_worker, daemon=True).start()
            _started = True
        _Q.append(handle_q)
        _EV.set()
        return True
    return False
