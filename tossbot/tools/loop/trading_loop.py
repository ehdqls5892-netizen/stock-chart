"""스스로 고치는 매매 점검 루프 (미장 스윙).

시험 → 원인 찾기 → 규칙 수정안 → 다시 시험을 자동으로 돌리고, 통과한 수정안만 '제안'으로 남겨요.
로봇 설정(config.json)은 절대 직접 바꾸지 않아요. 반영은 사용자가 승인할 때만.

과거에 맞추기만 한 '가짜 개선'을 막는 장치:
  - 고치는 기간(앞 구간)과 확인하는 기간(뒤 구간)을 나눔. 둘 다 나빠지지 않고, 확인 기간에서 확실히 좋아져야 통과
  - 후보 일부를 무작위로 빼고 여러 번 돌려 평균 (운에 덜 좌우)
  - 실패한 수정은 research/failure-journal.jsonl 에 남겨 180일 동안 다시 시험하지 않음
  - 사용자가 정한 규칙(LOCKED)은 수정안에서 아예 건드리지 않음

사용: python tools/loop/trading_loop.py [--seeds 3] [--rounds 3] [--send]
자료: LOOP_DATA 폴더의 s10-us.pkl.gz + feat10-us.pkl (tools/swing_data.py us --years 10 → tools/loop/feat.py us)
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("BT_PRE", "s10-")
os.environ.setdefault("BT_FEAT", "feat10-")
import sim  # noqa: E402

JOURNAL = ROOT / "research" / "failure-journal.jsonl"
REPORT = ROOT / "research" / "loop-report.md"
PROPOSALS = ROOT / "research" / "loop-proposals.json"
SPLIT = "2022-12-31"            # 이 날까지 = 고치는 기간, 다음 날부터 = 확인 기간
RETRY_DAYS = 180

# 사용자가 직접 정한 규칙 — 루프가 바꾸자고 제안하지 않음
LOCKED = {
    "stop_pct": "손절선 -5% 고정 (사용자 결정 2026-11-11)",
    "risk": "종목당 손실 한도 1.5% (사용자 결정)",
    "regime": "지수 50일선 아래여도 쉬지 않음 (사용자 결정)",
    "pt": "+4% 첫 익절 1/3 (사용자 결정)",
    "plock": "수익 잠금 사다리 (사용자 결정)",
    "market": "국장 거래 안 함·ETF 없음 (사용자 결정 2026-11-13)",
}


def baseline() -> dict:
    """지금 로봇 미장 설정 → 시험기 설정."""
    c = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["daily_swing"]
    pt = c.get("profit_take") or {}
    pl = c.get("profit_lock") or {}
    stop = (c.get("stop_by_market") or {}).get("US") or {}
    lr = c.get("loss_review") or {}
    return dict(
        early=None, slip=0.002, rerank=True,
        min_fs=c.get("min_forward_score", 3), exit_fs=c.get("exit_forward_score", 0),
        max_pos=(c.get("max_pos_by_market") or {}).get("US", c.get("max_pos", 6)),
        open_risk=(c.get("max_open_risk_by_market") or {}).get("US", 4),
        risk=c.get("risk_pct", 1.0), cap=c.get("cap_pct", 20),
        stop_pct=stop.get("pct"), stop_atr=stop.get("atr", 2.0),
        max_stop=c.get("max_stop_pct", 0.12),
        pt=pt.get("pct") if pt.get("enabled") else None, pt_frac=pt.get("frac", 0.34), pt_be=pt.get("be", True),
        plock={k: pl[k] for k in ("steps", "trail_from", "trail_pct", "take2_pct", "take2_frac") if k in pl} if pl.get("enabled") else None,
        exit_ma=c.get("exit_ma", 10), time_days=c.get("time_stop_days", 10), time_R=c.get("time_need_R", 0.5),
        be_R=c.get("be_after_R", 1), trail_R=c.get("trail_after_R", 2), trail_atr=c.get("trail_atr", 3),
        lr="close_fs" if lr.get("enabled") else "none", lr_pct=lr.get("pct", 3), lr_k=lr.get("max_score", 2),
        max_new=((c.get("max_new_by_market") or {}).get("US") or 99),
    )


# 시험할 수정안 (잠긴 규칙은 없음). id 는 실패 기록과 맞추는 열쇠라 바꾸지 말 것.
CANDIDATES = [
    ("min_fs_5", "후보 점수 5 이상만 사기 (지금 3)", {"min_fs": 5}),
    ("min_fs_7", "후보 점수 7 이상만 사기", {"min_fs": 7}),
    ("top_15", "밤 후보 상위 15개만 보기 (지금 30)", {"top": 15}),
    ("max_new_1", "하루 새 매수 1종목 (지금 2)", {"max_new": 1}),
    ("max_new_3", "하루 새 매수 3종목", {"max_new": 3}),
    ("max_pos_6", "최대 보유 6종목 (지금 8)", {"max_pos": 6}),
    ("open_risk_6", "열린 위험 합계 6% (지금 4%)", {"open_risk": 6}),
    ("exit_ma_5", "5일선 아래 마감이면 정리 (지금 10일선)", {"exit_ma": 5}),
    ("time_15", "15일 지나도 +0.5R 못 가면 정리 (지금 10일)", {"time_days": 15}),
    ("time_20_1R", "20일 지나도 +1R 못 가면 정리", {"time_days": 20, "time_R": 1}),
    ("trail_R_1_5", "+1.5R 부터 추적 손절 (지금 +2R)", {"trail_R": 1.5}),
    ("trail_R_3", "+3R 부터 추적 손절", {"trail_R": 3}),
    ("trail_atr_2", "추적 손절 폭 2×변동폭 (지금 3×)", {"trail_atr": 2}),
    ("be_R_off", "+1R 본전 이동 끄기", {"be_R": 99}),
    ("exit_fs_2", "현재·미래 점수 2 이하로 떨어지면 정리 (지금 0)", {"exit_fs": 2}),
    ("lr_off", "손실 -3%·점수≤2 마감 정리 끄기", {"lr": "none"}),
    ("lr_k_4", "손실 -3%·점수≤4 면 마감 정리 (지금 ≤2)", {"lr_k": 4}),
    ("max_ext_8", "20일선보다 8% 넘게 떠 있으면 안 사기 (추격 매수 막기)", {"max_ext": 0.08}),
    ("max_stop_8", "손절 폭이 8% 넘는 종목은 안 사기 (지금 12%)", {"max_stop": 0.08}),
    ("min_cq_0", "차트 모양 점수 0 이상만 사기 (지금 -2)", {"min_cq": 0}),
    ("guard_1", "지수가 그날 -1% 넘게 빠지면 새로 안 사기 (지금 -2%)", {"guard": -1.0}),
    ("max_hold_30", "최대 30일 보유", {"max_hold": 30}),
    ("time_5", "5일 지나도 +0.5R 못 가면 정리", {"time_days": 5}),
    ("time_3", "3일 지나도 +0.5R 못 가면 정리", {"time_days": 3}),
    ("max_pos_10", "최대 보유 10종목", {"max_pos": 10}),
    ("exit_ma_off", "10일선 정리 끄기", {"exit_ma": 0}),
    ("exit_ma_20", "20일선 아래 마감이면 정리", {"exit_ma": 20}),
]


FAC_CANDIDATES = [
    ("an_nodown90", "최근 90일 애널리스트 하향이 상향보다 많으면 안 사기", "an", lambda an, ins: an >= 0),
    ("an_up90", "최근 90일 애널리스트 상향이 하향보다 많을 때만 사기", "an", lambda an, ins: an >= 1),
    ("ins_nosell5m", "최근 90일 내부자 순매도 500만 달러 넘으면 안 사기", "ins", lambda an, ins: ins > -5e6),
    ("ins_nosell1m", "최근 90일 내부자 순매도 100만 달러 넘으면 안 사기", "ins", lambda an, ins: ins > -1e6),
    ("ins_buy90", "최근 90일 내부자 순매수가 있을 때만 사기", "ins", lambda an, ins: ins > 0),
    ("an_ins_both", "애널리스트 하향 우세 또는 내부자 순매도 500만 달러 넘으면 안 사기", "both", lambda an, ins: an >= 0 and ins > -5e6),
]


def load_hist(codes):
    """hist-us.json.gz → (종목, 날짜)마다 '최근 90일 등급 상향−하향', '최근 90일 내부자 순매수 금액'. 그날 이전 기록만 씀."""
    import bisect
    import gzip
    p = Path(os.getenv("LOOP_HIST", os.getenv("LOOP_DATA", str(HERE / "data")))) / "hist-us.json.gz"
    if not p.exists():
        return None, {"grades": 0, "insider": 0, "universe": len(codes)}
    H = json.loads(gzip.open(p).read())
    G, I = {}, {}
    for c, rows in (H.get("grades") or {}).items():
        ev = sorted((d, 1 if a == "upgr" else -1) for d, a in rows if a in ("upgr", "down"))
        G[c] = ([d for d, _ in ev], [0] + list(__import__("itertools").accumulate(v for _, v in ev)))
    for c, rows in (H.get("insider") or {}).items():
        ev = sorted((d, float(ch or 0) * float(px or 0)) for d, code, ch, px in rows)
        I[c] = ([d for d, _ in ev], [0] + list(__import__("itertools").accumulate(v for _, v in ev)))

    def win(T, c, d):
        if c not in T:
            return 0.0
        ds, cs = T[c]
        d0 = (dt.date.fromisoformat(str(d)[:10]) - dt.timedelta(days=90)).isoformat()
        hi = bisect.bisect_left(ds, str(d)[:10])          # 그날 '이전' 기록만
        lo = bisect.bisect_left(ds, d0)
        return cs[hi] - cs[lo]
    cov = {"grades": sum(1 for c in codes if c in G), "insider": sum(1 for c in codes if c in I), "universe": len(codes)}
    return (lambda c, d: win(G, c, d), lambda c, d: win(I, c, d)), cov


def journal() -> list:
    try:
        return [json.loads(x) for x in JOURNAL.read_text(encoding="utf-8").splitlines() if x.strip()]
    except FileNotFoundError:
        return []


def recent_fail(jr: list, cid: str) -> dict | None:
    cut = (dt.date.today() - dt.timedelta(days=RETRY_DAYS)).isoformat()
    for r in reversed(jr):
        if r.get("id") == cid and r.get("verdict") == "fail" and r.get("date", "") >= cut:
            return r
    return None


def avg(m, P, seeds, d0=None, d1=None):
    rs = [sim.run(m, {**P, "seed": s}, d0, d1) for s in range(seeds)]
    k = lambda f: sum(r[f] for r in rs) / len(rs)
    return dict(cagr=round(k("cagr"), 2), mdd=round(k("mdd"), 1), n=round(k("n")), win=round(k("win"), 1))


def score(P, seeds):
    return {"fix": avg("us", P, seeds, None, SPLIT), "check": avg("us", P, seeds, (dt.date.fromisoformat(SPLIT) + dt.timedelta(days=1)).isoformat(), None)}


def verdict(r, b, gain=1.0):
    """통과: 확인 기간 연 수익 +gain%p 이상 좋아짐 · 고치는 기간도 0.5%p 넘게 나빠지지 않음 · 확인 기간 최대 낙폭 3%p 넘게 늘지 않음."""
    why = []
    if r["check"]["cagr"] < b["check"]["cagr"] + gain:
        why.append(f"확인 기간 수익 {r['check']['cagr']:+.1f}% (기준 {b['check']['cagr']:+.1f}%) — 충분히 안 좋아짐")
    if r["fix"]["cagr"] < b["fix"]["cagr"] - 0.5:
        why.append(f"고치는 기간 수익이 나빠짐 {r['fix']['cagr']:+.1f}% (기준 {b['fix']['cagr']:+.1f}%)")
    if r["check"]["mdd"] > b["check"]["mdd"] + 3:
        why.append(f"최대 낙폭이 커짐 {r['check']['mdd']:.0f}% (기준 {b['check']['mdd']:.0f}%)")
    return ("pass" if not why else "fail"), why


def send(text):
    tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    if not tok or not chat:
        return
    for i in range(0, len(text), 3800):
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[i:i + 3800]}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=20).read()
        except Exception as e:
            print("텔레그램 실패:", e, file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    today = dt.date.today().isoformat()
    D = sim.load("us")[0]
    data_end = str(max(b[0] for b in D["index"]["SPY"]))
    base_P = baseline()
    base = score(base_P, a.seeds)
    print("기준(지금 설정):", base, flush=True)
    codes = list(sim.load("us")[0]["bars"])
    fn, cov = load_hist(codes)
    print("과거 정보 범위:", cov, flush=True)
    fac_ready = []
    if fn:
        an_f, ins_f = fn
        for cid, desc, need, rule in FAC_CANDIDATES:
            ok_g = cov["grades"] >= 0.9 * cov["universe"]
            ok_i = cov["insider"] >= 0.9 * cov["universe"]
            if (need == "an" and not ok_g) or (need == "ins" and not ok_i) or (need == "both" and not (ok_g and ok_i)):
                continue
            sim.FAC[cid] = (lambda r: (lambda c, d: r(an_f(c, d), ins_f(c, d))))(rule)
            fac_ready.append((cid, desc, {"fac": cid}))
    cands = CANDIDATES + fac_ready
    jr = journal()
    new_lines, skipped, passed = [], [], []
    cur_P, cur = dict(base_P), base
    tried = set()
    for rnd in range(1, a.rounds + 1):
        found = []
        for cid, desc, diff in cands:
            if cid in tried:
                continue
            if any(k in LOCKED for k in diff):
                continue
            old = recent_fail(jr + new_lines, cid)
            if old and rnd == 1:
                skipped.append((cid, desc, old.get("date")))
                tried.add(cid)
                continue
            r = score({**cur_P, **diff}, a.seeds)
            v, why = verdict(r, cur)
            print(f"[{rnd}] {cid:14s} {v} 고침 {r['fix']['cagr']:+.1f} 확인 {r['check']['cagr']:+.1f} 낙폭 {r['check']['mdd']:.0f}", flush=True)
            rec = dict(id=cid, desc=desc, diff=diff, date=today, data_end=data_end, round=rnd, verdict=v, why=why,
                       result=r, base=cur, on_top_of=[p[0] for p in passed])
            if v == "fail":
                new_lines.append(rec)
                tried.add(cid)
            else:
                found.append((r["check"]["cagr"] - cur["check"]["cagr"], cid, desc, diff, r))
        if not found:
            break
        found.sort(reverse=True)
        g, cid, desc, diff, r = found[0]           # 가장 좋은 수정 하나만 채택 → 그 위에서 나머지 다시 시험 (루프)
        passed.append((cid, desc, diff, r, cur))
        tried.add(cid)
        cur_P, cur = {**cur_P, **diff}, r
    # 기록
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        for rec in new_lines:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        for cid, desc, diff, r, b in passed:
            f.write(json.dumps(dict(id=cid, desc=desc, diff=diff, date=today, data_end=data_end, verdict="pass", result=r, base=b),
                               ensure_ascii=False) + "\n")
    PROPOSALS.write_text(json.dumps(dict(date=today, data_end=data_end, base=base, final=cur,
                                         proposals=[dict(id=c, desc=d, diff=x, result=r) for c, d, x, r, _ in passed]),
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    L = [f"# 매매 점검 루프 결과 ({today}, 자료 ~{data_end})", "",
         f"고치는 기간 ~{SPLIT} / 확인 기간 그 뒤 · 무작위 {a.seeds}번 평균 · 미장 스윙(차트 점수 부분만 — 애널리스트·AI·수급은 빠짐)", "",
         f"과거 정보(그때 알 수 있던 것만): 애널리스트 등급 기록 {cov['grades']}/{cov['universe']}종목 · 내부자 거래 {cov['insider']}/{cov['universe']}종목"
         + ("" if fac_ready else " — 90% 넘게 모이면 이 정보로 거르는 수정안도 시험"), "",
         f"**지금 설정**: 고치는 기간 연 {base['fix']['cagr']:+.1f}% · 확인 기간 연 {base['check']['cagr']:+.1f}% (최대 낙폭 {base['check']['mdd']:.0f}%)", ""]
    if passed:
        L.append("## 통과한 수정안 (승인해야 반영)")
        for i, (cid, desc, diff, r, b) in enumerate(passed, 1):
            L.append(f"{i}. {desc} — 확인 기간 연 {b['check']['cagr']:+.1f}% → **{r['check']['cagr']:+.1f}%**, "
                     f"고치는 기간 {b['fix']['cagr']:+.1f}% → {r['fix']['cagr']:+.1f}%, 낙폭 {b['check']['mdd']:.0f}% → {r['check']['mdd']:.0f}%")
        L.append(f"\n전부 합치면: 확인 기간 연 {base['check']['cagr']:+.1f}% → **{cur['check']['cagr']:+.1f}%**")
    else:
        L.append("## 통과한 수정안 없음 — 지금 설정 유지")
    L += ["", f"## 이번에 실패한 수정 ({len(new_lines)}건, 180일 동안 다시 안 함)"]
    L += [f"- {r['desc']}: {'; '.join(r['why'])}" for r in new_lines]
    if skipped:
        L += ["", f"## 이미 실패해서 건너뛴 수정 ({len(skipped)}건)"] + [f"- {d} (실패 {dd})" for _, d, dd in skipped]
    L += ["", "## 잠긴 규칙 (루프가 안 건드림)"] + [f"- {v}" for v in LOCKED.values()]
    REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    if a.send:
        msg = [f"🔁 매매 점검 루프 ({today})", f"지금 설정: 확인 기간 연 {base['check']['cagr']:+.1f}% (낙폭 {base['check']['mdd']:.0f}%)"]
        if passed:
            msg.append("통과한 수정안 — 승인하셔야 반영돼요:")
            msg += [f"{i}. {d} → 연 {r['check']['cagr']:+.1f}%" for i, (_, d, _, r, _) in enumerate(passed, 1)]
        else:
            msg.append("통과한 수정안 없음 → 지금 설정 그대로")
        msg.append(f"실패 {len(new_lines)}건 · 건너뜀 {len(skipped)}건 (자세히: research/loop-report.md)")
        send("\n".join(msg))


if __name__ == "__main__":
    main()
