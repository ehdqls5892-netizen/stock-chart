"""텔레그램 알림 (선택). .env 에 TG_TOKEN, TG_CHAT 이 있으면 보낸다."""
from __future__ import annotations

import os
import queue
import threading

import requests


class Notifier:
    def __init__(self, prefix: str = ""):
        self.token, self.chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
        self.prefix = prefix
        self.q: queue.Queue = queue.Queue()
        if self.enabled:
            threading.Thread(target=self._run, daemon=True).start()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def __call__(self, text: str):
        if self.enabled:
            self.q.put(self.prefix + text)

    def _run(self):
        while True:
            t = self.q.get()
            try:
                requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              json={"chat_id": self.chat, "text": t[:3900]}, timeout=10)
            except Exception:
                pass
