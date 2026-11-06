# -*- coding: utf-8 -*-
"""
박스권 + 거래량 증가 종목 텔레그램 알림 (GitHub Actions용)
데이터 출처 : 1순위 네이버 증권 (m.stock.naver.com 공개 JSON 주소, 비공식)
              2순위(백업) 한국거래소 KRX 정보데이터시스템 (pykrx 라이브러리, KRX 회원 ID/PW 필요)
  - 종목 목록 : 시가총액 순위 (ETF/ETN/레버리지/인버스/스팩/리츠 제외)
  - 종가/거래량/기관 순매수량
  - DATA_SOURCE: auto(네이버 실패 시 KRX 전환) / naver / krx
알림 : 조건 1, 2, 3 각각 별도의 텔레그램 표 이미지 (기관 순매수 큰 순, 실패 시 글자 메시지로 대체)
추가 정보 : 박스 안 위치 / 급증일 가격 방향 / 60일선 방향(단순+엄격)
성과 기록 : history/alerts.csv 에 알림 종목을 저장하고 5·10·20일 뒤 수익률을 자동 계산
자료 신선도 : 매 실행마다 기준일/지연 여부를 history/freshness.csv 에 기록하고, 하루 이상 늦으면 알림에 경고 표시
별도 알림 : 「기관 5일 순매수 금액 TOP 40」 (1~20위, 21~40위 표 이미지 2장) - 위 조건과 무관한 현황판
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
COND2_STREAK = 7        # 조건2: 전일 대비 거래량이 '중간에 끊기지 않고' 연속 증가한 일수
COND3_MULT = 2.0        # 조건3: 20일 평균 거래량의 2배 이상 터진 날이
COND3_COUNT = 3         #        3회 이상

MA_PERIOD = 60          # 이동평균선 기간
MA_FLAT_PCT = 2.0       # 60일선 20일 변화율이 ±2% 이내면 '횡보'
MA_STRICT_DAYS = 14     # 엄격 기준: 20일 중 60일선이 전일보다 오른(내린) 날이 14일(70%) 이상

# ⭐ 눌림목 패턴 (박스 하단 + 급증일 상승일 + 60일선 엄격 상승 + 종가가 60일선 근처)
PULLBACK_BOX_MAX = 33       # 박스 위치 33% 이하(하단)
PULLBACK_SPIKE_UP_MIN = 2   # 급증일 중 종가가 전일보다 오른 날 2일 이상
PULLBACK_MA_GAP = 3.0       # 종가가 60일선 대비 ±3% 이내

USE_INSTITUTION = True  # True: 기관 20일 누적 순매수량이 플러스인 종목만
PER_MARKET_COUNT = 400  # 코스피/코스닥 각각 시가총액 상위 몇 종목을 검사할지
MAX_PER_CONDITION = 30  # 조건별 메시지에 표시할 최대 종목 수
REQUEST_GAP = 0.3       # 요청 간격(초) - 너무 빠르면 차단될 수 있음

HISTORY_PATH = "history/alerts.csv"  # 성과 기록 파일
MIN_SAMPLE = 5          # 성과 통계를 보여주기 위한 최소 표본 수
# =====================================================================

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
FORCE = os.environ.get("FORCE", "").lower() == "true"  # 수동 실행이면 휴장 체크 생략
DATA_SOURCE = (os.environ.get("DATA_SOURCE") or "auto").strip().lower()  # auto | naver | krx
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
FAIL_LOG = []                        # 출처별 실패 기록 (모두 실패했을 때 알림에 사용)
FRESH = {}                           # 이번 실행의 자료 신선도 (예상 기준일 / 실제 기준일 / 지연 일수)
FRESH_PATH = "history/freshness.csv"
RAW_CAPS = {}                        # 네이버 응답의 시가총액 원본값 (TOP 40 시총 대비 %용)
CAPS_EOK = {}                        # KRX 시가총액 (억원)

TOP_N = 40              # 기관 순매수 TOP 몇 종목
TOP_DAYS = 5            # 최근 몇 거래일 기준
TOP_FORMAT = "image"    # TOP 40 표시 방식: image(표 이미지) / text(글자) / both  (이미지가 실패하면 자동으로 글자로 대체)
COND_FORMAT = "image"   # 조건 1·2·3 표시 방식: image(표 이미지) / text(글자) / both  (이미지가 실패하면 자동으로 글자로 대체)
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
                cap_raw = pick(d, ["marketValue", "marketCap", "marketCapitalization", "marketSum"])
                if cap_raw is not None:
                    RAW_CAPS[code] = to_num(cap_raw)
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


def get_history(code, need):
    """종가 이력(오래된 날짜 -> 최신) 최소 need일치. 후보 종목에만 사용. 실패 시 None"""
    if DATA_SOURCE == "krx":
        return get_history_krx(code, need)
    rows = []
    df = None
    for page in (1, 2, 3):
        data = get_json(f"{M_BASE}/api/stock/{code}/price?pageSize=60&page={page}", retry=2)
        lst = find_list(data) if data is not None else []
        if not lst:
            break
        rows += lst
        df = rows_to_df(rows)
        df = df[df["close"].notna()]
        if len(df) >= need:
            break
    if df is None or len(df) < need:
        data = get_json(f"{M_BASE}/api/stock/{code}/trend?pageSize={need}&page=1", retry=2)
        lst = find_list(data) if data is not None else []
        if lst:
            df2 = rows_to_df(lst)
            df2 = df2[df2["close"].notna()]
            if df is None or len(df2) > len(df):
                df = df2
    if (df is None or len(df) < need) and DATA_SOURCE != "naver":
        alt = get_history_krx(code, need)  # 네이버가 부족하면 KRX로 보충
        if alt is not None and (df is None or len(alt) > len(df)):
            df = alt
    if df is None or df.empty:
        return None
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------- KRX (한국거래소) 백업 출처
def krx_import():
    try:
        from pykrx import stock
        return stock
    except Exception as e:
        LAST_INFO["msg"] = f"pykrx 불러오기 실패: {type(e).__name__}: {str(e)[:120]}"
        return None


def krx_ohlcv(stock, d, m):
    """하루치 시장 전체 종가/거래량 (index=종목코드)"""
    for fn in ("get_market_ohlcv_by_ticker", "get_market_ohlcv"):
        f = getattr(stock, fn, None)
        if f is None:
            continue
        try:
            df = f(d, market=m)
            if df is not None and not df.empty and "종가" in df.columns:
                return df
        except Exception as e:
            LAST_INFO["msg"] = f"KRX {fn} 오류 {type(e).__name__}: {str(e)[:120]}"
    return None


def krx_net(stock, d, m):
    """하루치 시장 전체 기관 순매수량 (index=종목코드)"""
    try:
        df = stock.get_market_net_purchases_of_equities(d, d, m, "기관합계")
    except Exception as e:
        LAST_INFO["msg"] = f"KRX 기관 순매수 오류 {type(e).__name__}: {str(e)[:120]}"
        return None
    if df is None or df.empty:
        return None
    col = next((c for c in df.columns if "순매수" in str(c) and "거래량" in str(c)), None)
    if col is None:
        LAST_INFO["msg"] = f"KRX 순매수 항목을 못 찾음: {list(df.columns)[:8]}"
        return None
    return pd.to_numeric(df[col], errors="coerce")


def krx_collect(stock):
    """시장 전체를 날짜별로 받아 종목별 21거래일 DataFrame 으로 정리 -> {코드: df}"""
    today = dt.datetime.now(KST).date()
    start = today - dt.timedelta(days=60)
    ref = stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"),
                                         today.strftime("%Y%m%d"), "005930")
    dates = [d.strftime("%Y%m%d") for d in ref.index]
    need = WINDOW + 1
    if len(dates) < need:
        LAST_INFO["msg"] = LAST_INFO["msg"] or f"KRX 거래일 목록이 부족함 ({len(dates)}일)"
        return None

    markets = ("KOSPI", "KOSDAQ")
    panels = {}  # 날짜 -> DataFrame(index=종목코드, close/vol/inst/val)
    for d in reversed(dates):  # 최신 날짜부터
        frames = []
        for m in markets:
            oh = krx_ohlcv(stock, d, m)
            if oh is None:
                continue
            net = krx_net(stock, d, m)
            f = pd.DataFrame({"close": pd.to_numeric(oh["종가"], errors="coerce"),
                              "vol": pd.to_numeric(oh["거래량"], errors="coerce")})
            f["val"] = (pd.to_numeric(oh["거래대금"], errors="coerce")
                        if "거래대금" in oh.columns else float("nan"))
            f["inst"] = net.reindex(f.index).fillna(0) if net is not None else float("nan")
            f["mkt"] = m
            frames.append(f)
        panel = pd.concat(frames) if frames else None
        valid = panel is not None and not panel.empty and panel["inst"].notna().any()
        if not valid:
            if panels:  # 중간 날짜가 비면 이어 붙일 수 없음
                LAST_INFO["msg"] = LAST_INFO["msg"] or f"KRX {d} 자료가 비어 있음"
                return None
            continue    # 최신 날짜 수급이 아직 없으면 하루 전부터 사용
        panels[d] = panel
        if len(panels) >= need:
            break
    if len(panels) < need:
        LAST_INFO["msg"] = LAST_INFO["msg"] or f"KRX 자료가 부족함 ({len(panels)}일)"
        return None

    ds = sorted(panels)  # 오래된 -> 최신
    latest = ds[-1]
    # 시가총액 상위 종목 선택
    universe = []
    for m in markets:
        codes = None
        try:
            cap = stock.get_market_cap_by_ticker(latest, market=m)
            if cap is not None and not cap.empty and "시가총액" in cap.columns:
                codes = list(cap.sort_values("시가총액", ascending=False).index[:PER_MARKET_COUNT])
                for c_, v_ in cap["시가총액"].items():
                    CAPS_EOK[str(c_)] = float(v_) / 1e8  # 원 -> 억원
        except Exception as e:
            LAST_INFO["msg"] = f"KRX 시가총액 조회 오류 {type(e).__name__}: {str(e)[:100]}"
        if codes is None:  # 시가총액을 못 받으면 거래대금 순으로 대체
            sub = panels[latest]
            sub = sub[sub["mkt"] == m].sort_values("val", ascending=False)
            codes = list(sub.index[:PER_MARKET_COUNT])
        universe += codes

    close_df = pd.DataFrame({d: panels[d]["close"] for d in ds})
    vol_df = pd.DataFrame({d: panels[d]["vol"] for d in ds})
    inst_df = pd.DataFrame({d: panels[d]["inst"] for d in ds})
    out = {}
    for code in dict.fromkeys(universe):
        if code not in close_df.index:
            continue
        out[code] = pd.DataFrame({"date": ds, "close": close_df.loc[code].values,
                                  "vol": vol_df.loc[code].values,
                                  "inst": inst_df.loc[code].values})
    return out


def krx_name(stock, code):
    try:
        n = stock.get_market_ticker_name(code)
        return str(n) if isinstance(n, str) and n else code
    except Exception:
        return code


def get_history_krx(code, need):
    """KRX 종가 이력 (후보 종목 60일선/성과 계산용). 실패 시 None"""
    stock = krx_import()
    if stock is None:
        return None
    try:
        today = dt.datetime.now(KST).date()
        start = today - dt.timedelta(days=int(need * 1.6) + 15)
        df = stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"),
                                            today.strftime("%Y%m%d"), code)
        if df is None or df.empty or "종가" not in df.columns:
            return None
        out = pd.DataFrame({"date": [d.strftime("%Y%m%d") for d in df.index],
                            "close": pd.to_numeric(df["종가"], errors="coerce").values,
                            "vol": pd.to_numeric(df["거래량"], errors="coerce").values
                            if "거래량" in df.columns else float("nan"),
                            "inst": float("nan")})
        out = out[out["close"].notna() & (out["close"] > 0)]
        return out.sort_values("date").reset_index(drop=True)
    except Exception as e:
        LAST_INFO["msg"] = f"KRX 이력 조회 오류 {type(e).__name__}: {str(e)[:100]}"
        return None


# ---------------------------------------------------------------- 조건 판단
def longest_streak(flags):
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def analyze(code, name):
    return analyze_rows(code, name, get_rows(code))


def analyze_rows(code, name, d21):
    """반환: (결과dict 또는 None, 데이터상태 'ok'/'nodata', 기준일)"""
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
    mask = (v20 >= v20.mean() * COND3_MULT).values
    spikes = int(mask.sum())
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

    # 급증일의 가격 방향 (급증일 종가가 전일보다 올랐는지/내렸는지)
    closes21 = d21["close"].values
    up = dn = 0
    for j in range(WINDOW):
        if mask[j]:
            if closes21[j + 1] > closes21[j]:
                up += 1
            elif closes21[j + 1] < closes21[j]:
                dn += 1
    if spikes == 0:
        spike_dir = "없음"
    elif up > dn:
        spike_dir = "매수세 우위"
    elif dn > up:
        spike_dir = "매도세 우위"
    else:
        spike_dir = "중립"

    close = float(d20["close"].iloc[-1])
    box_pos = 50.0 if hi == lo else (close - lo) / (hi - lo) * 100

    return {
        "code": code, "name": name, "hits": hits, "date": last_date,
        "box": (hi - lo) / lo * 100, "box_pos": box_pos,
        "ratio": ratio * 100, "streak": streak, "spikes": spikes,
        "spike_up": up, "spike_dn": dn, "spike_dir": spike_dir,
        "close": int(close),
        "inst_qty": 0 if pd.isna(inst_sum) else inst_sum,
        "inst_amt": 0 if pd.isna(inst_amt) else inst_amt,
        "ma_label": None, "ma_chg": None, "ma_up_days": None,
        "ma_dn_days": None, "ma_strict": "", "above_ma": None,
        "ma_gap": None, "pullback": False,
    }, "ok", last_date


# ---------------------------------------------------------------- 60일선 분석 (후보 종목만)
def enrich_ma(r):
    """60일선 방향(단순: 20일 변화율 / 엄격: 20일 중 상승일 수)과 종가 위치"""
    need = MA_PERIOD + WINDOW  # 80일치
    df = get_history(r["code"], need)
    if df is None or len(df) < MA_PERIOD:
        r["ma_label"] = "부족"
        return
    close = df["close"].reset_index(drop=True)
    ma = close.rolling(MA_PERIOD).mean()
    r["above_ma"] = bool(close.iloc[-1] >= ma.iloc[-1])
    r["ma_gap"] = float((close.iloc[-1] / ma.iloc[-1] - 1) * 100)  # 종가의 60일선 대비 거리(%)
    if len(df) < need:
        r["ma_label"] = "부족"
        return
    chg = (ma.iloc[-1] / ma.iloc[-(WINDOW + 1)] - 1) * 100
    diffs = ma.diff().iloc[-WINDOW:]
    up_days, dn_days = int((diffs > 0).sum()), int((diffs < 0).sum())
    r["ma_chg"], r["ma_up_days"], r["ma_dn_days"] = chg, up_days, dn_days
    if chg >= MA_FLAT_PCT:
        r["ma_label"] = "상승"
    elif chg <= -MA_FLAT_PCT:
        r["ma_label"] = "하락"
    else:
        r["ma_label"] = "횡보"
    if up_days >= MA_STRICT_DAYS:
        r["ma_strict"] = "up"
    elif dn_days >= MA_STRICT_DAYS:
        r["ma_strict"] = "down"


def is_pullback(r):
    """⭐ 눌림목 패턴: 60일선이 꾸준히 오르는 추세에서 종가가 60일선 근처(±3%)까지 내려와 있고,
    박스 하단이며, 거래량 급증일에는 주가가 오른 날이 많았던 종목"""
    gap = r.get("ma_gap")
    return bool(gap is not None
                and abs(gap) <= PULLBACK_MA_GAP
                and r["box_pos"] <= PULLBACK_BOX_MAX
                and r["spike_up"] >= PULLBACK_SPIKE_UP_MIN
                and r["ma_label"] == "상승"
                and r["ma_strict"] == "up")


# ---------------------------------------------------------------- 성과 기록
HIST_COLS = ["base_date", "code", "name", "conds", "n_conds", "close", "box_pos",
             "ma_label", "ma_chg", "ma_strict", "above_ma", "ma_gap", "spike_dir",
             "pullback2", "inst_amt", "r5", "r10", "r20"]
NUM_COLS = ["n_conds", "close", "box_pos", "ma_chg", "ma_gap", "inst_amt", "r5", "r10", "r20"]


def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            df = pd.read_csv(HISTORY_PATH, encoding="utf-8-sig", dtype=str)
        except Exception as e:
            print("성과 기록 읽기 실패:", e)
            df = pd.DataFrame(columns=HIST_COLS)
    else:
        df = pd.DataFrame(columns=HIST_COLS)
    for c in HIST_COLS:
        if c not in df.columns:
            df[c] = ""
    df = df[HIST_COLS].copy()
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("base_date", "code", "name", "conds", "ma_label", "ma_strict",
              "above_ma", "spike_dir", "pullback2"):
        df[c] = df[c].fillna("").astype(str)
    return df


def save_history(df):
    os.makedirs(os.path.dirname(HISTORY_PATH) or ".", exist_ok=True)
    df.to_csv(HISTORY_PATH, index=False, encoding="utf-8-sig")


def record_history(df, results):
    """오늘 알림 종목을 기록 (같은 기준일+종목은 한 번만)"""
    seen = set(zip(df["base_date"], df["code"]))
    rows = []
    for r in results:
        key = (r["date"], r["code"])
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "base_date": r["date"], "code": r["code"], "name": r["name"],
            "conds": ",".join(map(str, r["hits"])), "n_conds": len(r["hits"]),
            "close": r["close"], "box_pos": round(r["box_pos"], 1),
            "ma_label": r["ma_label"] or "", "ma_chg": (round(r["ma_chg"], 2)
                                                        if r["ma_chg"] is not None else None),
            "ma_strict": r["ma_strict"], "above_ma": "" if r["above_ma"] is None
            else ("Y" if r["above_ma"] else "N"),
            "ma_gap": (round(r["ma_gap"], 2) if r.get("ma_gap") is not None else None),
            "spike_dir": r["spike_dir"], "pullback2": "Y" if r.get("pullback") else "N",
            "inst_amt": round(r["inst_amt"], 1),
            "r5": None, "r10": None, "r20": None,
        })
    if rows:
        df = pd.concat([df, pd.DataFrame(rows, columns=HIST_COLS)], ignore_index=True)
        for c in NUM_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def update_returns(df):
    """알림일 종가 대비 5/10/20거래일 뒤 수익률을 채움 (아직 비어 있는 것만)"""
    today = dt.datetime.now(KST).date()
    pending = []
    for idx, row in df.iterrows():
        if not pd.isna(row["r20"]):
            continue
        try:
            age = (today - dt.date.fromisoformat(row["base_date"])).days
        except Exception:
            continue
        if 7 <= age <= 60:
            pending.append(idx)
    if not pending:
        return df
    for code in sorted(set(df.loc[pending, "code"])):
        hist = get_history(code, 70)
        if hist is None:
            continue
        dates, closes = list(hist["date"]), list(hist["close"])
        for idx in [i for i in pending if df.at[i, "code"] == code]:
            bd = df.at[idx, "base_date"].replace("-", "")
            if bd not in dates:
                continue
            i0 = dates.index(bd)
            base = closes[i0]
            if not base or pd.isna(base):
                continue
            for n, col in ((5, "r5"), (10, "r10"), (20, "r20")):
                if pd.isna(df.at[idx, col]) and i0 + n < len(closes):
                    df.at[idx, col] = round((closes[i0 + n] / base - 1) * 100, 2)
    return df


def stats_line(label, sub):
    parts = []
    for col, nm in (("r5", "5일"), ("r10", "10일"), ("r20", "20일")):
        s = sub[col].dropna()
        if len(s) < MIN_SAMPLE:
            parts.append(f"{nm} 표본부족({len(s)})")
        else:
            parts.append(f"{nm} {s.mean():+.1f}% (승률 {(s > 0).mean() * 100:.0f}%, n={len(s)})")
    return f"<b>{esc(label)}</b>\n   " + " | ".join(parts)


def build_performance_message(df):
    ev = df[df["r5"].notna()]
    if ev.empty:
        return None

    def has_cond(k):
        return df["conds"].apply(lambda s: str(k) in str(s).split(","))

    g = df["ma_gap"]  # 종가의 60일선 대비 거리(%)
    groups = [
        ("전체 알림 종목", df),
        ("조건1", df[has_cond(1)]), ("조건2", df[has_cond(2)]), ("조건3", df[has_cond(3)]),
        ("🔴 3개 조건 충족", df[df["n_conds"] >= 3]),
        ("🟠 2개 조건 충족", df[df["n_conds"] == 2]),
        ("60일선 상승", df[df["ma_label"] == "상승"]),
        ("60일선 횡보", df[df["ma_label"] == "횡보"]),
        ("60일선 하락", df[df["ma_label"] == "하락"]),
        ("60일선 엄격 상승", df[df["ma_strict"] == "up"]),
        ("급증일 매수세 우위", df[df["spike_dir"] == "매수세 우위"]),
        ("급증일 매도세 우위", df[df["spike_dir"] == "매도세 우위"]),
        ("박스 상단(67%↑)", df[df["box_pos"] >= 67]),
        ("박스 하단(33%↓)", df[df["box_pos"] <= 33]),
        ("⭐ 눌림목 패턴 (60일선 ±3%)", df[df["pullback2"] == "Y"]),
        ("눌림목 아닌 종목 (비교용)", df[df["pullback2"] == "N"]),
        ("종가 60일선 대비 -10% 이하", df[g <= -10]),
        ("종가 60일선 대비 -10~-3%", df[(g > -10) & (g <= -3)]),
        ("종가 60일선 대비 -3~0%", df[(g > -3) & (g <= 0)]),
        ("종가 60일선 대비 0~+3%", df[(g > 0) & (g <= 3)]),
        ("종가 60일선 대비 +3~+10%", df[(g > 3) & (g <= 10)]),
        ("종가 60일선 대비 +10% 초과", df[g > 10]),
    ]
    lines = ["📈 <b>성과 기록</b> | 과거 알림 종목의 이후 수익률",
             f"누적 기록 {len(df)}건 / 5일 경과 {len(ev)}건",
             "알림 기준일 종가 대비 · 수수료/세금 미반영",
             "━━━━━━━━━━━━━━"]
    for label, sub in groups:
        lines.append(stats_line(label, sub))
    lines.append("")
    lines.append("※ 표본이 적으면 우연일 수 있습니다. 참고용이며 매수 추천이 아닙니다.")
    return "\n".join(lines)


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
    2: ("🟡", "조건2", f"거래량이 전일 대비 끊기지 않고 연속 {COND2_STREAK}일 이상 증가"),
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


def box_label(pos):
    return "하단" if pos < 33 else ("중단" if pos < 67 else "상단")


def detail_line(k, r):
    if k == 1:
        return f"📈 거래량 후반/전반 <b>{r['ratio']:+.0f}%</b>"
    if k == 2:
        return f"📈 거래량 연속 증가 <b>{r['streak']}일</b>"
    return (f"📈 평균 {COND3_MULT:g}배↑ 급증일 <b>{r['spikes']}회</b> "
            f"(▲{r['spike_up']} ▼{r['spike_dn']} → <b>{r['spike_dir']}</b>)")


def spike_line(r):
    if r["spikes"] < 1:
        return ""
    return (f"📊 급증일 {r['spikes']}회: ▲{r['spike_up']} ▼{r['spike_dn']} "
            f"→ <b>{r['spike_dir']}</b>\n")


def ma_line(r):
    lab = r.get("ma_label")
    pos = ""
    if r.get("ma_gap") is not None:
        pos = f" · 종가 60일선 대비 <b>{r['ma_gap']:+.1f}%</b>"
    elif r.get("above_ma") is not None:
        pos = " · 종가 60일선 " + ("위" if r["above_ma"] else "아래")
    if lab in (None, "부족"):
        return f"📈 60일선 방향 판정 불가(데이터 부족){pos}\n"
    icon = {"상승": "📈", "횡보": "➖", "하락": "📉"}[lab]
    s = f"{icon} 60일선 <b>{lab}</b> ({r['ma_chg']:+.1f}%) · 20일 중 {r['ma_up_days']}일 상승"
    if r["ma_strict"] == "up":
        s += " ✅엄격 상승"
    elif r["ma_strict"] == "down":
        s += " ⚠엄격 하락"
    return s + pos + "\n"


def send_condition_messages(results, total, nodata, base_date, source_label=""):
    now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    for k in (1, 2, 3):
        icon, title, desc = COND_INFO[k]
        items = [r for r in results if k in r["hits"]]
        items.sort(key=lambda r: r["inst_amt"], reverse=True)  # 기관 순매수 큰 순
        shown = items[:MAX_PER_CONDITION]
        n3 = sum(1 for r in items if len(r["hits"]) >= 3)
        n2 = sum(1 for r in items if len(r["hits"]) == 2)
        npb = sum(1 for r in items if r.get("pullback"))
        tier_txt = ""
        if n3 or n2 or npb:
            parts = ([f"🔴 3개충족 {n3}"] if n3 else []) + ([f"🟠 2개충족 {n2}"] if n2 else []) \
                + ([f"⭐ 눌림목 {npb}"] if npb else [])
            tier_txt = " (" + " · ".join(parts) + ")"

        header = (f"{icon} <b>{title}</b> | {esc(desc)}\n"
                  f"🕘 {now}  |  📅 기준일 {esc(base_date)}\n"
                  f"🗂 출처: {esc(source_label)}\n"
                  f"{fresh_line()}"
                  f"🔎 검사 {total}종목 → <b>해당 {len(items)}종목</b>"
                  f"{tier_txt}\n"
                  f"↕ 기관 순매수 큰 순 정렬\n"
                  f"━━━━━━━━━━━━━━")
        blocks = []
        for n, r in enumerate(shown, 1):
            icon_t, label_t = tier(r)
            mark = (f"{icon_t} " if icon_t else "") + ("⭐ " if r.get("pullback") else "")
            pb_line = (f"⭐ <b>눌림목 패턴</b> (박스 하단 · 급증일 ▲{r['spike_up']}일 · "
                       f"60일선 엄격 상승 · 종가 60일선 대비 {r['ma_gap']:+.1f}%)\n" if r.get("pullback") else "")
            strong_line = (f"{icon_t} <b>{label_t}: 조건{'·'.join(map(str, r['hits']))}</b>\n"
                           if icon_t else "")
            blocks.append(
                f"{mark}<b>{n}. {esc(r['name'])}</b> ({esc(r['code'])})\n"
                f"{strong_line}"
                f"{pb_line}"
                f"💰 {r['close']:,}원  ·  📦 박스폭 {r['box']:.1f}%\n"
                f"🧭 박스 위치 {r['box_pos']:.0f}% ({box_label(r['box_pos'])})\n"
                f"{detail_line(k, r)}\n"
                f"{spike_line(r) if k != 3 else ''}"
                f"{ma_line(r)}"
                f"🏦 기관 20일 순매수 <b>{r['inst_amt']:+,.0f}억</b> ({r['inst_qty']:+,.0f}주)\n"
                f"──────────────")
        if not blocks:
            blocks.append("해당 종목이 없습니다.")
        footer = []
        if len(items) > len(shown):
            footer.append(f"… 외 {len(items) - len(shown)}종목 (상위 {len(shown)}개만 표시)")
        if k == 3:
            footer.append("🔴 = 조건 3개 모두 충족 / 🟠 = 조건 2개 충족 (규칙 기반 신호)\n"
                          f"⭐ = 눌림목 패턴: 박스 하단({PULLBACK_BOX_MAX}%↓) + 급증일 ▲{PULLBACK_SPIKE_UP_MIN}일↑ "
                          f"+ 60일선 엄격 상승 + 종가 60일선 ±{PULLBACK_MA_GAP:g}% 이내\n"
                          "🧭 박스 위치: 0%=박스 하단, 100%=박스 상단 (종가 기준)\n"
                          "📊 급증일 방향: 거래량 급증일 종가가 전일보다 올랐으면 ▲, 내렸으면 ▼\n"
                          f"📈 60일선: 20일 변화율 ±{MA_FLAT_PCT:g}% 기준 / "
                          f"엄격=20일 중 {MA_STRICT_DAYS}일 이상 같은 방향")
            if nodata:
                footer.append(f"⚠ 자료 미흡/수집 실패 제외: {nodata}개")
            footer.append("※ ETF·레버리지·스팩·리츠 제외 / 거래대금 미제공(거래량 기준)\n"
                          "※ 투자 판단과 책임은 본인에게 있습니다.")
        if COND_FORMAT in ("image", "both") and shown:
            caption_head = header.replace("\n━━━━━━━━━━━━━━", "")
            if send_condition_images(k, shown, len(items), total, nodata, base_date,
                                     source_label, caption_head):
                if COND_FORMAT == "image":
                    continue
            else:
                print(f"조건{k} 표 이미지를 만들거나 보내지 못해 글자 메시지로 대체합니다.")
        send_blocks(header, blocks, "\n".join(footer))


# ---------------------------------------------------------------- 조건 1·2·3 표 이미지
def wrap_px(draw, text, font, max_w):
    """픽셀 너비 기준 줄바꿈"""
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def draw_star(d, cx, cy, r, fill):
    import math
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(pts, fill=fill)


def render_condition_image(k, chunk, start_rank, n_items, n_hidden, total, nodata, base_date, source_label):
    """조건 k 의 종목 표(PNG 바이트). 글꼴/Pillow 가 없으면 None"""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        print("Pillow 불러오기 실패:", e)
        return None
    f_title, f_sub = load_font(30, True), load_font(19)
    f_head, f_body, f_bold = load_font(16), load_font(20), load_font(20, True)
    f_small, f_tag = load_font(15), load_font(14, True)
    if not all([f_title, f_sub, f_head, f_body, f_bold, f_small, f_tag]):
        print("한글 글꼴을 찾지 못했습니다.")
        return None

    RED, BLUE, INK, GRAY = (217, 58, 58), (47, 111, 214), (29, 29, 31), (138, 143, 152)
    ORANGE, GOLD = (245, 140, 30), (232, 176, 20)
    LINE, HEAD_BG, PINK, YEL = (232, 234, 238), (246, 247, 249), (253, 238, 238), (255, 249, 226)
    W, ROW, TOP_H, HEAD_H = 1240, 46, 136, 70
    notes = [
        "박스 위치: 0%=박스 하단, 100%=박스 상단 (20일 종가 기준) / 급증일 방향: ▲ 급증일 종가가 전일보다 상승, ▼ 하락 / "
        f"60일선: 20일 변화율 ±{MA_FLAT_PCT:g}% 기준, 엄격=20일 중 {MA_STRICT_DAYS}일 이상 같은 방향 / 종가 vs 60일선: 종가가 60일선보다 몇 % 위(+)·아래(-)인지",
        "기관 20일 = 일별 기관 순매수 수량 × 종가의 20일 합계(추정, 억원) / ETF·레버리지·스팩·리츠 제외 / 거래대금 미제공(거래량 기준)",
        "규칙으로 계산한 신호이며 매수 추천이 아닙니다. 투자 판단과 책임은 본인에게 있습니다."]
    extra = []
    if n_hidden > 0:
        extra.append(f"외 {n_hidden}종목은 표시하지 않았습니다 (상위 {MAX_PER_CONDITION}개까지 표시)")
    if nodata:
        extra.append(f"자료 미흡/수집 실패로 제외된 종목 {nodata}개")
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    foot_lines = []
    for t in notes + extra:
        foot_lines += wrap_px(tmp, t, f_small, W - 48)
    H = TOP_H + HEAD_H + ROW * len(chunk) + 52 + 24 * len(foot_lines) + 16
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    def tw(txt, font):
        return d.textlength(txt, font=font)

    def put(x, y, txt, font, color, align="left"):
        w = tw(txt, font)
        x0 = x if align == "left" else (x - w if align == "right" else x - w / 2)
        d.text((x0, y), txt, font=font, fill=color, anchor="lm")

    icon, title, desc = COND_INFO[k]
    # 제목
    d.rounded_rectangle((24, 22, 62, 60), radius=8, fill=(228, 236, 255))
    put(43, 41, str(k), f_bold, BLUE, "center")
    put(76, 41, f"조건{k}", f_title, INK)
    put(76 + tw(f"조건{k}", f_title) + 14, 45, desc, f_sub, GRAY)
    meta = (f"기준일 {base_date}  ·  출처 {source_label}  ·  검사 {total}종목 → 해당 {n_items}종목  ·  기관 순매수 큰 순")
    put(24, 84, meta, f_small, GRAY)
    if FRESH and FRESH.get("lag", 0) > 0:
        put(24, 110, f"※ 최신 수급 미반영: 예상 기준일 {FRESH['expected']}, 실제 {FRESH['base']} ({FRESH['lag']}거래일 전 자료)", f_small, RED)
    elif FRESH:
        put(24, 110, "최신 자료 (예상 기준일과 일치)", f_small, GRAY)

    # 열 위치
    C_RANK, X_NAME, C_SIG = 34, 62, 266
    R_PRICE, R_INST = 372, 484
    R_METRIC, R_BOXW, R_BOXP = 626, 724, 836
    C_SPIKE, C_MA, R_GAP = 936, 1076, 1218
    metric_head = {1: ("거래량 증가율", "(후반 vs 전반)"), 2: ("거래량 연속", "증가 일수"),
                   3: ("급증일 수", f"(평균 {COND3_MULT:g}배↑)")}[k]
    y0 = TOP_H
    d.rectangle((0, y0, W, y0 + HEAD_H), fill=HEAD_BG)
    d.rectangle((384, y0, R_INST + 22, y0 + HEAD_H), fill=PINK)
    hy = y0 + HEAD_H // 2

    def head2(x, a, b, align, color=GRAY):
        put(x, hy - 12, a, f_head, color, align)
        put(x, hy + 12, b, f_head, color, align)

    put(C_RANK, hy, "#", f_head, GRAY, "center")
    put(X_NAME, hy, "종목명", f_head, GRAY)
    put(C_SIG, hy, "신호", f_head, GRAY, "center")
    head2(R_PRICE, "현재가", "(원)", "right")
    head2(R_INST, "기관 20일", "(억)", "right", RED)
    head2(R_METRIC, metric_head[0], metric_head[1], "right")
    head2(R_BOXW, "박스폭", "(20일 종가)", "right")
    head2(R_BOXP, "박스 위치", "(0%=하단)", "right")
    head2(C_SPIKE, "급증일 방향", "(▲오름 ▼내림)", "center")
    head2(C_MA, "60일선", "(20일 변화·엄격)", "center")
    head2(R_GAP, "종가 vs", "60일선", "right")

    for i, r in enumerate(chunk):
        top = y0 + HEAD_H + i * ROW
        cy = top + ROW // 2
        if r.get("pullback"):
            d.rectangle((0, top, W, top + ROW), fill=YEL)
        d.rectangle((384, top, R_INST + 22, top + ROW), fill=PINK)
        d.line((0, top + ROW, W, top + ROW), fill=LINE, width=1)

        put(C_RANK, cy, str(start_rank + i), f_body, GRAY, "center")
        nm = r["name"]
        while tw(nm, f_bold) > 188 and len(nm) > 2:
            nm = nm[:-1]
        if nm != r["name"]:
            nm = nm[:-1] + "…"
        put(X_NAME, cy, nm, f_bold, INK)

        # 신호: 조건 충족 개수(빨강=3개, 주황=2개) + 눌림목(별)
        marks = []
        if len(r["hits"]) >= 3:
            marks.append(("dot", RED))
        elif len(r["hits"]) == 2:
            marks.append(("dot", ORANGE))
        if r.get("pullback"):
            marks.append(("star", GOLD))
        for j, (kind, col) in enumerate(marks):
            cx = C_SIG + (j - (len(marks) - 1) / 2) * 30
            if kind == "dot":
                d.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=col)
            else:
                draw_star(d, cx, cy, 11, col)
        if not marks:
            put(C_SIG, cy, "·", f_body, LINE, "center")

        put(R_PRICE, cy, f"{r['close']:,}", f_body, INK, "right")
        put(R_INST, cy, f"{r['inst_amt']:+,.0f}", f_bold, INK, "right")
        if k == 1:
            put(R_METRIC, cy, f"{r['ratio']:+.0f}%", f_bold, RED if r["ratio"] > 0 else INK, "right")
        elif k == 2:
            put(R_METRIC, cy, f"{r['streak']}일", f_bold, INK, "right")
        else:
            put(R_METRIC, cy, f"{r['spikes']}회", f_bold, INK, "right")
        put(R_BOXW, cy, f"{r['box']:.1f}%", f_body, INK, "right")
        put(R_BOXP, cy, f"{r['box_pos']:.0f}% {box_label(r['box_pos'])}", f_body, INK, "right")

        if r["spikes"] >= 1:
            col = RED if r["spike_dir"] == "매수세 우위" else (BLUE if r["spike_dir"] == "매도세 우위" else INK)
            put(C_SPIKE, cy, f"▲{r['spike_up']} ▼{r['spike_dn']}", f_body, col, "center")
        else:
            put(C_SPIKE, cy, "–", f_body, GRAY, "center")

        lab = r.get("ma_label")
        if lab in ("상승", "횡보", "하락"):
            col = RED if lab == "상승" else (BLUE if lab == "하락" else GRAY)
            main = f"{lab} {r['ma_chg']:+.1f}%"
            tag = {"up": "엄격", "down": "엄격"}.get(r.get("ma_strict"), "")
            total_w = tw(main, f_body) + ((tw(tag, f_tag) + 18) if tag else 0)
            x = C_MA - total_w / 2
            put(x, cy, main, f_body, col)
            if tag:
                tx = x + tw(main, f_body) + 8
                d.rounded_rectangle((tx, cy - 11, tx + tw(tag, f_tag) + 10, cy + 11), radius=5, outline=col, width=2)
                put(tx + 5, cy, tag, f_tag, col)
        else:
            put(C_MA, cy, "자료 부족", f_body, GRAY, "center")

        gap = r.get("ma_gap")
        if gap is None:
            put(R_GAP, cy, "–", f_body, GRAY, "right")
        else:
            near = abs(gap) <= PULLBACK_MA_GAP
            put(R_GAP, cy, f"{gap:+.1f}%", f_bold if near else f_body,
                RED if gap > 0 else (BLUE if gap < 0 else INK), "right")

    # 범례 + 안내
    ly = y0 + HEAD_H + ROW * len(chunk) + 26
    x = 24
    d.ellipse((x, ly - 8, x + 16, ly + 8), fill=RED)
    put(x + 22, ly, "조건 3개 모두 충족", f_small, INK)
    x += 22 + tw("조건 3개 모두 충족", f_small) + 22
    d.ellipse((x, ly - 8, x + 16, ly + 8), fill=ORANGE)
    put(x + 22, ly, "조건 2개 충족", f_small, INK)
    x += 22 + tw("조건 2개 충족", f_small) + 22
    draw_star(d, x + 8, ly, 10, GOLD)
    lg = (f"눌림목 패턴 (노란 줄): 박스 하단({PULLBACK_BOX_MAX}%↓) · 급증일 ▲{PULLBACK_SPIKE_UP_MIN}일↑ · "
          f"60일선 엄격 상승 · 종가 60일선 ±{PULLBACK_MA_GAP:g}% 이내")
    put(x + 24, ly, lg, f_small, INK)
    fy = ly + 28
    for j, line in enumerate(foot_lines):
        put(24, fy + 24 * j, line, f_small, GRAY)
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def send_condition_images(k, shown, n_items, total, nodata, base_date, source_label, caption_head):
    """조건 k 표 이미지 전송 (20종목씩 나눔). 하나라도 실패하면 False"""
    parts = [shown[i:i + 20] for i in range(0, len(shown), 20)]
    for pi, chunk in enumerate(parts):
        png = render_condition_image(k, chunk, pi * 20 + 1, n_items, max(0, n_items - len(shown)),
                                     total, nodata, base_date, source_label)
        if png is None:
            return False
        caption = caption_head + (f"\n({pi + 1}/{len(parts)})" if len(parts) > 1 else "")
        if not tg_send_photo(png, caption[:1000]):
            return False
    return True


# ---------------------------------------------------------------- TOP 40 표 이미지
FONT_REG = ["/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "C:/Windows/Fonts/malgun.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc"]
FONT_BOLD = ["/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
             "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
             "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
             "C:/Windows/Fonts/malgunbd.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc"]


def load_font(size, bold=False):
    from PIL import ImageFont
    for path in (FONT_BOLD if bold else FONT_REG):
        if not os.path.exists(path):
            continue
        for idx in ((1, 0, 2) if path.endswith(".ttc") else (0,)):  # .ttc 는 한국어(KR) 서체 우선
            try:
                return ImageFont.truetype(path, size, index=idx)
            except Exception:
                continue
    return None


def render_top40_image(chunk, start_rank, caps, base_date, source_label):
    """표 이미지(PNG 바이트) 생성. 글꼴/Pillow 가 없으면 None"""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        print("Pillow 불러오기 실패:", e)
        return None
    f_title, f_sub = load_font(30, True), load_font(19)
    f_head, f_body, f_bold = load_font(17), load_font(21), load_font(21, True)
    f_small = load_font(16)
    if not all([f_title, f_sub, f_head, f_body, f_bold, f_small]):
        print("한글 글꼴을 찾지 못했습니다.")
        return None

    RED, BLUE, INK, GRAY = (217, 58, 58), (47, 111, 214), (29, 29, 31), (138, 143, 152)
    LINE, HEAD_BG, PINK = (232, 234, 238), (246, 247, 249), (253, 238, 238)
    W, ROW, TOP_H, HEAD_H = 1000, 46, 136, 70
    foot = [f"금액 = 기관 일별 순매수 수량 × 종가의 {TOP_DAYS}일 합계(추정) / 시총 대비 = {TOP_DAYS}일 순매수 금액 ÷ 시가총액"
            + ("" if caps else " (시총 자료 없음 → –)"),
            "확정 데이터 기준(장중 추정 아님) / ETF·레버리지·스팩·리츠 제외 / 투자 판단과 책임은 본인에게 있습니다"]
    H = TOP_H + HEAD_H + ROW * len(chunk) + 20 + 26 * len(foot) + 14
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    def tw(txt, font):
        return d.textlength(txt, font=font)

    def put(x, y, txt, font, color, align="left"):
        w = tw(txt, font)
        x0 = x if align == "left" else (x - w if align == "right" else x - w / 2)
        d.text((x0, y), txt, font=font, fill=color, anchor="lm")

    # 제목
    d.rounded_rectangle((24, 22, 62, 60), radius=8, fill=(228, 236, 255))
    put(43, 41, "M", f_bold, BLUE, "center")
    put(76, 41, f"기관 순매수 금액 TOP {TOP_N}", f_title, INK)
    rng = f"{start_rank}~{start_rank + len(chunk) - 1}위"
    put(76 + tw(f"기관 순매수 금액 TOP {TOP_N}", f_title) + 12, 45, f"{rng} · 최근 {TOP_DAYS}거래일 기관이 가장 많이 사들인 종목", f_sub, GRAY)
    meta = f"기준일 {base_date}  ·  출처 {source_label}"
    put(24, 84, meta, f_small, GRAY)
    if FRESH and FRESH.get("lag", 0) > 0:
        put(24, 110, f"※ 최신 수급 미반영: 예상 기준일 {FRESH['expected']}, 실제 {FRESH['base']} ({FRESH['lag']}거래일 전 자료)", f_small, RED)
    elif FRESH:
        put(24, 110, "최신 자료 (예상 기준일과 일치)", f_small, GRAY)

    # 열 위치
    X_RANK, X_NAME = 40, 78
    R_AMT, R_PCT, C_STREAK, R_DAY, R_CHG = 432, 590, 716, 846, 966
    y0 = TOP_H
    d.rectangle((0, y0, W, y0 + HEAD_H), fill=HEAD_BG)
    d.rectangle((258, y0, R_AMT + 22, y0 + HEAD_H), fill=PINK)
    hy = y0 + HEAD_H // 2
    put(X_RANK, hy, "#", f_head, GRAY, "center")
    put(X_NAME, hy, "종목명", f_head, GRAY)
    put(R_AMT, hy - 12, f"기관 {TOP_DAYS}일", f_head, RED, "right")
    put(R_AMT, hy + 12, "(억)", f_head, RED, "right")
    put(R_PCT, hy - 12, "시총 대비", f_head, GRAY, "right")
    put(R_PCT, hy + 12, "기관 순매수 %", f_head, GRAY, "right")
    put(C_STREAK, hy - 12, "기관 연속", f_head, GRAY, "center")
    put(C_STREAK, hy + 12, "순매수", f_head, GRAY, "center")
    put(R_DAY, hy, "등락률", f_head, GRAY, "right")
    put(R_CHG, hy, f"{TOP_DAYS}거래일", f_head, GRAY, "right")

    def chg_color(v):
        return RED if v > 0 else (BLUE if v < 0 else INK)

    for i, (code, name, m) in enumerate(chunk):
        top = y0 + HEAD_H + i * ROW
        cy = top + ROW // 2
        d.rectangle((258, top, R_AMT + 22, top + ROW), fill=PINK)
        d.line((0, top + ROW, W, top + ROW), fill=LINE, width=1)
        nm = name
        while tw(nm, f_bold) > 190 and len(nm) > 2:
            nm = nm[:-1]
        if nm != name:
            nm = nm[:-1] + "…"
        put(X_RANK, cy, str(start_rank + i), f_body, GRAY, "center")
        put(X_NAME, cy, nm, f_bold, INK)
        put(R_AMT, cy, f"{m['amt']:+,.0f}", f_bold, INK, "right")
        cap = caps.get(code)
        if cap:
            pct = m["amt"] / cap * 100
            put(R_PCT, cy, f"{pct:.2f}%", f_bold if pct >= 1.0 else f_body, RED if pct >= 1.0 else INK, "right")
        else:
            put(R_PCT, cy, "–", f_body, GRAY, "right")
        plus = "+" if m["streak"] >= m["all_days"] else ""
        if m["sign"] > 0 and m["streak"]:
            put(C_STREAK, cy, f"{m['streak']}{plus}일 매수", f_body, RED, "center")
        elif m["sign"] < 0 and m["streak"]:
            put(C_STREAK, cy, f"{m['streak']}{plus}일 매도", f_body, BLUE, "center")
        else:
            put(C_STREAK, cy, "보합", f_body, GRAY, "center")
        put(R_DAY, cy, f"{m['day']:+.1f}%", f_body, chg_color(m["day"]), "right")
        put(R_CHG, cy, f"{m['chg5']:+.1f}%", f_body, chg_color(m["chg5"]), "right")

    fy = y0 + HEAD_H + ROW * len(chunk) + 24
    for k, line in enumerate(foot):
        put(24, fy + 26 * k, line, f_small, GRAY)
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def tg_send_photo(png, caption):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[오류] 텔레그램 토큰/채팅ID(Secrets)가 설정되지 않았습니다. (이미지 %d바이트)" % len(png))
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                          data={"chat_id": TG_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                          files={"photo": ("top40.png", png, "image/png")}, timeout=30)
        ok = r.status_code == 200
        print("텔레그램 사진 전송", "성공" if ok else f"실패: {r.status_code} {r.text[:150]}")
    except Exception as e:
        print("텔레그램 사진 전송 실패:", e)
        ok = False
    time.sleep(1)
    return ok


def send_top40_images(picked, caps, base_date, source_label):
    """표 이미지 2장(1~20위, 21~40위) 전송. 하나라도 만들지 못하면 False"""
    now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    sent = 0
    for a, b in ((0, 20), (20, 40)):
        chunk = picked[a:b]
        if not chunk:
            continue
        png = render_top40_image(chunk, a + 1, caps, base_date, source_label)
        if png is None:
            return False
        caption = (f"🏦 <b>기관 순매수 금액 TOP {TOP_N}</b> ({a + 1}~{a + len(chunk)}위)\n"
                   f"🕘 {now}  |  📅 기준일 {esc(base_date)}\n"
                   f"🗂 출처: {esc(source_label)}\n"
                   f"{fresh_line()}")
        if not tg_send_photo(png, caption):
            return False
        sent += 1
    return sent > 0


# ---------------------------------------------------------------- 기관 5일 순매수 TOP 40 (별도 현황판)
def caps_in_eok():
    """종목별 시가총액(억원). 단위를 확신할 수 없으면 빈 값 -> 시총 대비 %는 '–'로 표시"""
    caps = dict(CAPS_EOK)
    if not caps:
        vals = [v for v in RAW_CAPS.values() if v == v and v > 0]
        if not vals:
            return {}
        mx = max(vals)
        div = 1e8 if mx >= 1e12 else (100 if mx >= 5e7 else 1)  # 원 / 백만원 / 억원 추정
        caps = {c: v / div for c, v in RAW_CAPS.items() if v == v and v > 0}
    if not caps:
        return {}
    mx = max(caps.values())
    if not (1e6 <= mx <= 1e8):  # 1위 시총이 100조~1경 원이 아니면 단위 오판으로 보고 사용 안 함
        return {}
    return caps


def top_metrics(d21):
    """한 종목의 5일 기관 순매수 금액·연속 일수·등락률. 자료가 부족하면 None"""
    d = d21.reset_index(drop=True)
    if len(d) < TOP_DAYS + 1 or d["close"].isna().tail(TOP_DAYS + 1).any():
        return None
    inst5 = d["inst"].tail(TOP_DAYS)
    if inst5.isna().any():
        return None
    close = d["close"]
    amt = float((inst5 * close.tail(TOP_DAYS)).sum() / 1e8)  # 억원 (순매수 수량 x 종가, 추정)

    # 기관 연속 순매수/순매도 일수 (가장 최근 날부터 같은 방향이 이어진 일수)
    vals = [x for x in d["inst"].tolist()][::-1]
    sign = 1 if vals[0] > 0 else (-1 if vals[0] < 0 else 0)
    streak = 0
    if sign != 0:
        for x in vals:
            if x == x and (x > 0) == (sign > 0) and x != 0:
                streak += 1
            else:
                break
    return {
        "amt": amt, "sign": sign, "streak": streak, "all_days": len(vals),
        "day": (close.iloc[-1] / close.iloc[-2] - 1) * 100,
        "chg5": (close.iloc[-1] / close.iloc[-(TOP_DAYS + 1)] - 1) * 100,
    }


def streak_text(m):
    if m["sign"] == 0 or m["streak"] == 0:
        return "⚪ 보합"
    plus = "+" if m["streak"] >= m["all_days"] else ""
    return (f"🔴 {m['streak']}{plus}일 매수" if m["sign"] > 0
            else f"🔵 {m['streak']}{plus}일 매도")


def send_top40_messages(top_rows, base_date, source_label, krx_stock=None):
    rows = []
    for code, (name, d21) in top_rows.items():
        m = top_metrics(d21)
        if m is None or m["amt"] <= 0:
            continue
        rows.append((code, name, m))
    if not rows:
        print("TOP 40: 계산 가능한 종목이 없습니다.")
        return
    rows.sort(key=lambda x: x[2]["amt"], reverse=True)

    caps = caps_in_eok()
    picked = []
    for code, name, m in rows:
        if krx_stock is not None and name == code:      # KRX 경로는 상위 종목만 이름 조회
            name = krx_name(krx_stock, code)
        if is_excluded(name):
            continue
        picked.append((code, name, m))
        if len(picked) >= TOP_N:
            break

    if TOP_FORMAT in ("image", "both"):
        if send_top40_images(picked, caps, base_date, source_label):
            if TOP_FORMAT == "image":
                return
        else:
            print("TOP 40 표 이미지를 만들거나 보내지 못해 글자 메시지로 대체합니다.")

    now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    for part, (a, b) in enumerate(((0, 20), (20, 40))):
        chunk = picked[a:b]
        if not chunk:
            continue
        header = (f"🏦 <b>기관 순매수 금액 TOP {TOP_N}</b> ({a + 1}~{a + len(chunk)}위)\n"
                  f"최근 {TOP_DAYS}거래일 기관이 가장 많이 사들인 종목\n"
                  f"🕘 {now}  |  📅 기준일 {esc(base_date)}\n"
                  f"🗂 출처: {esc(source_label)}\n"
                  f"{fresh_line()}"
                  f"━━━━━━━━━━━━━━")
        blocks = []
        for i, (code, name, m) in enumerate(chunk, a + 1):
            cap = caps.get(code)
            pct = f"{m['amt'] / cap * 100:.2f}%" if cap else "–"
            blocks.append(
                f"<b>{i}. {esc(name)}</b> ({esc(code)})\n"
                f"🏦 {TOP_DAYS}일 순매수 <b>{m['amt']:+,.0f}억</b> · 시총 대비 {pct}\n"
                f"{streak_text(m)} · 당일 {m['day']:+.1f}% · {TOP_DAYS}일 {m['chg5']:+.1f}%\n"
                f"──────────────")
        footer = ""
        if part == 1 or len(picked) <= 20:
            lines = [f"※ 금액 = 기관 일별 순매수 수량 × 종가의 {TOP_DAYS}일 합계(추정)",
                     f"※ 시총 대비 = {TOP_DAYS}일 순매수 금액 ÷ 시가총액"
                     + ("" if caps else " (시가총액 자료를 못 받아 '–' 표시)"),
                     "※ 확정 데이터 기준이며 장중 추정치는 아닙니다 / 검사 대상은 시가총액 상위 종목",
                     "※ ETF·레버리지·스팩·리츠 제외 / 위 조건 스크리너와는 별개의 현황판입니다",
                     "※ 투자 판단과 책임은 본인에게 있습니다."]
            footer = "\n".join(lines)
        send_blocks(header, blocks, footer)


# ---------------------------------------------------------------- 자료 신선도 (기준일 지연 점검)
def prev_trading_day(d):
    """d 보다 이전의 가장 가까운 거래일"""
    d = d - dt.timedelta(days=1)
    while is_market_closed(d):
        d -= dt.timedelta(days=1)
    return d


def expected_base_date(now):
    """지금 시각에 '있어야 정상'인 기준일: 거래일 16시 이후면 오늘, 그 외에는 직전 거래일"""
    today = now.date()
    if not is_market_closed(today) and now.hour >= 16:
        return today
    return prev_trading_day(today)


def trading_lag(base, expected):
    """base 가 expected 보다 몇 거래일 늦은지 (같거나 최신이면 0)"""
    n, d = 0, base
    while d < expected:
        d += dt.timedelta(days=1)
        if not is_market_closed(d):
            n += 1
    return n


def compute_fresh(now, base_date):
    try:
        base = dt.date.fromisoformat(str(base_date))
    except Exception:
        return {}
    exp = expected_base_date(now)
    return {"expected": exp.isoformat(), "base": base.isoformat(), "lag": trading_lag(base, exp)}


def fresh_line():
    if not FRESH:
        return ""
    if FRESH["lag"] > 0:
        return (f"⚠ <b>최신 수급 미반영</b>: 예상 기준일 {FRESH['expected']}, "
                f"실제 {FRESH['base']} ({FRESH['lag']}거래일 전 자료)\n")
    return "✅ 최신 자료 (예상 기준일과 일치)\n"


FRESH_COLS = ["run_at", "source", "expected_base", "actual_base", "lag_days",
              "stocks_at_base_pct", "total", "nodata", "candidates", "calc_min", "status"]


def record_freshness(now, source, base_dates, total, nodata, candidates, status, t0):
    """실행마다 한 줄 기록 -> 몇 주 쌓이면 어느 시각에 최신 자료가 나오는지 확인 가능"""
    try:
        os.makedirs(os.path.dirname(FRESH_PATH) or ".", exist_ok=True)
        top = max(base_dates) if base_dates else ""
        pct = round(base_dates.count(top) / len(base_dates) * 100, 1) if base_dates else ""
        row = {
            "run_at": now.strftime("%Y-%m-%d %H:%M"), "source": source,
            "expected_base": FRESH.get("expected", expected_base_date(now).isoformat()),
            "actual_base": top, "lag_days": FRESH.get("lag", ""),
            "stocks_at_base_pct": pct, "total": total, "nodata": nodata,
            "candidates": candidates, "calc_min": round((time.time() - t0) / 60, 1),
            "status": status,
        }
        new_file = not os.path.exists(FRESH_PATH)
        pd.DataFrame([row], columns=FRESH_COLS).to_csv(
            FRESH_PATH, mode="a", header=new_file, index=False, encoding="utf-8-sig")
    except Exception as e:
        print("신선도 기록 오류:", type(e).__name__, e)


# ---------------------------------------------------------------- 실행
SEND_MORNING = (7, 50)   # 오전 전송 시각 (한국시간)
SEND_EVENING = (18, 50)  # 오후 전송 시각 (한국시간)


def wait_until_send_time():
    """계산을 먼저 끝내 두고, 정해진 시각(오전 07:50 / 오후 18:50)까지 기다렸다가 전송.
    이미 그 시각이 지났으면 기다리지 않고 바로 전송. 수동 실행(FORCE)은 바로 전송."""
    if FORCE:
        return
    now = dt.datetime.now(KST)
    h, m = SEND_MORNING if now.hour < 12 else SEND_EVENING
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    wait = (target - now).total_seconds()
    if 0 < wait <= 45 * 60:  # 최대 45분까지만 대기
        print(f"{h:02d}:{m:02d}까지 {int(wait)}초 대기")
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
    lines = [f"⚠ {prefix}"]
    for x in FAIL_LOG:
        lines.append("- " + x)
    if not FAIL_LOG:
        lines.append(f"진단: {LAST_INFO['msg'] or '원인 기록 없음'}")
    lines.append(f"응답 항목: {LAST_INFO['keys'] or '없음'}")
    return "\n".join(lines)


def scan_naver():
    """1순위: 네이버 증권. 실패하면 None (알림은 보내지 않고 다음 출처로 넘김)"""
    LAST_INFO["msg"] = ""
    stocks = get_universe()
    if not stocks:
        FAIL_LOG.append("네이버: 종목 목록 실패 / " + (LAST_INFO["msg"] or "원인 기록 없음"))
        return None
    print("[네이버] 검사 종목 수:", len(stocks))

    results, nodata, base_dates = [], 0, []
    top_rows = {}  # TOP 40 현황판용 (코드 -> (이름, 21일 데이터))
    for i, (code, name) in enumerate(stocks, 1):
        try:
            d21 = get_rows(code)
            if d21 is not None:
                top_rows[code] = (name, d21)
            r, status, last = analyze_rows(code, name, d21)
        except Exception as e:
            print("오류", code, e)
            r, status, last = None, "nodata", None
        if status == "nodata":
            nodata += 1
        if last:
            base_dates.append(last)
        if r:
            results.append(r)
        if i == 30 and nodata >= 28:  # 처음부터 거의 다 실패 -> 빨리 중단
            FAIL_LOG.append("네이버: 종목별 데이터를 거의 읽지 못함 / " + (LAST_INFO["msg"] or "원인 기록 없음"))
            return None
        if i % 100 == 0:
            print(f"  진행 {i}/{len(stocks)}")
    if nodata > len(stocks) * 0.5:
        FAIL_LOG.append(f"네이버: 데이터 수집 실패 {nodata}/{len(stocks)} / " + (LAST_INFO["msg"] or ""))
        return None
    return {"results": results, "total": len(stocks), "nodata": nodata, "base_dates": base_dates,
            "top_rows": top_rows, "krx_stock": None}


def scan_krx():
    """2순위(또는 지정 시): 한국거래소 KRX (pykrx). 실패하면 None"""
    LAST_INFO["msg"] = ""
    stock = krx_import()
    if stock is None:
        FAIL_LOG.append("KRX: " + LAST_INFO["msg"])
        return None
    try:
        data = krx_collect(stock)
    except Exception as e:
        FAIL_LOG.append(f"KRX: 수집 오류 {type(e).__name__}: {str(e)[:150]}"
                        + (f" / {LAST_INFO['msg']}" if LAST_INFO["msg"] else ""))
        return None
    if not data:
        FAIL_LOG.append("KRX: " + (LAST_INFO["msg"] or "자료 없음 (KRX 로그인/접속 확인 필요)"))
        return None
    print("[KRX] 검사 종목 수:", len(data))

    results, nodata, base_dates = [], 0, []
    top_rows = {code: (code, d21) for code, d21 in data.items()}  # TOP 40 현황판용
    for code, d21 in data.items():
        try:
            r, status, last = analyze_rows(code, code, d21)
        except Exception as e:
            print("오류", code, e)
            r, status, last = None, "nodata", None
        if status == "nodata":
            nodata += 1
        if last:
            base_dates.append(last)
        if r:
            results.append(r)
    # 종목명 확인 + ETF/스팩/리츠 등 제외 (후보 종목에만 조회)
    final = []
    for r in results:
        name = krx_name(stock, r["code"])
        if is_excluded(name):
            continue
        r["name"] = name
        final.append(r)
    if nodata > len(data) * 0.5:
        FAIL_LOG.append(f"KRX: 데이터 수집 실패 {nodata}/{len(data)}")
        return None
    return {"results": final, "total": len(data), "nodata": nodata, "base_dates": base_dates,
            "top_rows": top_rows, "krx_stock": stock}


def main():
    t0 = time.time()
    now = dt.datetime.now(KST)
    if not FORCE and is_market_closed(now.date()):
        print("휴장일이라 건너뜁니다:", now.date())
        return

    out, label = None, ""
    if DATA_SOURCE in ("auto", "naver"):
        out, label = scan_naver(), "네이버 증권"
    if out is None and DATA_SOURCE in ("auto", "krx"):
        if DATA_SOURCE == "auto":
            print("네이버 수집 실패 -> KRX(한국거래소)로 전환합니다.")
        out = scan_krx()
        label = "한국거래소(KRX) 정보데이터시스템" + (" ※네이버 실패로 전환" if DATA_SOURCE == "auto" else "")
    if out is None:
        record_freshness(now, "-", [], 0, 0, 0, "fail", t0)
        send_plain(fail_message("모든 데이터 출처에서 수집에 실패했습니다."))
        return

    results, total, nodata = out["results"], out["total"], out["nodata"]
    base_dates = out["base_dates"]
    base_date = max(base_dates) if base_dates else "알 수 없음"
    FRESH.clear()
    FRESH.update(compute_fresh(now, base_date))
    if FRESH.get("lag", 0) > 0:
        print(f"⚠ 최신 수급 미반영: 예상 {FRESH['expected']} / 실제 {FRESH['base']} ({FRESH['lag']}거래일)")

    # 후보 종목에만 60일선 분석 (요청 수를 아끼기 위해)
    print(f"60일선 분석: {len(results)}종목")
    for r in results:
        try:
            enrich_ma(r)
        except Exception as e:
            print("60일선 분석 오류", r["code"], e)
            r["ma_label"] = "부족"
        r["pullback"] = is_pullback(r)
    print("⭐ 눌림목 패턴:", sum(1 for r in results if r["pullback"]), "종목")

    # 성과 기록 저장 + 과거 알림 종목 수익률 갱신
    perf_msg = None
    try:
        hist = load_history()
        hist = record_history(hist, results)
        hist = update_returns(hist)
        save_history(hist)
        perf_msg = build_performance_message(hist)
    except Exception as e:
        print("성과 기록 처리 오류:", e)

    record_freshness(now, label, base_dates, total, nodata, len(results), "ok", t0)
    wait_until_send_time()
    send_condition_messages(results, total, nodata, base_date, label)
    try:  # 별도 현황판: 실패해도 기존 알림에는 영향 없음
        send_top40_messages(out.get("top_rows") or {}, base_date, label, out.get("krx_stock"))
    except Exception as e:
        print("TOP 40 처리 오류:", type(e).__name__, e)
    if perf_msg and (FORCE or now.hour >= 12):  # 성과 요약은 저녁 알림에만
        tg_send(perf_msg, use_html=True)
    print("완료. 해당 종목:", len(results))


if __name__ == "__main__":
    main()
