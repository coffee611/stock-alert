# -*- coding: utf-8 -*-
"""
박스권 + 거래량 증가 종목 텔레그램 알림 (GitHub Actions용)
데이터 출처 : 네이버 증권 (m.stock.naver.com 공개 JSON 주소, 비공식)
  - 종목 목록 : 시가총액 순위
  - 종가/거래량/기관 순매수량 : 종목별 투자자 동향
"""
import os
import re
import time
import datetime as dt
from zoneinfo import ZoneInfo

import requests
import pandas as pd

# =====================================================================
# 설정 (필요하면 숫자만 바꾸세요)
# =====================================================================
WINDOW = 20             # 분석 기간 (거래일)
BOX_MAX_RANGE = 0.20    # 박스권: 20일 종가 (최고-최저)/최저 가 20% 이하
COND2_RATIO = 0.20      # 조건2: 후반10일 평균이 전반10일 평균보다 20% 이상 증가
COND3_DAYS = 12         # 조건3: 전일 대비 거래량 증가일 12일 이상
COND4_MULT = 2.0        # 조건4: 20일 평균의 2배 이상 터진 날 1회 이상
USE_INSTITUTION = True  # True: 기관 20일 누적 순매수량이 플러스인 종목만
PER_MARKET_COUNT = 400  # 코스피/코스닥 각각 시가총액 상위 몇 종목을 검사할지
REQUEST_GAP = 0.3       # 요청 간격(초) - 너무 빠르면 차단될 수 있음
# =====================================================================

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
FORCE = os.environ.get("FORCE", "").lower() == "true"  # 수동 실행이면 휴장 체크 생략
KST = ZoneInfo("Asia/Seoul")
M_BASE = "https://m.stock.naver.com"
S_BASE = "https://stock.naver.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                   "Mobile/15E148 Safari/604.1"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.stock.naver.com/",
}
LAST_INFO = {"msg": "", "keys": ""}  # 실패 원인 기록 (진단용)
CODE_RE = re.compile(r"^[0-9A-Z]{6}$")


# ---------------------------------------------------------------- 공통 도우미
def get_json(url, retry=3):
    for i in range(retry):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            time.sleep(REQUEST_GAP)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    LAST_INFO["msg"] = (f"JSON이 아닌 응답 / {url} / "
                                        + r.text[:120].replace("\n", " "))
            else:
                LAST_INFO["msg"] = (f"HTTP {r.status_code} / {url} / "
                                    + r.text[:120].replace("\n", " "))
                if r.status_code in (403, 429):  # 차단 신호 -> 재시도하지 않음
                    print("요청 실패:", LAST_INFO["msg"])
                    return None
        except Exception as e:
            LAST_INFO["msg"] = f"접속 오류 {type(e).__name__}: {str(e)[:120]} / {url}"
        print("요청 실패:", LAST_INFO["msg"])
        time.sleep(1 + i)
    return None


def find_list(obj):
    """응답 안에서 '딕셔너리 목록'을 찾아 돌려줌 (응답 모양이 조금 달라도 동작)"""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            return obj
        for v in obj:
            r = find_list(v)
            if r:
                return r
    elif isinstance(obj, dict):
        for key in ("stocks", "items", "datas", "data", "result", "list", "rows"):
            if key in obj:
                r = find_list(obj[key])
                if r:
                    return r
        for v in obj.values():
            r = find_list(v)
            if r:
                return r
    return []


def pick(d, names):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


def to_num(x):
    try:
        return float(str(x).replace(",", "").replace("+", "").strip())
    except Exception:
        return float("nan")


def inst_value(d):
    """기관 순매수량 항목을 이름으로 찾음"""
    v = pick(d, ["organPureBuyQuant", "organizationPureBuyQuant",
                 "orgnPureBuyQuant", "institutionPureBuyQuant"])
    if v is not None:
        return v
    for k, val in d.items():
        kl = k.lower()
        if (("organ" in kl or "orgn" in kl or "institution" in kl)
                and ("buy" in kl or "quant" in kl or "qty" in kl)
                and "ratio" not in kl and "amount" not in kl):
            return val
    return None


