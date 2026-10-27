"""실시간 뉴스: 종목 찾기·호재/악재 나누기 시험 (python -m tests.test_news)."""
from bot.news import NewsWatch

kr = NewsWatch("KR", {"005930": "삼성전자", "005935": "삼성전자우", "267260": "HD현대일렉트릭", "001680": "대상",
                      "042700": "한미반도체", "000660": "SK하이닉스"}, print)
cases = [
    ("[특징주] HD현대일렉트릭, 북미 변압기 대규모 수주에 급등", ["267260"], 1),
    ("한미반도체, 목표주가 상향…HBM 장비 수요", ["042700"], 1),
    ("삼성전자우 강세, 삼성전자는 약보합", ["005935", "005930"], 1),
    ("SK하이닉스 유상증자 우려에 급락", ["000660"], -1),
    ("○○제약, 유상증자 철회에 반등", [], 1),
    ("정부 지원 대상 기업 발표", [], 0),
    ("[특징주] 대상, 실적 개선 기대감에 강세", ["001680"], 1),
]
for title, codes, tone in cases:
    got, tn = kr.match(title), kr.tone(title)
    print(f"{title} → {got} {tn:+d}")
    assert got == codes, (title, got)
    assert tn == tone, (title, tn)

us = NewsWatch("US", {"NVDA": "NVIDIA Corporation", "AAPL": "Apple Inc.", "DIS": "The Walt Disney Company"}, print)
for title, codes, tone in [
    ("Nvidia shares jump after analyst upgrade", ["NVDA"], 1),
    ("Apple stock falls as iPhone sales miss estimates", ["AAPL"], -1),
    ("Acme Robotics (NASDAQ: ACME) soars on FDA approval", ["ACME"], 1),
]:
    got, tn = us.match(title), us.tone(title)
    print(f"{title} → {got} {tn:+d}")
    assert got == codes, (title, got)
    assert tn == tone, (title, tn)

xml = """<rss><channel><item><title>[특징주] 한미반도체, 공급계약 체결에 상승 - 테스트뉴스</title><link>http://a/1</link>
<pubDate>Tue, 27 Oct 2026 01:00:00 GMT</pubDate><source url="x">테스트뉴스</source></item></channel></rss>"""
kr.max_age = 10 ** 7
out = kr.handle(kr.parse(xml))
assert out and out[0][0] == "042700" and out[0][1] == 1 and "042700" in kr.good
assert kr.handle(kr.parse(xml)) == []        # 같은 기사는 한 번만
print("✅ 뉴스 감시 시험 통과")
