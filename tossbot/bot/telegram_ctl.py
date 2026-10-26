"""텔레그램으로 로봇 조작 (어디서나, 인터넷만 되면).

.env 에 TG_TOKEN, TG_CHAT 이 있으면 켜져요. TG_CHAT 이 아닌 사람이 보낸 메시지는 무시해요.
봇 채팅방에 아래 버튼이 생겨요:
  📊 상태 | 💵 투자금 | ▶️ 연습 시작 | 🔴 실전 시작 | ⏹ 종료(보유 유지) | 💰 종료(전부 팔기)
'업데이트' 라고 보내면 GitHub 새 버전을 바로 받아 시험 후 적용해요(서버).
실전 시작·전부 팔기는 한 번 더 확인 버튼을 눌러야 실행돼요.
"""
from __future__ import annotations

import os
import threading
import time

import requests

KB = {"keyboard": [[{"text": "📊 상태"}, {"text": "💵 투자금"}],
                   [{"text": "▶️ 연습 시작"}, {"text": "🔴 실전 시작"}],
                   [{"text": "⏹ 종료(보유 유지)"}, {"text": "💰 종료(전부 팔기)"}]],
      "resize_keyboard": True, "is_persistent": True}


def won(v, mk):
    if v is None:
        return "–"
    return f"${v:,.2f}" if mk == "US" else f"{v:,.0f}원"