# ---------------------------------------------------------------- 종목 목록
def get_universe():
    stocks, seen = [], set()
    pages = (PER_MARKET_COUNT + 99) // 100
    for market in ("KOSPI", "KOSDAQ"):
        for page in range(1, pages + 1):
            data = get_json(f"{M_BASE}/api/stocks/marketValue/{market}"
                            f"?page={page}&pageSize=100")
            lst = find_list(data) if data is not None else []
            if not lst:
                LAST_INFO["msg"] = LAST_INFO["msg"] or f"종목 목록이 비어 있음 ({market} {page}페이지)"
                break
            for d in lst:
                code = str(pick(d, ["itemCode", "stockCode", "code", "symbolCode"]) or "")
                name = str(pick(d, ["stockName", "itemName", "name"]) or code)
                if CODE_RE.match(code) and code not in seen:
                    seen.add(code)
                    stocks.append((code, name))
    return stocks


# ---------------------------------------------------------------- 종목 데이터
def rows_to_df(lst):
    out = []
    for d in lst:
        date = re.sub(r"\D", "", str(pick(d, ["bizdate", "localTradedAt", "tradeDate",
                                              "date", "businessDate"]) or ""))
        out.append((date,
                    to_num(pick(d, ["closePrice", "close"])),
                    to_num(pick(d, ["accumulatedTradingVolume", "tradingVolume", "volume"])),
                    to_num(inst_value(d))))
    df = pd.DataFrame(out, columns=["date", "close", "vol", "inst"])
    df = df[df["date"].str.len() == 8]
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def get_rows(code):
    """최근 21거래일 (날짜, 종가, 거래량, 기관순매수량) -> DataFrame 또는 None"""
    need = WINDOW + 1
    urls = [f"{M_BASE}/api/stock/{code}/trend?pageSize={need}&page=1",
            f"{S_BASE}/api/domestic/detail/{code}/trend?tradeType=KRX&startIdx=0&pageSize={need}"]
    for url in urls:
        data = get_json(url, retry=2)
        lst = find_list(data) if data is not None else []
        if not lst:
            continue
        if not LAST_INFO["keys"]:
            LAST_INFO["keys"] = ", ".join(list(lst[0].keys())[:15])
        df = rows_to_df(lst)
        # 종가/거래량이 빠져 있으면 일별 시세에서 보충
        if df["close"].isna().all() or df["vol"].isna().all():
            pdata = get_json(f"{M_BASE}/api/stock/{code}/price?pageSize={need}&page=1", retry=2)
            plst = find_list(pdata) if pdata is not None else []
            if plst:
                pdf = rows_to_df(plst)[["date", "close", "vol"]]
                df = df[["date", "inst"]].merge(pdf, on="date", how="inner")
        if len(df) >= need:
            return df.tail(need).reset_index(drop=True)
    return None


# ---------------------------------------------------------------- 조건 판단
def analyze(code, name):
    """반환: (결과dict 또는 None, 데이터상태 'ok'/'nodata', 기준일)"""
    d21 = get_rows(code)
    if d21 is None:
        return None, "nodata", None
    d20 = d21.tail(WINDOW).reset_index(drop=True)
    last_date = d20["date"].iloc[-1]
    last_date = f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:]}"

    # 거래량/종가 자료 확인 (없거나 0이면 제외)
    if (d21["vol"].isna().any() or (d21["vol"] <= 0).any()
            or d21["close"].isna().any()):
        return None, "nodata", last_date

    # 박스권 (종가 기준)
    hi, lo = d20["close"].max(), d20["close"].min()
    if lo <= 0 or (hi - lo) / lo > BOX_MAX_RANGE:
        return None, "ok", last_date

    v21, v20 = d21["vol"], d20["vol"]
    hits = []
    ratio = v20.iloc[10:].mean() / v20.iloc[:10].mean() - 1
    if ratio >= COND2_RATIO:
        hits.append(2)
    up_days = int((v21.diff().iloc[1:] > 0).sum())
    if up_days >= COND3_DAYS:
        hits.append(3)
    spikes = int((v20 >= v20.mean() * COND4_MULT).sum())
    if spikes >= 1:
        hits.append(4)
    if not hits:
        return None, "ok", last_date

    # 기관 수급
    inst_sum = d20["inst"].sum()
    inst_amt = (d20["inst"] * d20["close"]).sum() / 1e8  # 억원(추정)
    if USE_INSTITUTION:
        if d20["inst"].isna().any():
            return None, "nodata", last_date
        if inst_sum <= 0:
            return None, "ok", last_date

    return {
        "code": code, "name": name, "hits": hits,
        "box": (hi - lo) / lo * 100, "ratio": ratio * 100,
        "up_days": up_days, "spikes": spikes,
        "close": int(d20["close"].iloc[-1]),
        "inst_qty": inst_sum, "inst_amt": inst_amt,
    }, "ok", last_date


