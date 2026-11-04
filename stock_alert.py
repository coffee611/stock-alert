# -*- coding: utf-8 -*-
"""
박스권 + 거래량 증가 종목 텔레그램 알림 (GitHub Actions용)
데이터 출처 : 네이버 증권 (m.stock.naver.com 공개 JSON 주소, 비공식)
  - 종목 목록 : 시가총액 순위 (ETF/ETN/레버리지/인버스/스팩/리츠 제외)
  - 종가/거래량/기관 순매수량 : 종목별 투자자 동향
알림 : 조건 1, 2, 3 각각 별도의 텔레그램 메시지 (기관 순매수 큰 순)
"""
import os
import re
import time
import html as htmllib
import datetime as dt
from zoneinfo import ZoneInfo

import requests
import pandas as pd

# =====================================================================
# 설정 (필요하면 숫자만 바꾸세요)
# =====================================================================
WINDOW = 20             # 분석 기간 (거래일)
BOX_MAX_RANGE = 0.20    # 박스권: 20일 종가 (최고-최저)/최저 가 20% 이하

COND1_RATIO = 0.30      # 조건1: 후반10일 평균 거래량이 전반10일 평균보다 30% 이상 증가
COND2_STREAK = 7       # 조건2: 전일 대비 거래량이 '연속'으로 증가한 날 7일 이상
COND3_MULT = 2.0        # 조건3: 20일 평균 거래량의 2배 이상 터진 날이
COND3_COUNT = 3         #        3회 이상

USE_INSTITUTION = True  # True: 기관 20일 누적 순매수량이 플러스인 종목만
PER_MARKET_COUNT = 400  # 코스피/코스닥 각각 시가총액 상위 몇 종목을 검사할지
MAX_PER_CONDITION = 30  # 조건별 메시지에 표시할 최대 종목 수
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

# ETF/ETN/레버리지/스팩/리츠 제외용 이름 규칙
EXCLUDE_PREFIX = ("KODEX", "TIGER", "ACE ", "SOL ", "RISE ", "KBSTAR", "HANARO",
                  "ARIRANG", "KOSEF", "PLUS ", "TIMEFOLIO", "WON ", "1Q ", "KIWOOM ",
                  "TRUSTON", "마이티", "히어로즈", "파워 ", "BNK ", "HK ")
EXCLUDE_WORD = ("ETN", "ETF", "레버리지", "인버스", "선물", "커버드콜", "스팩", "SPAC",
                "리츠", "REIT")


def is_excluded(name, type_text=""):
    n = name.upper()
    if type_text.lower() in ("etf", "etn", "elw"):
        return True
    if n.startswith(tuple(p.upper() for p in EXCLUDE_PREFIX)):
        return True
    return any(w.upper() in n for w in EXCLUDE_WORD)


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
    stocks, seen, skipped = [], set(), 0
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
                tp = str(pick(d, ["stockEndType", "stockType", "endType", "typeCode"]) or "")
                if not CODE_RE.match(code) or code in seen:
                    continue
                seen.add(code)
                if is_excluded(name, tp):
                    skipped += 1
                    continue
                stocks.append((code, name))
    print(f"ETF/레버리지/스팩/리츠 등 제외: {skipped}개")
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
def longest_streak(flags):
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


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
    if ratio >= COND1_RATIO:
        hits.append(1)
    streak = longest_streak((v21.diff().iloc[1:] > 0).tolist())
    if streak >= COND2_STREAK:
        hits.append(2)
    spikes = int((v20 >= v20.mean() * COND3_MULT).sum())
    if spikes >= COND3_COUNT:
        hits.append(3)
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
        "streak": streak, "spikes": spikes,
        "close": int(d20["close"].iloc[-1]),
        "inst_qty": 0 if pd.isna(inst_sum) else inst_sum,
        "inst_amt": 0 if pd.isna(inst_amt) else inst_amt,
    }, "ok", last_date


# ---------------------------------------------------------------- 텔레그램
def tg_send(text, use_html=False):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[오류] 텔레그램 토큰/채팅ID(Secrets)가 설정되지 않았습니다.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if use_html:
        data["parse_mode"] = "HTML"
    try:
        r = requests.post(url, data=data, timeout=15)
        if r.status_code == 200:
            print("텔레그램 전송 성공")
        else:
            print("텔레그램 전송 실패:", r.status_code, r.text)
    except Exception as e:
        print("텔레그램 전송 실패:", e)
    time.sleep(1)


def send_plain(text):
    """오류/안내용 일반 텍스트 메시지"""
    tg_send(text[:3800], use_html=False)


def send_blocks(header, blocks, footer=""):
    """제목 + 종목 블록들을 4000자 이하로 나눠 전송"""
    chunks, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > 3500:
            chunks.append(cur)
            cur = "(이어서)\n"
        cur += "\n" + b
    if footer:
        cur += "\n\n" + footer
    chunks.append(cur)
    for c in chunks:
        tg_send(c, use_html=True)


