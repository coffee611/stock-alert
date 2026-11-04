# -*- coding: utf-8 -*-
"""
박스권 + 거래량 증가 종목 텔레그램 알림 (GitHub Actions용)
데이터 출처 : 네이버 금융 (finance.naver.com)
  - 종목 목록 : 시가총액 순위 페이지
  - 종가/거래량/기관 순매매량 : 종목별 '외국인·기관 매매' 페이지
"""
import os
import re
import time
import datetime as dt
from io import StringIO
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
USE_INSTITUTION = True  # True: 기관 20일 누적 순매매량이 플러스인 종목만
PER_MARKET_PAGES = 8    # 시총 순위 페이지 수 (1페이지=50종목, 코스피/코스닥 각각)
REQUEST_GAP = 0.25      # 요청 간격(초) - 너무 빠르면 차단될 수 있음
# =====================================================================

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
FORCE = os.environ.get("FORCE", "").lower() == "true"  # 수동 실행이면 휴장 체크 생략
KST = ZoneInfo("Asia/Seoul")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://finance.naver.com/",
}
DATE_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")


LAST_INFO = {"msg": ""}  # 마지막 실패 원인 기록 (진단용)


def http_get(url, retry=3):
    for i in range(retry):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            time.sleep(REQUEST_GAP)
            if r.status_code == 200:
                r.encoding = "euc-kr"  # 네이버 금융은 euc-kr
                return r.text
            snippet = r.text[:150].replace("\n", " ")
            LAST_INFO["msg"] = f"HTTP {r.status_code} / {url} / {snippet}"
        except Exception as e:
            LAST_INFO["msg"] = f"접속 오류 {type(e).__name__}: {str(e)[:150]} / {url}"
        print("요청 실패:", LAST_INFO["msg"])
        time.sleep(1 + i)
    return None


# ---------------------------------------------------------------- 종목 목록
def get_universe():
    stocks, seen = [], set()
    pat = re.compile(r'<a href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>')
    for sosok in (0, 1):  # 0=코스피, 1=코스닥
        for page in range(1, PER_MARKET_PAGES + 1):
            html = http_get(f"https://finance.naver.com/sise/sise_market_sum.naver"
                            f"?sosok={sosok}&page={page}")
            if not html:
                continue
            found = pat.findall(html)
            if not found:
                LAST_INFO["msg"] = ("접속은 됐으나 종목을 못 읽음 (화면 구조 변경/차단 페이지 가능성) "
                                    f"/ 응답 길이 {len(html)} / 앞부분: "
                                    + html[:150].replace("\n", " "))
                print(LAST_INFO["msg"])
            for code, name in found:
                if code not in seen:
                    seen.add(code)
                    stocks.append((code, name.strip()))
    return stocks


# ---------------------------------------------------------------- 종목 데이터
def to_num(x):
    try:
        return float(str(x).replace(",", "").replace("+", "").strip())
    except Exception:
        return float("nan")


def fetch_page(code, page):
    """종목별 일자 데이터 1페이지 -> [(날짜, 종가, 거래량, 기관순매매량), ...] 최신순"""
    html = http_get(f"https://finance.naver.com/item/frgn.naver?code={code}&page={page}")
    if not html:
        return None
    try:
        tables = pd.read_html(StringIO(html))
    except Exception:
        return None
    for t in tables:
        if t.shape[1] < 6:
            continue
        first = t.iloc[:, 0].astype(str).str.strip()
        rows = t[first.str.match(DATE_RE)]
        if rows.empty:
            continue
        out = []
        for _, r in rows.iterrows():
            # 열 순서: 날짜, 종가, 전일비, 등락률, 거래량, 기관 순매매량, 외국인 ...
            out.append((str(r.iloc[0]).strip(), to_num(r.iloc[1]),
                        to_num(r.iloc[4]), to_num(r.iloc[5])))
        return out
    return None


class Rows:
    """필요한 만큼만 페이지를 가져오는 도우미 (불필요한 요청 줄이기)"""
    def __init__(self, code):
        self.code, self.page, self.rows, self.ok = code, 0, [], True

    def ensure(self, n):
        while len(self.rows) < n and self.page < 4 and self.ok:
            self.page += 1
            got = fetch_page(self.code, self.page)
            if not got:
                self.ok = False
                break
            self.rows += got
        df = pd.DataFrame(self.rows, columns=["date", "close", "vol", "inst"])
        df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
        return df if len(df) >= n else None


# ---------------------------------------------------------------- 조건 판단
def analyze(code, name):
    """반환: (결과dict 또는 None, 데이터상태 'ok'/'nodata', 기준일)"""
    R = Rows(code)
    d = R.ensure(WINDOW)
    if d is None:
        return None, "nodata", None
    d20 = d.tail(WINDOW).reset_index(drop=True)
    last_date = d20["date"].iloc[-1]

    # 거래량 자료 확인 (없거나 0이면 제외)
    if d20["vol"].isna().any() or (d20["vol"] <= 0).any():
        return None, "nodata", last_date

    # 박스권 (종가 기준) - 통과 못하면 여기서 종료 (요청 절약)
    hi, lo = d20["close"].max(), d20["close"].min()
    if lo <= 0 or (hi - lo) / lo > BOX_MAX_RANGE:
        return None, "ok", last_date

    # 조건3 계산용으로 21일째 데이터 확보
    d21 = R.ensure(WINDOW + 1)
    if d21 is None:
        return None, "nodata", last_date
    d21 = d21.tail(WINDOW + 1).reset_index(drop=True)
    d20 = d21.tail(WINDOW).reset_index(drop=True)
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
           "출처: 네이버 금융 (종가·거래량·기관 순매매량)", ""]
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
                f"    기관 20일 순매매 {r['inst_qty']:,.0f}주 (약 {r['inst_amt']:+,.0f}억원, 추정)")
        msg.append("")
    if nodata:
        msg.append(f"⚠ 자료 미흡/수집 실패로 제외된 종목: {nodata}개")
    msg.append("※ 거래대금은 네이버에서 제공되지 않아 거래량만 사용했습니다.")
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


def main():
    today = dt.datetime.now(KST).date()
    if not FORCE and is_market_closed(today):
        print("휴장일이라 건너뜁니다:", today)
        return
    stocks = get_universe()
    if not stocks:
        send_telegram("⚠ 네이버 금융에서 종목 목록을 가져오지 못했습니다. (접속 차단/화면 변경 가능성)\n"
                      "진단: " + (LAST_INFO["msg"] or "원인 기록 없음"))
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
        if i % 100 == 0:
            print(f"  진행 {i}/{len(stocks)}")

    # 대부분 실패했다면 차단/구조 변경으로 판단
    if nodata > len(stocks) * 0.5:
        send_telegram(f"⚠ 데이터 수집 실패가 많습니다 ({nodata}/{len(stocks)}). "
                      "네이버 접속 차단 또는 화면 구조 변경 가능성이 있습니다.")
        return
    base_date = max(base_dates) if base_dates else "알 수 없음"
    wait_until_0850()
    send_telegram(build_message(results, len(stocks), nodata, base_date))
    print("완료. 해당 종목:", len(results))


if __name__ == "__main__":
    main()
