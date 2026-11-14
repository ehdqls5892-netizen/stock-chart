"""과거 시점 정보(애널리스트·실적 전망·내부자)를 키로 얼마나 거슬러 받을 수 있는지 확인. 키 값은 출력하지 않음."""
import os
import requests

S = ["AAPL", "JPM", "CDNS", "EQIX", "KEY"]
F, P = os.getenv("FINNHUB_KEY"), os.getenv("FMP_KEY")


def span(name, rows, key):
    ds = sorted(str(r.get(key))[:10] for r in rows if r.get(key))
    print(f"{name:42s} {len(rows):5d}건  {ds[0] if ds else '-'} ~ {ds[-1] if ds else '-'}")


for s in S:
    print("==", s)
    if F:
        for nm, url, k in [("Finnhub 애널리스트 추천 추이(월별)", "https://finnhub.io/api/v1/stock/recommendation", "period"),
                           ("Finnhub 등급 상향·하향", "https://finnhub.io/api/v1/stock/upgrade-downgrade", "gradeTime"),
                           ("Finnhub 실적 서프라이즈", "https://finnhub.io/api/v1/stock/earnings", "period")]:
            try:
                r = requests.get(url, params={"symbol": s, "token": F, "limit": 1000}, timeout=20)
                j = r.json()
                span(nm, j if isinstance(j, list) else [], k) if r.ok and isinstance(j, list) else print(f"{nm:42s} 안 됨 ({r.status_code} {str(j)[:80]})")
            except Exception as e:
                print(nm, "오류", type(e).__name__)
        try:
            r = requests.get("https://finnhub.io/api/v1/stock/insider-transactions", params={"symbol": s, "token": F, "from": "2015-01-01"}, timeout=20)
            span("Finnhub 내부자 거래", (r.json() or {}).get("data") or [], "transactionDate")
        except Exception as e:
            print("내부자 오류", type(e).__name__)
    if P:
        for nm, path, k in [("FMP 등급 기록(grades)", "grades", "date"), ("FMP 등급 과거 집계(grades-historical)", "grades-historical", "date"),
                            ("FMP 목표가 뉴스(price-target-news)", "price-target-news", "publishedDate"),
                            ("FMP 실적 전망 기록(analyst-estimates)", "analyst-estimates", "date")]:
            try:
                prm = {"symbol": s, "apikey": P, "limit": 1000}
                if path == "analyst-estimates":
                    prm["period"] = "quarter"
                r = requests.get(f"https://financialmodelingprep.com/stable/{path}", params=prm, timeout=20)
                j = r.json()
                span(nm, j, k) if r.ok and isinstance(j, list) else print(f"{nm:42s} 안 됨 ({r.status_code} {str(j)[:80]})")
            except Exception as e:
                print(nm, "오류", type(e).__name__)