def esc(s):
    return htmllib.escape(str(s))


COND_INFO = {
    1: ("🟢", "조건1", f"후반10일 평균 거래량이 전반10일보다 {COND1_RATIO*100:.0f}% 이상 증가"),
    2: ("🟡", "조건2", f"거래량이 전일 대비 연속 {COND2_STREAK}일 이상 증가"),
    3: ("🔴", "조건3", f"20일 평균 거래량의 {COND3_MULT:g}배 이상 터진 날 {COND3_COUNT}회 이상"),
}


def tier(r):
    """동시 충족 조건 수에 따른 강조 단계: 3개=🔴, 2개=🟠, 1개=없음"""
    n = len(r["hits"])
    if n >= 3:
        return "🔴", "3개 조건 모두 충족"
    if n == 2:
        return "🟠", "2개 조건 충족"
    return "", ""


def detail_line(k, r):
    if k == 1:
        return f"📈 거래량 후반/전반 <b>{r['ratio']:+.0f}%</b>"
    if k == 2:
        return f"📈 거래량 연속 증가 <b>{r['streak']}일</b>"
    return f"📈 평균 {COND3_MULT:g}배↑ 급증일 <b>{r['spikes']}회</b>"


def send_condition_messages(results, total, nodata, base_date):
    now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    for k in (1, 2, 3):
        icon, title, desc = COND_INFO[k]
        items = [r for r in results if k in r["hits"]]
        items.sort(key=lambda r: r["inst_amt"], reverse=True)  # 기관 순매수 큰 순
        shown = items[:MAX_PER_CONDITION]
        n3 = sum(1 for r in items if len(r["hits"]) >= 3)
        n2 = sum(1 for r in items if len(r["hits"]) == 2)
        tier_txt = ""
        if n3 or n2:
            parts = ([f"🔴 3개충족 {n3}"] if n3 else []) + ([f"🟠 2개충족 {n2}"] if n2 else [])
            tier_txt = " (" + " · ".join(parts) + ")"

        header = (f"{icon} <b>{title}</b> | {esc(desc)}\n"
                  f"🕘 {now}  |  📅 기준일 {esc(base_date)}\n"
                  f"🔎 검사 {total}종목 → <b>해당 {len(items)}종목</b>"
                  f"{tier_txt}\n"
                  f"↕ 기관 순매수 큰 순 정렬\n"
                  f"━━━━━━━━━━━━━━")
        blocks = []
        for n, r in enumerate(shown, 1):
            icon_t, label_t = tier(r)
            mark = f"{icon_t} " if icon_t else ""
            strong_line = (f"{icon_t} <b>{label_t}: 조건{'·'.join(map(str, r['hits']))}</b>\n"
                           if icon_t else "")
            blocks.append(
                f"{mark}<b>{n}. {esc(r['name'])}</b> ({esc(r['code'])})\n"
                f"{strong_line}"
                f"💰 {r['close']:,}원  ·  📦 박스폭 {r['box']:.1f}%\n"
                f"{detail_line(k, r)}\n"
                f"🏦 기관 20일 순매수 <b>{r['inst_amt']:+,.0f}억</b> ({r['inst_qty']:+,.0f}주)\n"
                f"──────────────")
        if not blocks:
            blocks.append("해당 종목이 없습니다.")
        footer = []
        if len(items) > len(shown):
            footer.append(f"… 외 {len(items) - len(shown)}종목 (상위 {len(shown)}개만 표시)")
        if k == 3:
            if nodata:
                footer.append(f"⚠ 자료 미흡/수집 실패 제외: {nodata}개")
            footer.append("🔴 = 조건 3개 모두 충족 / 🟠 = 조건 2개 충족 (규칙 기반 신호)\n"
                          "※ ETF·레버리지·스팩·리츠 제외 / 거래대금 미제공(거래량 기준)\n"
                          "※ 투자 판단과 책임은 본인에게 있습니다.")
        send_blocks(header, blocks, "\n".join(footer))


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
        send_plain(fail_message("네이버 증권에서 종목 목록을 가져오지 못했습니다."))
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
            send_plain(fail_message("종목별 데이터를 거의 읽지 못해 중단했습니다."))
            return
        if i % 100 == 0:
            print(f"  진행 {i}/{len(stocks)}")

    if nodata > len(stocks) * 0.5:
        send_plain(fail_message(f"데이터 수집 실패가 많습니다 ({nodata}/{len(stocks)})."))
        return
    base_date = max(base_dates) if base_dates else "알 수 없음"
    wait_until_0850()
    send_condition_messages(results, len(stocks), nodata, base_date)
    print("완료. 해당 종목:", len(results))


if __name__ == "__main__":
    main()
