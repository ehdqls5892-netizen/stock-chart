"""실시간 뉴스 감시 (Claude 사용량 없음, 서버가 직접 1분마다 확인).

Google 뉴스 RSS 에서 '특징주·공급계약·목표가 상향·유상증자…'(미장: upgrade·downgrade·FDA·guidance…) 같은
주가를 움직이는 기사 제목을 받아, 제목에 나온 종목을 찾고 호재/악재를 나눈다.
  - 호재 종목 → 후보 맨 앞 + 점수 가산 (차트 신호가 나오면 우선 매수)
  - 악재 종목 → 새로 안 삼, 보유 중이면 알림 + 손절 판단선 아래로 가면 기다리지 않고 바로 정리
제목만 보고 판단하므로 사는 결정은 여전히 차트·거래량 신호가 한다(뉴스만으로는 안 삼).
"""
from __future__ import annotations

import datetime as dt
import email.utils
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

Q = {
    "KR": ["특징주", "공급계약 체결", "목표주가 상향", "목표가 상향", "수주", "어닝 서프라이즈", "최대 실적",
           "유상증자", "목표가 하향", "어닝 쇼크", "임상", "인수합병", "자사주 소각", "급등", "급락", "상한가"],
    "US": ["stock upgrade analyst", "stock downgrade analyst", "shares jump", "shares surge", "shares plunge",
           "FDA approval", "raises guidance", "cuts guidance", "earnings beat", "earnings miss", "price target raised",
           "acquisition deal shares", "stock offering"],
}
GOOD = {
    "KR": ["급등", "상승", "강세", "신고가", "상한가", "수주", "공급계약", "단일판매", "상향", "흑자전환", "최대 실적", "최대실적",
           "어닝 서프라이즈", "서프라이즈", "승인", "허가", "특허", "자사주", "소각", "인수", "합병", "호실적", "기대감", "급증", "돌파"],
    "US": ["jump", "jumps", "surge", "surges", "soar", "soars", "rally", "rallies", "upgrade", "upgraded", "raises", "raised",
           "beat", "beats", "record", "approval", "approved", "wins", "contract", "buyback", "acquire", "higher", "climb"],
}
BAD = {
    "KR": ["급락", "하락", "약세", "하한가", "유상증자", "하향", "적자", "어닝 쇼크", "쇼크", "횡령", "배임", "소송", "거래정지",
           "감자", "블록딜", "매각", "부진", "리콜", "실패", "중단", "철수", "경고", "불확실", "우려", "제재"],
    "US": ["plunge", "plunges", "plummet", "sink", "sinks", "fall", "falls", "drop", "drops", "tumble", "downgrade",
           "downgraded", "cuts", "cut", "miss", "misses", "lawsuit", "probe", "recall", "offering", "dilution", "halt", "lower", "warns"],
}
NOT = {"KR": ["철회", "해소", "벗어", "반등"], "US": []}     # '유상증자 철회'·'우려 해소' 같은 반전 표현