class TelegramControl:
    def __init__(self, start_fn, stop_fn, status_fn, capital_fn=None):
        self.token, self.chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
        self.start_fn, self.stop_fn, self.status_fn, self.capital_fn = start_fn, stop_fn, status_fn, capital_fn
        self.pending: dict[str, float] = {}   # 확인 대기 중인 위험한 명령
        self.offset = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def send(self, text, kb=True):
        try:
            body = {"chat_id": self.chat, "text": text[:3900]}
            if kb:
                body["reply_markup"] = KB
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=body, timeout=10)
        except Exception:
            pass

    def status_text(self) -> str:
        s = self.status_fn()
        FL, NM = {"KR": "🇰🇷", "US": "🇺🇸"}, {"KR": "국장", "US": "미장"}
        lines = [f"📊 {'실행 중' if s.get('running') else '꺼져 있음'}"
                 + (f" · {'실전' if s.get('mode') == 'live' else '연습'}" if s.get("running") else "")]
        for mk, x in (s.get("markets") or {}).items():
            lines.append(f"{FL[mk]} {NM[mk]}: {x.get('phase')} · 손익 {won(x.get('realized', 0), mk)} · 매수 {x.get('trades', 0)}회"
                         + (f" · 투자금 {won(x.get('capital'), mk)} 중 {won(x.get('invested', 0), mk)} 사용" if x.get("capital") else "")
                         + (f" · ⛔ {x['halted']}" if x.get("halted") else "") + (f" · ❌ {x['error']}" if x.get("error") else ""))
        pos = s.get("positions") or []
        if pos:
            lines.append("보유:")
            for p in pos:
                mk = p.get("market", "KR")
                pct = f"{p['pct']:+.2f}%" if p.get("pct") is not None else "–"
                lines.append(f"- {FL.get(mk, '')} {p['name']} {p['qty']}주 {pct} · 손절선 {won(p.get('sl'), mk)}"
                             + (" · 들고 가는 중" if p.get("carry") else ""))
        else:
            lines.append("보유 없음")
        cl = s.get("closed") or []
        if cl:
            lines.append("오늘 매도:")
            for c in cl[-8:]:
                mk = c.get("market", "KR")
                lines.append(f"- {FL.get(mk, '')} {c['name']} {c['pct']:+.2f}% {won(c['pnl'], mk)}" if c.get("pct") is not None else f"- {c['name']}")
        return "\n".join(lines)

    def capital_cmd(self, t: str) -> str:
        """'투자금 국장 50만' / '투자금 미장 400' / '투자금' (지금 값 보기)"""
        import re
        parts = t.replace("💵", "").replace("투자금", "").split()
        mk = None
        if parts and parts[0] in ("국장", "KR", "kr", "한국"):
            mk = "KR"
        elif parts and parts[0] in ("미장", "US", "us", "미국"):
            mk = "US"
        if not mk or len(parts) < 2 or not self.capital_fn:
            ms = (self.status_fn().get("markets") or {})
            cur = " · ".join(f"{'국장' if k == 'KR' else '미장'} {won(v.get('capital'), k)}" for k, v in ms.items())
            return (f"💵 오늘 투자금: {cur}\n바꾸려면 이렇게 보내세요:\n투자금 국장 50만\n투자금 미장 400")
        raw = "".join(parts[1:]).replace(",", "").replace("원", "").replace("$", "")
        m = re.fullmatch(r"([\d.]+)(만)?", raw)
        if not m:
            return "숫자로 적어 주세요. 예: 투자금 국장 50만"
        v = float(m.group(1)) * (10000 if m.group(2) else 1)
        return "💵 " + self.capital_fn(mk, v)

    def update_now(self) -> str:
        """GitHub 최신 버전을 바로 받아 시험 후 적용 (서버에서만). 로봇 프로그램 밖에서 돌려야 재시작돼도 끊기지 않음."""
        import shutil
        import subprocess
        from pathlib import Path
        tb = Path(__file__).resolve().parent.parent / "server" / "tb"
        if not shutil.which("systemd-run") or not tb.exists():
            return "이 컴퓨터에서는 텔레그램 업데이트를 쓸 수 없어요 (서버 전용)."
        try:
            subprocess.Popen(["systemd-run", "--no-block", "--collect", "--unit", f"tossbot-update-now-{int(time.time())}",
                              str(tb), "자동확인"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return f"업데이트를 시작하지 못했어요: {e}"
        return "🆕 GitHub 새 버전을 확인하고 있어요. 새 버전이 있으면 시험 후 적용하고 1~2분 안에 알려 드릴게요 (없으면 조용히 그대로)."

    def handle(self, text: str):
        t = (text or "").strip()
        now = time.time()
        if t.startswith("투자금") or t.startswith("💵"):
            return self.send(self.capital_cmd(t))
        if t in ("업데이트", "/update", "🆕 업데이트"):
            return self.send(self.update_now())
        if "상태" in t or t in ("/status", "/start"):
            return self.send(self.status_text())
        if "연습 시작" in t:
            return self.send("▶️ " + self.start_fn("paper", True))
        if "실전 시작" in t and "확인" not in t:
            self.pending["live"] = now
            return self.send("🔴 실제 돈으로 주문해요. 60초 안에 '실전 시작 확인' 을 보내면 시작해요.", kb=False)
        if t == "실전 시작 확인":
            if now - self.pending.pop("live", 0) < 60:
                return self.send("🔴 " + self.start_fn("live", True))
            return self.send("확인 시간이 지났어요. 다시 '🔴 실전 시작' 을 눌러 주세요.")
        if "보유 유지" in t:
            return self.send("⏹ " + self.stop_fn("keep"))
        if "전부 팔기" in t and "확인" not in t:
            self.pending["sell"] = now
            return self.send("💰 보유 종목을 전부 시장가로 팔아요. 60초 안에 '전부 팔기 확인' 을 보내면 실행해요.", kb=False)
        if t == "전부 팔기 확인":
            if now - self.pending.pop("sell", 0) < 60:
                return self.send("💰 " + self.stop_fn("sell"))
            return self.send("확인 시간이 지났어요. 다시 눌러 주세요.")
        self.send("아래 버튼을 눌러 주세요.")

    def _loop(self):
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        # 켜기 전에 쌓인 옛 메시지는 무시
        try:
            r = requests.get(url, params={"timeout": 0}, timeout=15).json()
            if r.get("result"):
                self.offset = r["result"][-1]["update_id"] + 1
        except Exception:
            pass
        self.send("🤖 로봇 프로그램이 켜졌어요. 아래 버튼으로 조작할 수 있어요.")
        while True:
            try:
                p = {"timeout": 30}
                if self.offset:
                    p["offset"] = self.offset
                r = requests.get(url, params=p, timeout=40).json()
                for u in r.get("result", []):
                    self.offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str((msg.get("chat") or {}).get("id")) != str(self.chat):
                        continue  # 내 채팅방이 아닌 곳의 메시지는 무시
                    self.handle(msg.get("text", ""))
            except Exception:
                time.sleep(5)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
