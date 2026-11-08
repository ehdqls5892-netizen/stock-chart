"""후보 종목에 붙이는 추가 자료 (GitHub Secrets 에 키가 있을 때만 켜짐).
  DART_KEY       : 금융감독원 전자공시 — 국장 최근 분기 영업이익 증감·최근 잠정실적 공시일·실적 발표 예고·치명적 공시(유상증자 등)
  NAVER_ID/SECRET: 네이버 검색 API — 국장 최근 뉴스 제목
  ANTHROPIC_API_KEY (+ CLAUDE_MODEL): 후보마다 뉴스·숫자를 읽고 '왜 움직였나·호재/악재·위험'을 한 줄로, 점수 −2~+2 보정
키가 없거나 연결이 안 되면 조용히 건너뜀."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import sys
import zipfile

import requests

UA = {"User-Agent": "Mozilla/5.0 (stock-chart bot)"}
_CORP: dict = {}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ── DART ─────────────────────────────────────────────────────────────────────
def _dart_corp(key: str) -> dict:
    if _CORP:
        return _CORP
    r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}, timeout=60)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    xml = z.read(z.namelist()[0]).decode("utf-8", "replace")
    for blk in re.findall(r"<list>(.*?)</list>", xml, re.S):          # 회사 하나씩 끊어서 (옆 회사 코드와 섞이지 않게)
        cc = re.search(r"<corp_code>\s*(\d+)\s*</corp_code>", blk)
        sc = re.search(r"<stock_code>\s*(\d{6})\s*</stock_code>", blk)
        if cc and sc:
            _CORP[sc.group(1)] = cc.group(1)
    log("DART 회사 코드", len(_CORP))
    return _CORP


RED_DART = ("유상증자결정", "전환사채권발행결정", "신주인수권부사채권발행결정", "감자결정", "횡령", "배임", "회생절차",
            "상장폐지", "관리종목", "감사의견", "거래정지", "불성실공시")


def dart(code: str, today: dt.date | None = None) -> dict:
    key = os.getenv("DART_KEY", "").strip()
    if not key:
        return {}
    out, cc = {}, None
    try:
        cc = _dart_corp(key).get(code)
        if not cc:
            return {}
        today = today or dt.date.today()
        r = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=20,
                         params={"crtfc_key": key, "corp_code": cc, "bgn_de": (today - dt.timedelta(days=100)).strftime("%Y%m%d"),
                                 "end_de": today.strftime("%Y%m%d"), "page_count": 100})
        items = (r.json() or {}).get("list") or []
        prov = [x for x in items if "잠정" in x.get("report_nm", "") and "실적" in x.get("report_nm", "")]
        if prov:
            out["prov_date"] = prov[0]["rcept_dt"]
        reg = [x["rcept_dt"] for x in items if re.search(r"(분기|반기|사업)보고서", x.get("report_nm", "")) and "기재정정" not in x.get("report_nm", "")]
        if prov and reg and prov[0]["rcept_dt"] > max(reg):
            out["op_stale"] = True          # 이미 새 분기 잠정실적이 나왔는데 정기보고서(숫자)는 아직 → 지난 분기 숫자라 점수에 안 씀
        plan = [x for x in items if "결산실적공시예고" in x.get("report_nm", "").replace(" ", "") or "기업설명회" in x.get("report_nm", "")]
        if plan:
            out["ir_notice"] = plan[0]["rcept_dt"] + " " + plan[0]["report_nm"][:40]
        recent = (today - dt.timedelta(days=3)).strftime("%Y%m%d")
        flags = [x["report_nm"] for x in items if x.get("rcept_dt", "") >= recent and any(k in x.get("report_nm", "").replace(" ", "") for k in RED_DART)]
        if flags:
            out["dart_flags"] = flags[:3]
        # 최근 정기보고서 영업이익 (누적) 전년 같은 기간 대비
        y = today.year
        for year, rc in ((y, "11014"), (y, "11012"), (y, "11013"), (y - 1, "11011")):
            r2 = requests.get("https://opendart.fss.or.kr/api/fnlttSinglAcnt.json", timeout=20,
                              params={"crtfc_key": key, "corp_code": cc, "bsns_year": str(year), "reprt_code": rc})
            js2 = r2.json() or {}
            rows = js2.get("list") or []
            if js2.get("status") not in (None, "000", "013"):
                log("DART 재무 응답", code, js2.get("status"), js2.get("message"))
            op = [x for x in rows if str(x.get("account_nm", "")).replace(" ", "").startswith("영업이익") and x.get("fs_div") in ("CFS", "OFS")]
            if not op:
                continue
            x = sorted(op, key=lambda z: z.get("fs_div") != "CFS")[0]
            cur = x.get("thstrm_add_amount") or x.get("thstrm_amount")
            prv = x.get("frmtrm_add_amount") or x.get("frmtrm_amount")
            cur = float(str(cur).replace(",", "")) if cur not in (None, "", "-") else None
            prv = float(str(prv).replace(",", "")) if prv not in (None, "", "-") else None
            if cur is not None and prv:
                out["op_yoy"] = round(cur / abs(prv) - 1, 4)
                out["op_period"] = f"{year} {dict(zip(('11013', '11012', '11014', '11011'), ('1분기', '반기', '3분기', '연간')))[rc]}"
            break
    except Exception as e:
        log("DART 실패", code, type(e).__name__, str(e)[:80])
    log("DART", code, cc, {k: out.get(k) for k in ("op_yoy", "op_period", "op_stale", "prov_date", "dart_flags") if out.get(k)} or "공시 숫자 없음")
    return out


# ── 네이버 뉴스 ────────────────────────────────────────────────────────────────
def naver_news(name: str, n: int = 8) -> list:
    cid, sec = os.getenv("NAVER_ID", "").strip(), os.getenv("NAVER_SECRET", "").strip()
    if not cid or not sec:
        return []
    try:
        # 2026-07 부터 새 발급은 NAVER API HUB(네이버 클라우드) — 먼저 HUB, 안 되면 예전 개발자센터 주소
        r = requests.get("https://naverapihub.apigw.ntruss.com/search/v1/news", timeout=15,
                         params={"query": name, "display": n, "sort": "date", "format": "json"},
                         headers={"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": sec})
        if r.status_code != 200:
            log("네이버 API HUB 응답", r.status_code, r.text[:120])
            r = requests.get("https://openapi.naver.com/v1/search/news.json", timeout=15,
                             params={"query": name, "display": n, "sort": "date"},
                             headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec})
        if r.status_code != 200:
            log("네이버 뉴스 응답", r.status_code, r.text[:120])
            return []
        out = []
        for it in (r.json() or {}).get("items") or []:
            t = re.sub(r"<[^>]+>|&quot;|&apos;|&amp;|&lt;|&gt;", "", it.get("title", ""))
            if name[:2] in t and t not in out:
                out.append(t)
        return out[:5]
    except Exception as e:
        log("네이버 뉴스 실패", name, type(e).__name__)
        return []


def naver_stock_news(code: str, n: int = 8) -> list:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from bot.news import naver_stock_news as f
    return f(code, n, log=log)


# ── Claude 판독 ───────────────────────────────────────────────────────────────
def _claude(model: str, prompt: str, max_tokens: int, code=None) -> dict:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=120,
                          headers={k: v for k, v in {"x-api-key": key, "authorization": f"Bearer {key}", "anthropic-version": "2023-06-01",
                                                     "content-type": "application/json",
                                                     "anthropic-workspace-id": os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()}.items() if v},
                          json={"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})
        js = r.json()
        if r.status_code != 200:
            log("Claude 응답 오류", model, r.status_code, str(js)[:200])
            return {}
        u = js.get("usage") or {}
        USAGE[model] = [USAGE.get(model, [0, 0])[0] + u.get("input_tokens", 0), USAGE.get(model, [0, 0])[1] + u.get("output_tokens", 0)]
        txt = "".join(c.get("text", "") for c in js.get("content", []) if c.get("type") == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            log("Claude 응답 해석 실패", code, model, js.get("stop_reason"), [c.get("type") for c in js.get("content", [])], txt[:200])
            return {}
        return json.loads(m.group(0))
    except Exception as e:
        log("Claude 판독 실패", code, type(e).__name__, str(e)[:80])
        return {}


USAGE: dict = {}

RULES = ("규칙: 1) 아래 자료에 없는 사실은 절대 지어내지 마라. 2) 확실하지 않으면 adj 는 0. "
         "3) 기사 제목이 시장 전체 얘기면 이 종목 근거로 쓰지 마라. 4) 실적 숫자가 '지난 분기'(op_stale) 표시면 최신 아님. "
         "5) 손실을 피하는 게 수익보다 중요하다: 희석(유상증자·CB), 회계·소송·규제, 실적 하향·가이던스 축소, 대주주 매도, "
         "급등 직후 재료 소멸, 실적 발표 임박 같은 위험은 반드시 짚어라.")


def ai_read(market: str, x: dict, news: list) -> dict:
    """1단계(전 후보, 싼 모델): 빠른 거르기. 올리는 건 +1 까지만, 내리는 건 −2 까지."""
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        return {}
    model = os.getenv("CLAUDE_MODEL", "").strip() or "claude-haiku-5-5"
    facts = {"종목": x.get("name"), "시장": market, "종가": x.get("close"), "점수 근거": x.get("reason"),
             "애널리스트·실적·공시": {k: v for k, v in (x.get("ev") or {}).items() if k not in ("est", "news", "ai")},
             "국장 컨센서스·수급": (x.get("ev") or {}).get("est"), "최근 뉴스 제목": news[:8]}
    prompt = ("너는 15년 차 주식 애널리스트다. 아래 자료만 보고 이 종목을 앞으로 1~4주 들고 갈 만한지 판단해라. " + RULES +
              " JSON 한 줄만 답해라: "
              '{"view":"긍정|중립|부정","catalyst":"최근 움직임의 이유 한 문장(모르면 \'확인 안 됨\')",'
              '"risk":"가장 큰 위험 한 문장","adj":-2~1 사이 정수}\n\n'
              + json.dumps(facts, ensure_ascii=False, default=str)[:6000])
    d = _claude(model, prompt, 3000, x.get("code"))
    if not d:
        return {}
    try:
        adj = int(max(-2, min(1, int(d.get("adj", 0)))))
    except (TypeError, ValueError):
        adj = 0
    return {"view": str(d.get("view", ""))[:4], "catalyst": str(d.get("catalyst", ""))[:120],
            "risk": str(d.get("risk", ""))[:120], "adj": adj, "model": model}


def ai_final(market: str, x: dict, news: list, first: dict) -> dict:
    """2단계(실제로 살 상위 후보만, 가장 좋은 모델): 깊게 다시 읽고 확정. 치명적 위험이면 veto(매수 금지).
    점수 보정은 −3 ~ +2 (위험 쪽을 더 크게)."""
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        return {}
    model = os.getenv("CLAUDE_MODEL_FINAL", "").strip() or "claude-opus-5-5"
    facts = {"종목": x.get("name"), "코드": x.get("code"), "시장": market, "종가": x.get("close"), "손절가": x.get("stop"),
             "점수 근거": x.get("reason"),
             "애널리스트·실적·공시": {k: v for k, v in (x.get("ev") or {}).items() if k not in ("est", "news", "ai")},
             "국장 컨센서스·수급": (x.get("ev") or {}).get("est"), "최근 뉴스 제목": news[:15], "1차 판독": first}
    prompt = ("너는 15년 차 주식 애널리스트이자 리스크 매니저다. 이 종목은 내일 실제로 돈을 넣을 최종 후보다. "
              "손절은 손절가에서 자동으로 걸린다. 아래 자료만 근거로, 1~4주 보유가 위험 대비 이득인지 냉정하게 판단해라. "
              "1차 판독이 틀렸으면 바로잡아라. " + RULES +
              " 치명적 위험(희석 발표, 회계·감사 문제, 거래정지 위험, 실적 쇼크·가이던스 대폭 하향, 재료가 이미 끝난 급등)이 "
              "자료에 분명히 있으면 veto 를 true 로. 애매하면 veto 는 false 로 두고 adj 를 낮춰라. JSON 한 줄만 답해라: "
              '{"view":"긍정|중립|부정","catalyst":"움직임의 이유 한 문장(모르면 \'확인 안 됨\')",'
              '"risk":"가장 큰 위험 한 문장","adj":-3~2 사이 정수,"veto":true|false,"veto_reason":"veto 일 때만 한 문장"}\n\n'
              + json.dumps(facts, ensure_ascii=False, default=str)[:9000])
    d = _claude(model, prompt, 6000, x.get("code"))
    if not d:
        return {}
    try:
        adj = int(max(-3, min(2, int(d.get("adj", 0)))))
    except (TypeError, ValueError):
        adj = 0
    out = {"view": str(d.get("view", ""))[:4], "catalyst": str(d.get("catalyst", ""))[:150],
           "risk": str(d.get("risk", ""))[:150], "adj": adj, "model": model, "final": True}
    if d.get("veto") is True:
        out["veto"] = str(d.get("veto_reason") or out["risk"])[:150]
    return out