def _norm_us(n: str) -> str:
    n = re.sub(r"[,.]", " ", n)
    n = re.sub(r"\b(Inc|Corp|Corporation|Company|Co|Holdings|Group|Ltd|plc|PLC|Class [A-C]|The)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


class NewsWatch:
    def __init__(self, market: str, names: dict, log, notify=None, per_min: int = 6, max_age_min: int = 90):
        self.m, self.log, self.notify = market, log, notify
        self.per_min, self.max_age = per_min, max_age_min
        self.seen: set[str] = set()
        self.good: dict[str, dict] = {}
        self.bad: dict[str, dict] = {}
        self.qi = 0
        self.last = 0.0
        self.fail = 0
        self.set_names(names)

    def set_names(self, names: dict):
        """names: code → 이름. 긴 이름부터 찾아서 '삼성전자우' 가 '삼성전자' 로 잘못 잡히지 않게."""
        pats = []
        for code, nm in names.items():
            if not nm:
                continue
            if self.m == "US":
                short = _norm_us(nm)
                keys = {short} if len(short) >= 3 else set()
                first = short.split(" ")[0] if short else ""
                if len(first) >= 4 and first.lower() not in ("american", "first", "united", "general", "global", "national"):
                    keys.add(first)
                for k in keys:
                    pats.append((k, code))
            elif len(nm) >= 2:
                pats.append((nm, code))     # 2글자 이름은 match 에서 더 엄격하게 봄
        pats.sort(key=lambda x: -len(x[0]))
        self.pats = pats

    def match(self, title: str) -> list[str]:
        out, t = [], title
        if self.m == "US":
            for tk in re.findall(r"\((?:NASDAQ|NYSE|AMEX|NYSEARCA|Nasdaq|NYSE American)?:?\s*([A-Z]{1,5})\)", title):
                out.append(tk)
            for tk in re.findall(r"\b([A-Z]{2,5})\b(?= (?:stock|shares))", title):
                out.append(tk)
        for k, code in self.pats:
            if self.m == "US":
                fl = re.IGNORECASE if k.isupper() else 0      # 'NVIDIA' 는 'Nvidia' 로도 씀
                mt = re.search(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])", t, fl)
                if mt:
                    out.append(code)
                    t = t[:mt.start()] + " " + t[mt.end():]
            else:
                i = t.find(k)
                if i >= 0:
                    after = t[i + len(k): i + len(k) + 1]
                    before = t[i - 1: i] if i else ""
                    if len(k) <= 2:                  # '대상'·'한화' 같은 짧은 이름: '[특징주] 대상, …' 처럼 분명할 때만
                        ok = after in (",", "(", "↑", "↓", "·", "") and (not before or not re.match(r"[가-힣A-Za-z0-9]", before))
                    else:
                        ok = (not after or not re.match(r"[가-힣A-Za-z0-9]", after) or after in "은는이가을를의도와과에") \
                            and (not before or not re.match(r"[가-힣A-Za-z0-9]", before))
                    if ok:
                        out.append(code)
                        t = t.replace(k, " ")
        return list(dict.fromkeys(out))

    def tone(self, title: str) -> int:
        tl = title if self.m == "KR" else title.lower()
        g = sum(1 for w in GOOD[self.m] if w in tl)
        b = sum(1 for w in BAD[self.m] if w in tl)
        if any(w in tl for w in NOT[self.m]):
            g, b = g + b, 0
        return 1 if g > b else -1 if b > g else 0

    def url(self, q: str) -> str:
        if self.m == "KR":
            return f"https://news.google.com/rss/search?q={quote(q + ' when:1h')}&hl=ko&gl=KR&ceid=KR:ko"
        return f"https://news.google.com/rss/search?q={quote(q + ' when:1h')}&hl=en-US&gl=US&ceid=US:en"

    def parse(self, xml: str) -> list[dict]:
        items = []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return items
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            src = (it.findtext("source") or "").strip()
            if src and title.endswith(" - " + src):
                title = title[: -len(src) - 3]
            try:
                pub = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
            except (TypeError, ValueError):
                pub = None
            items.append({"title": title, "link": it.findtext("link") or title, "pub": pub, "src": src})
        return items

    def handle(self, items: list[dict]) -> list[tuple]:
        """새 기사 → [(code, +1/-1, 제목)]"""
        now = dt.datetime.now(dt.timezone.utc)
        out = []
        for x in items:
            if x["link"] in self.seen:
                continue
            self.seen.add(x["link"])
            if x["pub"] and (now - x["pub"]).total_seconds() > self.max_age * 60:
                continue
            tn = self.tone(x["title"])
            if not tn:
                continue
            for code in self.match(x["title"]):
                rec = {"t": time.time(), "title": x["title"], "src": x["src"]}
                if tn > 0:
                    self.good[code] = rec
                    self.bad.pop(code, None)
                else:
                    self.bad[code] = rec
                    self.good.pop(code, None)
                out.append((code, tn, x["title"]))
        if len(self.seen) > 5000:
            self.seen = set(list(self.seen)[-2000:])
        # 오래된(3시간) 뉴스 효과는 끝
        for d in (self.good, self.bad):
            for c in [c for c, r in d.items() if time.time() - r["t"] > 3 * 3600]:
                d.pop(c, None)
        return out

    def poll(self) -> list[tuple]:
        """1분에 per_min 개 검색어를 돌아가며 확인. 새로 나온 (종목, 호재/악재, 제목) 목록."""
        if time.time() - self.last < 60:
            return []
        self.last = time.time()
        import requests
        out = []
        qs = Q[self.m]
        for _ in range(min(self.per_min, len(qs))):
            q = qs[self.qi % len(qs)]
            self.qi += 1
            try:
                r = requests.get(self.url(q), timeout=8, headers={"User-Agent": "Mozilla/5.0 (tossbot news)"})
                if r.status_code == 200:
                    out += self.handle(self.parse(r.text))
                    self.fail = 0
                else:
                    self.fail += 1
            except Exception:
                self.fail += 1
        if self.fail == 12:
            self.log("📰 뉴스 감시: 뉴스 사이트에 계속 연결이 안 돼요 (매매는 그대로, 뉴스 반영만 멈춤)")
        return out