# ---------------------------------------------------------------- 텔레그램
def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[오류] 텔레그램 토큰/채팅ID(Secrets)가 설정되지 않았습니다.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        try:
            r = requests.post(url, data={"chat_id": TG_CHAT_ID, "text": c}, timeout=15)
            if r.status_code == 200:
                print("텔레그램 전송 성공")
            else:
                print("텔레그램 전송 실패:", r.status_code, r.text)
        except Exception as e:
            print("텔레그램 전송 실패:", e)


def build_message(results, total, nodata, base_date):
    now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    titles = {
        2: "[조건2] 박스권 + 후반10일 평균 거래량 20%↑",
        3: f"[조건3] 박스권 + 전일대비 증가일 {COND3_DAYS}일↑",
        4: f"[조건4] 박스권 + 20일 평균 {COND4_MULT:.0f}배 터진 날 1회↑",
    }
    msg = [f"📊 수급 박스권 알림 ({now})",
           f"데이터 기준일: {base_date}",
           f"검사 {total}종목 / 해당 {len(results)}종목",
           "출처: 네이버 증권 (종가·거래량·기관 순매수량)", ""]
    for k in (2, 3, 4):
        items = [r for r in results if k in r["hits"]]
        msg.append(f"{titles[k]}  ({len(items)}개)")
        if not items:
            msg.append("  - 해당 종목 없음")
        for r in items:
            msg.append(
                f"  • {r['name']}({r['code']}) {r['close']:,}원\n"
                f"    박스폭 {r['box']:.1f}% | 후반/전반 {r['ratio']:+.0f}% | "
                f"증가일 {r['up_days']}일 | 급증일 {r['spikes']}회\n"
                f"    기관 20일 순매수 {r['inst_qty']:,.0f}주 (약 {r['inst_amt']:+,.0f}억원, 추정)")
        msg.append("")
    if nodata:
        msg.append(f"⚠ 자료 미흡/수집 실패로 제외된 종목: {nodata}개")
    msg.append("※ 거래대금은 제공되지 않아 거래량만 사용했습니다.")
    msg.append("※ 투자 판단과 책임은 본인에게 있습니다.")
    return "\n".join(msg)


# ---------------------------------------------------------------- 실행
def wait_until_0850():
    """아침(9시 이전) 자동 실행이면 계산을 먼저 끝내고 08:50까지 기다렸다가 전송"""
    now = dt.datetime.now(KST)
    if FORCE or now.hour >= 9:
        return
    target = now.replace(hour=8, minute=50, second=0, microsecond=0)
    wait = (target - now).total_seconds()
    if 0 < wait <= 40 * 60:  # 최대 40분까지만 대기
        print(f"08:50까지 {int(wait)}초 대기")
        time.sleep(wait)


def is_market_closed(today):
    if today.weekday() >= 5:
        return True
    if (today.month, today.day) == (12, 31):  # 연말 휴장
        return True
    try:
        import holidays
        return today in holidays.KR(years=today.year)
    except Exception:
        return False


def fail_message(prefix):
    return (f"⚠ {prefix}\n진단: {LAST_INFO['msg'] or '원인 기록 없음'}\n"
            f"응답 항목: {LAST_INFO['keys'] or '없음'}")


def main():
    today = dt.datetime.now(KST).date()
    if not FORCE and is_market_closed(today):
        print("휴장일이라 건너뜁니다:", today)
        return
    stocks = get_universe()
    if not stocks:
        send_telegram(fail_message("네이버 증권에서 종목 목록을 가져오지 못했습니다."))
        return
    print("검사 종목 수:", len(stocks))

    results, nodata, base_dates = [], 0, []
    for i, (code, name) in enumerate(stocks, 1):
        try:
            r, status, last = analyze(code, name)
        except Exception as e:
            print("오류", code, e)
            r, status, last = None, "nodata", None
        if status == "nodata":
            nodata += 1
        if last:
            base_dates.append(last)
        if r:
            results.append(r)
        if i == 30 and nodata >= 28:  # 처음부터 거의 다 실패 -> 빨리 중단하고 원인 알림
            send_telegram(fail_message("종목별 데이터를 거의 읽지 못해 중단했습니다."))
            return
        if i % 100 == 0:
            print(f"  진행 {i}/{len(stocks)}")

    if nodata > len(stocks) * 0.5:
        send_telegram(fail_message(f"데이터 수집 실패가 많습니다 ({nodata}/{len(stocks)})."))
        return
    base_date = max(base_dates) if base_dates else "알 수 없음"
    wait_until_0850()
    send_telegram(build_message(results, len(stocks), nodata, base_date))
    print("완료. 해당 종목:", len(results))


if __name__ == "__main__":
    main()
