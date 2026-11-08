"""1시간마다 종목별 최신 기사 → AI(Haiku, API 크레딧)가 호재/악재 다시 판정.

- 한 번에 최대 BATCH 종목을 한 번의 AI 호출로 판정 (호출 수·비용을 줄여도 판정 수는 그대로).
- 기사 제목이 지난번과 똑같으면 지난 판정을 그대로 씀 (새 기사가 있을 때만 다시 읽음 → 같은 돈으로 더 자주).
- 결과: {code: {"impact":"호재|악재|중립", "why":"쉬운 말 한 줄", "adj":-2..1, "severe":bool, "h":기사 지문, "t":판정 시각}}
GitHub Actions(tools/news_judge.py, 후보·오늘 살 종목·데스크 종목)와 서버(보유 종목, 키가 있을 때) 둘 다 이 파일을 씀.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re

BATCH = 10
MODEL_ENV = "CLAUDE_NEWS_MODEL"

PROMPT = ("너는 15년 차 주식 애널리스트다. 아래 종목마다 최근 기사 제목만 보고 '앞으로 1~4주 주가에' 호재인지 악재인지 판정해라. "
          "규칙: 1) 제목에 없는 사실은 지어내지 마라. 2) 시장 전체 얘기·광고성·단순 시황 기사는 그 종목 근거로 쓰지 마라. "
          "3) 확실하지 않으면 중립, adj 0. 4) severe=true 는 유상증자·CB(주식 희석), 회계·감사 문제, 거래정지, 횡령·배임, "
          "실적 쇼크·가이던스 대폭 하향, 대규모 소송·규제 제재, 대주주 대량 매도처럼 '사면 안 될 수준'일 때만. "
          "5) why 는 중학생도 아는 쉬운 말 한 줄(30자 안팎). "
          'JSON 하나만 답해라: {"코드":{"impact":"호재|악재|중립","why":"한 줄","adj":-2~1 정수,"severe":true|false}, ...}\n\n')


def fp(heads: list) -> str:
    return hashlib.sha1("\n".join(sorted(heads)).encode()).hexdigest()[:12]


def _call(prompt: str, model: str, claude=None) -> dict:
    if claude:
        return claude(model, prompt) or {}
    import requests
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return {}
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=120,
                          headers={k: v for k, v in {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
                                                     "anthropic-workspace-id": os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()}.items() if v},
                          json={"model": model, "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]})
        js = r.json()
        if r.status_code != 200:
            print("뉴스 AI 응답 오류", r.status_code, str(js)[:200])
            return {}
        txt = "".join(c.get("text", "") for c in js.get("content", []) if c.get("type") == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else {}
    except Exception as e:
        print("뉴스 AI 실패", type(e).__name__, str(e)[:80])
        return {}


def clean(d: dict) -> dict | None:
    if not isinstance(d, dict):
        return None
    imp = str(d.get("impact", ""))
    imp = "호재" if "호재" in imp else "악재" if "악재" in imp else "중립"
    try:
        adj = max(-2, min(1, int(d.get("adj", 0))))
    except (TypeError, ValueError):
        adj = 0
    if imp == "중립":
        adj = 0
    return {"impact": imp, "why": str(d.get("why", ""))[:60], "adj": adj, "severe": bool(d.get("severe") is True and imp == "악재")}


def judge(market: str, items: list, news_fn, prev: dict | None = None, claude=None, now=None) -> dict:
    """items = [{"code","name"}]. news_fn(code, name) → 기사 제목 목록. prev = 지난 결과(같은 기사면 재사용)."""
    prev = prev or {}
    now = now or dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    model = os.getenv(MODEL_ENV, "").strip() or os.getenv("CLAUDE_MODEL", "").strip() or "claude-haiku-5-5"
    out, todo = {}, []
    for it in items:
        c, nm = str(it["code"]), it.get("name") or str(it["code"])
        try:
            heads = [h for h in (news_fn(c, nm) or []) if h][:8]
        except Exception:
            heads = []
        if not heads:
            continue
        h = fp(heads)
        old = prev.get(c)
        if old and old.get("h") == h:
            out[c] = old                          # 새 기사 없음 → 지난 판정 그대로
            continue
        todo.append((c, nm, heads, h))
    for i in range(0, len(todo), BATCH):
        part = todo[i:i + BATCH]
        body = {c: {"종목": nm, "최근 기사 제목": heads} for c, nm, heads, _ in part}
        d = _call(PROMPT + f"시장: {market}\n" + json.dumps(body, ensure_ascii=False), model, claude)
        for c, nm, heads, h in part:
            v = clean(d.get(c))
            if v:
                out[c] = {**v, "name": nm, "h": h, "t": now, "head": heads[0][:60]}
    return out


def fresh(d: dict | None, hours: float = 6, now=None) -> dict:
    """판정 시각이 hours 시간 안인 것만 (오래된 뉴스 판정은 매수 판단에 안 씀)."""
    if not d:
        return {}
    now = now or dt.datetime.now(dt.timezone.utc)
    out = {}
    for c, v in ((d.get("stocks") if "stocks" in d else d) or {}).items():
        try:
            age = (now - dt.datetime.fromisoformat(v["t"])).total_seconds() / 3600
        except Exception:
            continue
        if age <= hours:
            out[c] = v
    return out
