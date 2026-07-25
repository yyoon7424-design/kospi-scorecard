#!/usr/bin/env python3
"""
코스피 아침 스코어카드 - 데이터 수집 및 점수 계산
GitHub Actions에서 매일 자동 실행됨. 표준 라이브러리만 사용 (의존성 없음).
"""
import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

AV_KEY = os.environ["ALPHAVANTAGE_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

AV_BASE = "https://www.alphavantage.co/query"

FACTORS = [
    {"key": "fx", "name": "원/달러 환율", "direction": -1, "weight": 1.0},
    {"key": "spy", "name": "S&P500 (SPY)", "direction": 1, "weight": 1.0},
    {"key": "qqq", "name": "나스닥 (QQQ)", "direction": 1, "weight": 0.5},
    {"key": "soxx", "name": "반도체ETF (SOXX)", "direction": 1, "weight": 2.0},
    {"key": "wti", "name": "WTI 유가", "direction": -1, "weight": 0.5},
    {"key": "tnote", "name": "미국채 10년물", "direction": -1, "weight": 1.0},
]

SYMBOL_MAP = {"반도체ETF (SOXX)": "SOXX", "S&P500 (SPY)": "SPY", "나스닥 (QQQ)": "QQQ"}


def av_call(params):
    params = dict(params)
    params["apikey"] = AV_KEY
    url = AV_BASE + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if "Note" in data or "Information" in data or "Error Message" in data:
            print("AV call failed:", params.get("function"), data)
            return None
        return data
    except Exception as e:
        print("AV call exception:", params.get("function"), e)
        return None


def pct_change(latest, prev):
    return (latest - prev) / prev * 100


def fetch_fx():
    data = av_call({"function": "FX_DAILY", "from_symbol": "USD", "to_symbol": "KRW", "outputsize": "compact"})
    if not data:
        return None
    series = data.get("Time Series FX (Daily)")
    if not series:
        return None
    dates = sorted(series.keys(), reverse=True)
    latest = float(series[dates[0]]["4. close"])
    prev = float(series[dates[1]]["4. close"])
    chg = pct_change(latest, prev)
    return {"change": chg, "label": f"{chg:+.2f}%"}


def fetch_quote(symbol):
    data = av_call({"function": "GLOBAL_QUOTE", "symbol": symbol})
    if not data:
        return None
    q = data.get("Global Quote")
    if not q or "10. change percent" not in q or not q["10. change percent"]:
        return None
    change = float(q["10. change percent"].replace("%", ""))
    return {"change": change, "label": f"{change:+.2f}%"}


def fetch_wti():
    data = av_call({"function": "WTI", "interval": "monthly"})
    if not data:
        return None
    series = data.get("data")
    if not series or len(series) < 2:
        return None
    latest = float(series[0]["value"])
    prev = float(series[1]["value"])
    chg = pct_change(latest, prev)
    return {"change": chg, "label": f"{chg:+.2f}%(전월비)"}


def fetch_treasury():
    data = av_call({"function": "TREASURY_YIELD", "interval": "monthly", "maturity": "10year"})
    if not data:
        return None
    series = data.get("data")
    if not series or len(series) < 2:
        return None
    latest = float(series[0]["value"])
    prev = float(series[1]["value"])
    return {"change": latest - prev, "label": f"{latest:.2f}% (전월 {prev:.2f}%)"}


def call_claude(prompt, max_tokens=300):
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
        return result["content"][0]["text"]
    except Exception as e:
        print("Claude call exception:", e)
        return None


def fetch_news_summary(symbol, factor_name):
    data = av_call({"function": "NEWS_SENTIMENT", "tickers": symbol, "limit": 5})
    if not data or "feed" not in data or not data["feed"]:
        return None
    articles = data["feed"][:5]
    snippets = "\n".join(f"- {a.get('title', '')}: {a.get('summary', '')}" for a in articles)
    prompt = (
        f"다음은 {factor_name}({symbol}) 관련 최신 뉴스 목록입니다.\n\n{snippets}\n\n"
        f"이 뉴스들을 바탕으로 {factor_name}이(가) 오늘 왜 그렇게 움직였는지 한국어로 2~3문장으로 요약해줘. "
        f"과장하지 말고 사실 위주로 작성해줘."
    )
    return call_claude(prompt)


def main():
    # Alpha Vantage free tier allows 5 requests/minute, so we pace calls out
    # with a delay to stay well under that limit.
    AV_DELAY_SECONDS = 13

    results = {}
    results["fx"] = fetch_fx()
    time.sleep(AV_DELAY_SECONDS)
    results["spy"] = fetch_quote("SPY")
    time.sleep(AV_DELAY_SECONDS)
    results["qqq"] = fetch_quote("QQQ")
    time.sleep(AV_DELAY_SECONDS)
    results["soxx"] = fetch_quote("SOXX")
    time.sleep(AV_DELAY_SECONDS)
    results["wti"] = fetch_wti()
    time.sleep(AV_DELAY_SECONDS)
    results["tnote"] = fetch_treasury()
    time.sleep(AV_DELAY_SECONDS)

    total_weight = 0.0
    score = 0.0
    rows = []
    for f in FACTORS:
        res = results.get(f["key"])
        if not res:
            rows.append({"name": f["name"], "label": "데이터 없음", "weight": f["weight"], "contrib": None})
            continue
        sign = 1 if res["change"] > 0 else (-1 if res["change"] < 0 else 0)
        contrib = sign * f["direction"] * f["weight"]
        total_weight += f["weight"]
        score += contrib
        rows.append({"name": f["name"], "label": res["label"], "weight": f["weight"], "contrib": contrib})

    norm = (score / total_weight) if total_weight > 0 else 0
    if norm >= 0.15:
        signal = "상승 우세"
    elif norm <= -0.15:
        signal = "하락 우세"
    else:
        signal = "중립"

    available = [r for r in rows if r["contrib"] is not None]
    reason = None
    top_factor_name = None
    if available:
        top = max(available, key=lambda r: abs(r["contrib"]))
        top_factor_name = top["name"]
        symbol = SYMBOL_MAP.get(top_factor_name)
        if symbol:
            reason = fetch_news_summary(symbol, top_factor_name)

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "score": score,
        "normalized_score": norm,
        "signal": signal,
        "top_factor": top_factor_name,
        "reason": reason,
    }

    with open("data.json", "w", encoding="utf-8") as fp:
        json.dump(output, fp, ensure_ascii=False, indent=2)

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
