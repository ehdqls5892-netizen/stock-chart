"""점검 루프용 '그때 알 수 있었던' 정보 모으기 (미장 전 종목, 10년).

- 애널리스트 등급 바뀜 기록 (FMP grades: 날짜별 상향·하향·유지) — 무료 하루 호출 한도가 있어 하루 --fmp 개씩 나눠 받음
- 내부자 거래 (Finnhub insider-transactions: 날짜별 매수 P / 매도 S)

결과는 OUT 폴더의 hist-us.json.gz 에 쌓이고, 이미 받은 종목은 30일 지나야 다시 받음. 키 값은 출력하지 않음.
사용: python hist_collect.py --out DIR [--fmp 150]
"""
import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from swing_data import us_universe  # noqa: E402

REFRESH = 30


def load(p: Path) -> dict:
    try:
        return json.loads(gzip.open(p).read())
    except Exception:
        return {"grades": {}, "insider": {}, "got": {}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--fmp", type=int, default=150)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    f_ = out / "hist-us.json.gz"
    H = load(f_)
    today = dt.date.today()
    cut = (today - dt.timedelta(days=REFRESH)).isoformat()
    uni = sorted(us_universe())
    fk, pk = os.getenv("FINNHUB_KEY"), os.getenv("FMP_KEY")
    ses = requests.Session()
    n_f = n_i = 0
    t0 = time.time()
    for s in uni:
        sym = s.replace(".", "-")
        got = H["got"].setdefault(s, {})
        if pk and n_f < a.fmp and got.get("grades", "") < cut:
            try:
                r = ses.get("https://financialmodelingprep.com/stable/grades", params={"symbol": sym, "apikey": pk, "limit": 5000}, timeout=20)
                j = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
                if isinstance(j, list):
                    H["grades"][s] = [[x.get("date", "")[:10], (x.get("action") or "").lower()[:4]] for x in j if x.get("date")]
                    got["grades"] = today.isoformat()
                    n_f += 1
                else:
                    print("FMP 한도 도달 또는 응답 이상 → 오늘은 여기까지", r.status_code, str(r.text)[:80])
                    n_f = a.fmp
            except Exception as e:
                print("FMP 오류", s, type(e).__name__)
        if fk and got.get("insider", "") < cut:
            try:
                r = ses.get("https://finnhub.io/api/v1/stock/insider-transactions", params={"symbol": sym, "token": fk, "from": "2015-01-01"}, timeout=20)
                if r.status_code == 429:
                    time.sleep(30)
                    r = ses.get("https://finnhub.io/api/v1/stock/insider-transactions", params={"symbol": sym, "token": fk, "from": "2015-01-01"}, timeout=20)
                d = (r.json() or {}).get("data") if r.ok else None
                if d is not None:
                    H["insider"][s] = [[x.get("transactionDate", "")[:10], x.get("transactionCode"), x.get("change") or 0,
                                        x.get("transactionPrice") or 0] for x in d if x.get("transactionCode") in ("P", "S") and x.get("transactionDate")]
                    got["insider"] = today.isoformat()
                    n_i += 1
                time.sleep(1.1)                       # 무료 한도 분당 60회
            except Exception as e:
                print("Finnhub 오류", s, type(e).__name__)
    with gzip.open(f_, "wt", encoding="utf-8") as fp:
        json.dump(H, fp)
    cg = sum(1 for s in uni if s in H["grades"])
    ci = sum(1 for s in uni if s in H["insider"])
    print(f"이번에 받음: 등급 {n_f} · 내부자 {n_i} · 누적 등급 {cg}/{len(uni)} · 내부자 {ci}/{len(uni)} · {time.time() - t0:.0f}초")
    (out / "hist-status.json").write_text(json.dumps({"date": today.isoformat(), "grades": cg, "insider": ci, "universe": len(uni)}))


if __name__ == "__main__":
    main()
