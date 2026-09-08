"""
Bloomberg WTI Crude Oil Single Excess Return (ER) / Total Return (TR) Index Replication
Bloomberg Single Commodity Index Methodology — Production Implementation

Base Date : 2010-01-04  |  Base Level : 100.0 (both ER and TR)
    (Bloomberg 방법론상의 공식 Base Date. 이 모듈(정적 롤오버 공통 베이스)의
     __main__ 실행 예시는 보유 선물 데이터 구간에 맞춰 START_DATE = 2016-01-02
     부터 계산해 100.0으로 리베이스합니다 — 아래 __main__ 블록의 START_DATE /
     BASE_ER / BASE_TR 주석 참조. 실제로 README에 보고된 GSCI DRA 성과는
     `reference/ws_1_index.py`의 __main__(START_DATE = 2016-01-04)으로 산출된
     것이며, README §2-5의 "Base 2016-01-04"는 그 값을 가리킵니다.)
Commodity : WTI Crude Oil  |  Bloomberg Code : CL  |  Exchange : CME/NYMEX
"""

from __future__ import annotations

import warnings
from typing import Optional, Tuple

import numpy as np
import pandas as pd


# ============================================================
# 파트 1: 상수 및 설정 정의
# 인덱스 계산에 필요한 모든 고정 상수값을 정의합니다.
# Bloomberg 방법론에 명시된 Base Date, Base Level, 계약 코드 매핑,
# 롤 파라미터, T-Bill 계산 기준 등을 이 섹션에서 관리합니다.
# ============================================================

BASE_DATE: pd.Timestamp = pd.Timestamp("2010-01-04")
BASE_ER_LEVEL: float = 100.0
BASE_TR_LEVEL: float = 100.0

LOT_SIZE: float = 1000.0  # WTI 원유 선물 계약 단위: 1계약 = 1,000배럴
                           # 주의: DER = WAV/PWAV - 1 계산 시 L이 분자·분모에서 약분되므로
                           # ER·TR 인덱스 레벨 자체에는 영향이 없음. WAV/PWAV 절대값에만 반영됨.
CIM: float = 1.0        # Commodity Index Multiplier (연간 리밸런싱 없음 → 항상 1)

# Bloomberg 방법론 명시 월별 Lead 계약 코드
# (각 달의 첫 번째 영업일 기준 Lead Contract)
# Jan→G(Feb), Feb→H(Mar), Mar→J(Apr), Apr→K(May),
# May→M(Jun), Jun→N(Jul), Jul→Q(Aug), Aug→U(Sep),
# Sep→V(Oct), Oct→X(Nov), Nov→Z(Dec), Dec→F(Jan 다음 해)
MONTH_TO_LEAD_CODE: dict[int, str] = {
    1: "G", 2: "H", 3: "J", 4: "K", 5: "M", 6: "N",
    7: "Q", 8: "U", 9: "V", 10: "X", 11: "Z", 12: "F",
}

# 선물 월 코드 → 달 번호 매핑
FUTURES_CODE_TO_MONTH: dict[str, int] = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

# 월 코드 순서 리스트 (Next 계약 코드 결정에 사용)
ALL_MONTH_CODES: list[str] = ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]

ROLL_START_BD: int = 6      # 롤 시작 영업일 순번
ROLL_END_BD: int = 10       # 롤 완료 영업일 순번
WEIGHT_STEP: float = 0.20   # 영업일당 롤 비중 이동량 (20%)

TBILL_MATURITY: int = 91    # 13주 T-Bill 만기 일수
TBILL_DAY_COUNT: int = 360  # T-Bill 할인율 계산 기준 일수 (Actual/360)

COMMODITY_PREFIX: str = "CL"  # WTI 선물 티커 접두사


# ============================================================
# 파트 2: CME 영업일 캘린더 생성
# WTI 원유 선물이 거래되는 CME(NYMEX) 거래소의 휴장일을 반영한
# 영업일 목록을 생성합니다. pandas_market_calendars 라이브러리가
# 설치되어 있으면 이를 우선 사용하고, 없으면 알려진 CME 공휴일을
# 수동으로 적용하는 대체 로직을 실행합니다.
# ============================================================

def get_cme_business_days(
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DatetimeIndex:
    """지정 기간의 CME 영업일 목록을 반환합니다."""
    try:
        import pandas_market_calendars as mcal  # type: ignore

        # "CME" 는 등록된 캘린더명이 아님 — WTI(CL) 전용 캘린더 사용
        cal = mcal.get_calendar("CMEGlobex_CL")
        schedule = cal.schedule(start_date=start_date, end_date=end_date)
        bdays = mcal.date_range(schedule, frequency="1D").normalize().tz_localize(None)
        return bdays
    except (ImportError, RuntimeError) as e:
        warnings.warn(
            f"pandas_market_calendars 캘린더 로드 실패 ({e}) → 내장 CME 공휴일 목록으로 대체.\n"
            "라이브러리 미설치 시: pip install pandas-market-calendars",
            UserWarning,
            stacklevel=2,
        )
        return _cme_fallback(start_date, end_date)


def _cme_fallback(
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DatetimeIndex:
    """pandas_market_calendars 없이 CME 영업일을 계산하는 대체 함수."""
    all_weekdays = pd.bdate_range(start=start_date, end=end_date)
    holidays = _build_cme_holidays(start_date.year, end_date.year)
    return all_weekdays[~all_weekdays.isin(holidays)]


def _build_cme_holidays(start_year: int, end_year: int) -> pd.DatetimeIndex:
    """
    CME가 준수하는 미국 연방 공휴일 목록을 생성합니다.
    포함 공휴일: New Year's Day, MLK Day, Presidents Day, Good Friday,
    Memorial Day, Juneteenth (2022~), Independence Day,
    Labor Day, Thanksgiving, Christmas Day
    """
    try:
        from dateutil.easter import easter
        from dateutil.relativedelta import MO, TH, relativedelta
    except ImportError:
        raise ImportError(
            "python-dateutil 필요: pip install python-dateutil"
        )

    holidays: list[pd.Timestamp] = []

    def obs(d: pd.Timestamp) -> pd.Timestamp:
        """주말 공휴일의 대체 관측일 반환 (토→금, 일→월)."""
        if d.dayofweek == 5:
            return d - pd.Timedelta(days=1)
        if d.dayofweek == 6:
            return d + pd.Timedelta(days=1)
        return d

    for yr in range(start_year, end_year + 1):
        # New Year's Day
        holidays.append(obs(pd.Timestamp(yr, 1, 1)))
        # MLK Day — 1월 3번째 월요일
        holidays.append(pd.Timestamp(yr, 1, 1) + relativedelta(weekday=MO(3)))
        # Presidents Day — 2월 3번째 월요일
        holidays.append(pd.Timestamp(yr, 2, 1) + relativedelta(weekday=MO(3)))
        # Good Friday — 부활절 전 금요일
        holidays.append(pd.Timestamp(easter(yr)) - pd.Timedelta(days=2))
        # Memorial Day — 5월 마지막 월요일
        holidays.append(pd.Timestamp(yr, 5, 31) + relativedelta(weekday=MO(-1)))
        # Juneteenth — 2022년부터
        if yr >= 2022:
            holidays.append(obs(pd.Timestamp(yr, 6, 19)))
        # Independence Day
        holidays.append(obs(pd.Timestamp(yr, 7, 4)))
        # Labor Day — 9월 첫 번째 월요일
        holidays.append(pd.Timestamp(yr, 9, 1) + relativedelta(weekday=MO(1)))
        # Thanksgiving — 11월 4번째 목요일
        holidays.append(pd.Timestamp(yr, 11, 1) + relativedelta(weekday=TH(4)))
        # Christmas Day
        holidays.append(obs(pd.Timestamp(yr, 12, 25)))

    return pd.DatetimeIndex(sorted(set(holidays)))


# ============================================================
# 파트 3: 계약 티커 및 스케줄 생성
# 각 영업일에 대해 어떤 Lead 계약과 Next 계약을 보유하는지 결정하고,
# 실제 Bloomberg 티커 형식(예: CLG15, CLH15)의 계약명을 생성합니다.
# 월 경계(특히 11월/12월)에서의 연도 처리도 포함합니다.
# ============================================================

def _next_code(lead: str) -> str:
    """Lead 계약 코드로부터 Next 계약 코드를 반환합니다."""
    idx = ALL_MONTH_CODES.index(lead)
    return ALL_MONTH_CODES[(idx + 1) % 12]


def _contract_year(calendar_month: int, calendar_year: int, code: str) -> int:
    """
    계약 코드와 현재 달/연도로부터 실제 계약 연도를 계산합니다.
    계약 월이 현재 달보다 같거나 작으면 다음 연도 계약입니다.
    예) December 2015의 Lead 'F'(Jan) → 2016
    """
    contract_month = FUTURES_CODE_TO_MONTH[code]
    return calendar_year + 1 if contract_month <= calendar_month else calendar_year


def _ticker(prefix: str, code: str, year: int) -> str:
    """Bloomberg 티커 형식 생성: 예) CL + G + 15 = 'CLG15'"""
    return f"{prefix}{code}{str(year)[-2:]}"


def build_contract_schedule(
    business_days: pd.DatetimeIndex,
    prefix: str = COMMODITY_PREFIX,
) -> pd.DataFrame:
    """
    각 영업일에 대한 Lead/Next 계약 티커와 월별 영업일 순번을 생성합니다.

    Returns
    -------
    pd.DataFrame
        index : business_days
        columns : lead_ticker, next_ticker, bd_of_month
    """
    df = pd.DataFrame({"date": business_days})
    df = df.set_index("date")

    df["year"] = df.index.year
    df["month"] = df.index.month

    # 월별 영업일 순번 (1-based)
    df["bd_of_month"] = (
        df.groupby([df.index.year, df.index.month]).cumcount() + 1
    )

    def row_tickers(row: pd.Series) -> pd.Series:
        m, y = int(row["month"]), int(row["year"])
        lc = MONTH_TO_LEAD_CODE[m]
        nc = _next_code(lc)
        ly = _contract_year(m, y, lc)
        ny = _contract_year(m, y, nc)
        return pd.Series(
            {
                "lead_ticker": _ticker(prefix, lc, ly),
                "next_ticker": _ticker(prefix, nc, ny),
            }
        )

    tickers = df.apply(row_tickers, axis=1)
    df = df.join(tickers)
    return df[["lead_ticker", "next_ticker", "bd_of_month"]]


# ============================================================
# 파트 4: 롤 비중(Roll Weight) 계산 — MDE 처리 포함
# Bloomberg 방법론에 따라 매월 6번째~10번째 영업일에 걸쳐
# Lead 계약에서 Next 계약으로 20%씩 비중을 이동합니다.
# MDE(시장 교란 이벤트 — 가격 데이터 누락으로 정의)가 발생하면
# 해당 날의 롤을 보류하고 다음 정상 영업일에 누적 적용합니다.
# ============================================================

def calculate_roll_weights(
    contract_schedule: pd.DataFrame,
    prices_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    각 영업일의 Lead/Next 롤 비중과 MDE 여부를 계산합니다.

    MDE 정의: 롤 기간(BD 6~10) 중 Lead 또는 Next 계약의
    결제가격이 누락(NaN)된 영업일.

    Returns
    -------
    pd.DataFrame
        index : business_days
        columns : lead_weight, next_weight, is_mde
    """
    lead_w_dict: dict = {}
    next_w_dict: dict = {}
    mde_dict: dict = {}

    cur_lead_w: float = 1.0
    cur_next_w: float = 0.0
    pending: int = 0          # MDE로 보류된 롤 횟수
    last_ym: tuple = (0, 0)   # (year, month) — 월 경계 감지용

    def has_price(ticker: str, date: pd.Timestamp) -> bool:
        if ticker not in prices_df.columns:
            return False
        if date not in prices_df.index:
            return False
        return not np.isnan(prices_df.at[date, ticker])

    for date in contract_schedule.index:
        row = contract_schedule.loc[date]
        bd = int(row["bd_of_month"])
        ym = (date.year, date.month)

        # 새 달 시작 시 롤 상태 초기화
        if ym != last_ym:
            cur_lead_w = 1.0
            cur_next_w = 0.0
            pending = 0
            last_ym = ym

        lead_t: str = row["lead_ticker"]
        next_t: str = row["next_ticker"]

        is_mde: bool = False

        if ROLL_START_BD <= bd <= ROLL_END_BD:
            # 롤 기간: 가격 누락 = MDE
            if not has_price(lead_t, date) or not has_price(next_t, date):
                is_mde = True
                pending += 1
            else:
                # 오늘 롤 + 이전 보류분 한 번에 처리
                rolls = 1 + pending
                cur_next_w = min(1.0, cur_next_w + WEIGHT_STEP * rolls)
                cur_lead_w = max(0.0, 1.0 - cur_next_w)
                pending = 0

        elif bd > ROLL_END_BD and pending > 0:
            # 롤 기간 이후에도 보류분이 남아 있으면 다음 정상일에 처리
            if not has_price(lead_t, date) or not has_price(next_t, date):
                is_mde = True
                pending += 1
            else:
                cur_next_w = min(1.0, cur_next_w + WEIGHT_STEP * pending)
                cur_lead_w = max(0.0, 1.0 - cur_next_w)
                pending = 0

        lead_w_dict[date] = cur_lead_w
        next_w_dict[date] = cur_next_w
        mde_dict[date] = is_mde

    return pd.DataFrame(
        {
            "lead_weight": lead_w_dict,
            "next_weight": next_w_dict,
            "is_mde": mde_dict,
        }
    )


# ============================================================
# 파트 5: 데이터 로딩 및 검증
# 사용자가 제공하는 WTI 선물 결제가격(Databento Long 형식)과
# 13주 T-Bill 월간 고할인율 데이터를 로드하고 전처리합니다.
#
# [선물 결제가격 - Databento 형식]
# 컬럼 A: Trade date (거래일)
# 컬럼 B: Symbol (예: CLG6 — 마지막 1자리 연도, 10년 주기 중복 가능)
# 컬럼 C: Settlement price (결제가격, USD/배럴)
# 컬럼 F: expiration (정확한 만기일시, 타임존 포함)
# → expiration에서 4자리 연도를 추출하여 내부 2자리 연도 티커 생성
#   예) CLG6 + 만기 2016년 → 내부 티커 'CLG16'
#       CLG6 + 만기 2026년 → 내부 티커 'CLG26'
# → 여러 연도의 CSV 파일을 리스트로 넘기면 자동 병합
#
# [T-Bill 금리 - Moody's Analytics 형식]
# 상단 5행: 메타데이터 헤더 (자동 스킵)
# 날짜 형식: 'YY-Mon' 또는 'Mon-YY' 혼용
#   → 숫자 부분이 연도(2자리), 영문 부분이 월 약어
#   → 00~30 → 2000~2030 / 31~99 → 1931~1999
#   예) '10-Jan' 또는 'Jan-10' → 2010년 1월
# 값: % 단위 월간 고할인율 → 소수점으로 변환하여 반환
# 'na' 값은 NaN으로 처리하고 제거
# ============================================================

def load_futures_prices(
    source,
    trade_date_col: str = "Trade date",
    symbol_col: str = "Symbol",
    price_col: str = "Settlement price",
    expiration_col: str = "expiration",
) -> pd.DataFrame:
    """
    WTI 선물 결제가격 데이터를 로드합니다. (Databento Long 형식 전용)

    Parameters
    ----------
    source : str, list[str], 또는 pd.DataFrame
        - str      : 단일 CSV 파일 경로
        - list[str]: 연도별 분할 파일 목록 (자동 병합)
        - pd.DataFrame: 이미 로드된 데이터프레임

    Returns
    -------
    pd.DataFrame  (index=거래일, columns=내부 2자리연도 티커, dtype=float)
        예) 컬럼명: CLG16, CLH16, CLJ16, ...
    """
    # 단일·복수 파일 또는 DataFrame 로드
    if isinstance(source, list):
        frames = [pd.read_csv(f) for f in source]
        df = pd.concat(frames, ignore_index=True)
    elif isinstance(source, str):
        df = pd.read_csv(source)
    else:
        df = source.copy()

    # 필수 컬럼 존재 확인
    for col in [trade_date_col, symbol_col, price_col, expiration_col]:
        if col not in df.columns:
            raise ValueError(
                f"필수 컬럼 '{col}'이(가) 데이터에 없습니다. "
                f"실제 컬럼: {list(df.columns)}"
            )

    # 거래일 파싱
    df[trade_date_col] = pd.to_datetime(df[trade_date_col])

    # 만기일 파싱 및 타임존 제거
    df[expiration_col] = (
        pd.to_datetime(df[expiration_col], utc=True).dt.tz_localize(None)
    )

    # 내부 티커 생성 (중복 단일자리 심볼 해결)
    #
    # [버그 방지] WTI 선물은 인도월(delivery month) 전달에 만기가 옵니다.
    # 예) CLG16 (2월 인도)의 만기일 = 2016-01-20 (1월)
    # → 만기일의 달(1월="F")로 코드를 추출하면 CLF16이 생성되어 한 달 밀림.
    #
    # 해결: Symbol 컬럼에서 월 코드 문자를 직접 추출 (항상 인도월 기준으로 올바름).
    # 연도는 만기일에서 추출하되, 인도월이 만기월보다 작거나 같으면 +1년 보정.
    # 예) CLG6  만기 2016-01-20: G(2월) > 1월 → 인도연도=2016 → CLG16 ✓
    #     CLF6  만기 2015-12-18: F(1월) ≤ 12월 → 인도연도=2016 → CLF16 ✓
    df["_month_code"] = df[symbol_col].str.extract(r"^CL([A-Z])")[0]
    exp_year  = df[expiration_col].dt.year
    exp_month = df[expiration_col].dt.month
    delivery_month_num = df["_month_code"].map(FUTURES_CODE_TO_MONTH)
    # 인도월 번호가 만기월 번호보다 크면 같은 해, 같거나 작으면 다음 해
    delivery_year = exp_year.where(delivery_month_num > exp_month, exp_year + 1)
    df["_internal_ticker"] = (
        COMMODITY_PREFIX + df["_month_code"] + delivery_year.astype(str).str[-2:]
    )

    # 결제가격 숫자 변환
    df[price_col] = pd.to_numeric(df[price_col], errors="coerce")
    if df[price_col].isna().all():
        raise ValueError("결제가격 컬럼에 유효한 숫자 데이터가 없습니다.")

    # Wide 형식으로 피벗: 거래일(행) × 내부 티커(열)
    prices = df.pivot_table(
        index=trade_date_col,
        columns="_internal_ticker",
        values=price_col,
        aggfunc="first",
    )
    prices.columns.name = None
    prices = prices.sort_index().astype(float)

    if prices.empty:
        raise ValueError("선물 가격 데이터가 비어 있습니다.")
    if (prices < 0).any().any():
        warnings.warn("음수 선물 가격이 감지되었습니다. 데이터를 확인하세요.", UserWarning)

    return prices


def load_tbill_rates(
    source,
    rate_col_index: int = 1,
) -> pd.Series:
    """
    13주 T-Bill 월간 고할인율(High Discount Rate)을 로드합니다.
    Moody's Analytics CSV 내보내기 형식 전용.

    Parameters
    ----------
    source : str 또는 pd.DataFrame
        Moody's Analytics CSV 파일 경로 (또는 이미 로드된 DataFrame)
    rate_col_index : int
        금리 값이 있는 열 인덱스 (기본값=1, 두 번째 열)

    날짜 파싱 규칙:
        - 숫자 부분 = 2자리 연도, 영문 부분 = 월 약어 (순서 무관)
        - 예) '10-Jan' → 2010-01-01 / 'Jan-10' → 2010-01-01
        - 2자리 연도: 00~30 → 2000~2030, 31~99 → 1931~1999

    Returns
    -------
    pd.Series  (index=월초 날짜, values=소수점 금리)
    """
    if isinstance(source, str):
        # 상단 5행 메타데이터 헤더 스킵, 컬럼명 없음
        raw = pd.read_csv(source, header=None, skiprows=5)
    else:
        raw = source.copy()
        raw.columns = range(len(raw.columns))

    def _parse_tbill_date(date_str: str) -> Optional[pd.Timestamp]:
        """'YY-Mon' 또는 'Mon-YY' 형식의 날짜 문자열을 pd.Timestamp로 변환합니다."""
        s = str(date_str).strip()
        parts = s.split("-")
        if len(parts) != 2:
            return None
        a, b = parts[0].strip(), parts[1].strip()
        # 숫자 부분이 연도, 영문 부분이 월
        if a.isdigit():
            year_str, month_str = a, b
        else:
            month_str, year_str = a, b
        try:
            yr2 = int(year_str)
            year = 2000 + yr2 if yr2 <= 30 else 1900 + yr2
            return pd.Timestamp(f"{year}-{month_str}-01")
        except Exception:
            return None

    dates = raw[0].apply(_parse_tbill_date)
    rates = pd.to_numeric(raw[rate_col_index], errors="coerce") / 100.0

    s = pd.Series(rates.values, index=dates, name="tbr")
    s = s[s.index.notna()]  # 날짜 파싱 실패 행 제거
    s = s.dropna()          # 'na' 금리 값 제거
    s = s.sort_index()

    if s.empty:
        raise ValueError("T-Bill 금리 데이터가 비어 있습니다.")
    if (s < 0).any():
        warnings.warn("음수 T-Bill 금리가 감지되었습니다.", UserWarning)

    return s


def align_tbill(
    tbill: pd.Series,
    business_days: pd.DatetimeIndex,
) -> pd.Series:
    """
    주간 T-Bill 금리를 영업일 기준으로 재인덱싱하고 forward-fill을 적용합니다.
    Bloomberg 방법론: TBR_{t-1}은 가장 최근 주간 경매 고할인율을 사용합니다.
    """
    aligned = tbill.reindex(business_days, method="ffill")
    n_miss = aligned.isna().sum()
    if n_miss:
        warnings.warn(
            f"T-Bill 금리 {n_miss}개 영업일에 데이터가 없습니다. "
            "데이터 범위를 확인하세요.",
            UserWarning,
        )
    return aligned


# ============================================================
# 파트 6: WAV 및 PWAV 계산
# Bloomberg 방법론의 Weighted Average Value를 계산합니다.
# WAV는 오늘 가격에 어제의 롤 비중을 적용한 가중평균가치이고,
# PWAV는 어제 가격에 어제의 롤 비중을 적용한 가중평균가치입니다.
# 두 값의 비율(WAV/PWAV)이 일일 초과 수익률(DER)의 기반입니다.
# 가격이 누락된 경우 Bloomberg 방법론에 따라 마지막 유효 가격을 사용합니다.
# ============================================================

def calculate_wav_pwav(
    business_days: pd.DatetimeIndex,
    contract_schedule: pd.DataFrame,
    roll_weights: pd.DataFrame,
    prices_df: pd.DataFrame,
    lot_size: float = LOT_SIZE,
) -> pd.DataFrame:
    """
    각 영업일의 WAV(오늘 기준)와 PWAV(어제 기준)를 계산합니다.

    Bloomberg 공식:
    WAV_t  = CIM * YLRW * (LCSP_t  / L) + CIM * YNRW * (NCSP_t  / L)
    PWAV_t = CIM * YLRW * (LCSP_t-1 / L) + CIM * YNRW * (NCSP_t-1 / L)

    여기서 YLRW, YNRW는 t-1(어제)의 롤 비중,
    LCSP/NCSP는 각각 어제 보유한 Lead/Next 계약의 가격입니다.

    Returns
    -------
    pd.DataFrame  columns: wav, pwav
    """
    # 각 계약에 대해 마지막 유효 가격 forward-fill 적용
    # (Bloomberg: 가격 미발표 시 직전 유효 결제가격 사용)
    prices_ffill = prices_df.reindex(business_days).ffill()

    dates = list(business_days)
    wav_vals: list[float] = []
    pwav_vals: list[float] = []

    for i, date in enumerate(dates):
        if i == 0:
            wav_vals.append(np.nan)
            pwav_vals.append(np.nan)
            continue

        prev = dates[i - 1]

        # 어제의 롤 비중 (YLRW = Yesterday Lead Roll Weight)
        ylrw: float = roll_weights.at[prev, "lead_weight"]
        ynrw: float = roll_weights.at[prev, "next_weight"]

        # 어제 보유한 계약 티커
        lead_prev = contract_schedule.at[prev, "lead_ticker"]
        next_prev = contract_schedule.at[prev, "next_ticker"]

        def safe_price(ticker: str, dt: pd.Timestamp) -> float:
            if ticker not in prices_ffill.columns:
                return np.nan
            if dt not in prices_ffill.index:
                return np.nan
            return prices_ffill.at[dt, ticker]

        lcsp_t   = safe_price(lead_prev, date)
        ncsp_t   = safe_price(next_prev, date)
        lcsp_tm1 = safe_price(lead_prev, prev)
        ncsp_tm1 = safe_price(next_prev, prev)

        wav  = CIM * ylrw * (lcsp_t   / lot_size) + CIM * ynrw * (ncsp_t   / lot_size)
        pwav = CIM * ylrw * (lcsp_tm1 / lot_size) + CIM * ynrw * (ncsp_tm1 / lot_size)

        wav_vals.append(wav)
        pwav_vals.append(pwav)

    return pd.DataFrame({"wav": wav_vals, "pwav": pwav_vals}, index=dates)


# ============================================================
# 파트 7: ER(초과 수익) 인덱스 계산
# 일일 초과 수익률(DER = WAV/PWAV - 1)을 누적하여 ER 인덱스를 계산합니다.
# ER 인덱스는 선물 가격 변동만을 반영하며 무위험 수익률(T-Bill)은
# 포함하지 않습니다. Zero Floor(인덱스 레벨 ≥ 0)가 적용됩니다.
# 공식: IndexER_t = IndexER_{t-1} * (1 + DER_t), 8자리 소수점 반올림.
# ============================================================

def calculate_er_index(
    wav_pwav: pd.DataFrame,
    base_level: float = BASE_ER_LEVEL,
) -> pd.Series:
    """
    ER 인덱스 레벨을 계산합니다.

    Bloomberg 공식:
      DER_t       = WAV_t / PWAV_t - 1
      IndexER_t   = IndexER_{t-1} * (1 + DER_t)   [Zero Floor, 8dp]
    """
    er: list[float] = []
    prev_level = base_level

    for wav, pwav in zip(wav_pwav["wav"], wav_pwav["pwav"]):
        if np.isnan(wav) or np.isnan(pwav) or pwav == 0.0:
            er.append(round(prev_level, 8))
            continue

        der = wav / pwav - 1.0
        new_level = max(0.0, prev_level * (1.0 + der))
        new_level = round(new_level, 8)
        er.append(new_level)
        prev_level = new_level

    return pd.Series(er, index=wav_pwav.index, name="er_index")


# ============================================================
# 파트 8: T-Bill 일일 수익률(IR) 계산
# 13주 T-Bill 주간 경매 고할인율과 영업일 간 캘린더 일수 차이(D)를
# 이용하여 일일 T-Bill 수익률(IR)을 계산합니다.
# D는 주말·공휴일을 포함한 실제 캘린더 일수 차이입니다
# (예: 월요일 → 전주 금요일 = 3일).
# 공식: IR_t = [1/(1-(91/360)*TBR_{t-1})]^(D/91) - 1
# ============================================================

def calculate_ir(
    business_days: pd.DatetimeIndex,
    tbill_aligned: pd.Series,
) -> pd.Series:
    """
    T-Bill 일일 수익률(IR_t)을 계산합니다.

    Bloomberg 공식:
      IR_t = [1 / (1 - (91/360) * TBR_{t-1})]^(D/91) - 1

    여기서:
      TBR_{t-1} : 직전 영업일의 13주 T-Bill 주간 경매 고할인율 (소수점)
      D         : 직전 영업일과 오늘 사이의 캘린더 일수 차이
    """
    dates = list(business_days)
    ir_vals: list[float] = []

    for i, date in enumerate(dates):
        if i == 0:
            ir_vals.append(0.0)
            continue

        prev = dates[i - 1]
        D: int = (date - prev).days
        tbr = tbill_aligned.get(prev, np.nan)

        if np.isnan(tbr):
            warnings.warn(
                f"{prev.date()} T-Bill 금리 누락 → IR=0 처리", UserWarning, stacklevel=2
            )
            ir_vals.append(0.0)
            continue

        denom = 1.0 - (TBILL_MATURITY / TBILL_DAY_COUNT) * tbr
        if denom <= 0.0:
            warnings.warn(
                f"{date.date()} IR 계산 오류: 분모 ≤ 0 → IR=0 처리", UserWarning, stacklevel=2
            )
            ir_vals.append(0.0)
            continue

        ir = (1.0 / denom) ** (D / TBILL_MATURITY) - 1.0
        ir_vals.append(ir)

    return pd.Series(ir_vals, index=dates, name="ir")


# ============================================================
# 파트 9: TR(총 수익) 인덱스 계산
# ER 인덱스의 일일 수익률과 T-Bill 일일 수익률을 합산하여
# Bloomberg WTI Single TR 인덱스 레벨을 계산합니다.
# TR은 선물 가격 변동(ER 기여분)과 담보 수익(T-Bill 기여분)을
# 모두 반영하는 최종 인덱스입니다.
# 공식: IndexTR_t = IndexTR_{t-1} * (IndexER_t/IndexER_{t-1} + IR_t)
# ============================================================

def calculate_tr_index(
    er_index: pd.Series,
    ir: pd.Series,
    base_level: float = BASE_TR_LEVEL,
) -> pd.Series:
    """
    TR 인덱스 레벨을 계산합니다.

    Bloomberg 공식:
      IndexTR_t = IndexTR_{t-1} * (IndexER_t / IndexER_{t-1} + IR_t)
      [Zero Floor, 8dp]
    """
    dates = er_index.index.tolist()
    tr: list[float] = []
    prev_tr = base_level

    for i, _ in enumerate(dates):
        if i == 0:
            tr.append(round(base_level, 8))
            continue

        er_t   = er_index.iloc[i]
        er_tm1 = er_index.iloc[i - 1]
        ir_t   = ir.iloc[i] if i < len(ir) else 0.0

        if er_tm1 == 0.0 or np.isnan(er_t) or np.isnan(er_tm1):
            tr.append(round(prev_tr, 8))
            continue

        new_tr = max(0.0, prev_tr * (er_t / er_tm1 + ir_t))
        new_tr = round(new_tr, 8)
        tr.append(new_tr)
        prev_tr = new_tr

    return pd.Series(tr, index=dates, name="tr_index")


# ============================================================
# 파트 10: 일일 선물 포지션 및 롤오버 추적
# 각 영업일에 보유 중인 Lead/Next 계약 정보, 결제가격, 블렌디드 가격,
# 롤오버 발생 여부, 그리고 전일 대비 계약 수 변동을 기록합니다.
# notional(가상 운용 자산, USD)을 제공하면 실제 계약 수도 계산합니다.
# ============================================================

def calculate_positions(
    contract_schedule: pd.DataFrame,
    roll_weights: pd.DataFrame,
    prices_df: pd.DataFrame,
    notional: Optional[float] = None,
    fix_contracts: bool = False,
    lot_size: float = LOT_SIZE,
) -> pd.DataFrame:
    """
    각 영업일의 선물 포지션 현황 및 롤오버 이벤트를 계산합니다.

    Parameters
    ----------
    contract_schedule : build_contract_schedule() 반환값
    roll_weights      : calculate_roll_weights() 반환값
    prices_df         : load_futures_prices() 반환값 (Wide 형식)
    notional          : 가상 운용 자산 규모 (USD)
        - 예) 10_000_000 → $10,000,000 규모의 포지션 계산
        - None이면 계약 수 관련 컬럼은 출력에 포함되지 않음
    fix_contracts     : 계약 수 고정 모드 선택
        - False (기본값): Notional 고정 방식
            매일 total_contracts = notional / (blended_price × lot_size) 재계산.
            명목 자산 규모가 고정되고 계약 수는 가격에 따라 매일 변동.
            전략 간 동일 자본 투입 비교에 적합.
        - True: 계약 수 고정 방식
            첫 영업일에 notional 기준으로 계약 수를 한 번 산출하고 이후 고정.
            total_contracts = notional / (초일 blended_price × lot_size)  ← 1회만 계산
            이후 가격 변동 시 notional_value(= total_contracts × blended_price × lot_size)가
            자연스럽게 변동. 롤 기간 중 Lead/Next 비율만 변경.
            실제 ETN 헷징 운용 방식에 가장 근접.
    lot_size          : 계약당 배럴 수 (WTI = 1,000)

    포지션 계산 공식 (fix_contracts=False):
        blended_price   = lead_weight × lead_price + next_weight × next_price  ($/barrel)
        total_contracts = notional / (blended_price × lot_size)   ← 매일 재계산
        lead_contracts  = lead_weight × total_contracts
        next_contracts  = next_weight × total_contracts

    포지션 계산 공식 (fix_contracts=True):
        total_contracts = notional / (초일_blended_price × lot_size)  ← 초일 1회 고정
        lead_contracts  = lead_weight × total_contracts
        next_contracts  = next_weight × total_contracts
        notional_value  = total_contracts × blended_price × lot_size  ← 매일 변동

    롤오버 감지:
        is_roll_day         : 전일 대비 비중 변동 또는 lead_ticker 변경 시 True
        lead_ticker_changed : Lead 계약 자체가 교체된 날 (월 전환 롤 완료 후 첫 날)

    Returns
    -------
    pd.DataFrame  (index=영업일)
        항상 포함 컬럼:
            lead_ticker, next_ticker, bd_of_month,
            lead_weight, next_weight, is_mde,
            lead_price, next_price, blended_price,
            is_roll_day, lead_ticker_changed
        notional 제공 시 추가 컬럼 (fix_contracts=False):
            total_contracts,
            lead_contracts, next_contracts,
            lead_contracts_traded, next_contracts_traded
        notional 제공 시 추가 컬럼 (fix_contracts=True):
            total_contracts,        ← 전 기간 고정
            lead_contracts, next_contracts,
            lead_contracts_traded, next_contracts_traded,
            notional_value          ← 매일 변동하는 명목 자산 가치 ($)
    """
    bdays = contract_schedule.index
    prices_ffill = prices_df.reindex(bdays).ffill()

    rows = []
    prev_lead_ticker: Optional[str] = None
    prev_lead_w: Optional[float] = None
    prev_lead_c: float = 0.0
    prev_next_c: float = 0.0
    fixed_total_c: Optional[float] = None   # fix_contracts=True 시 초일 계약 수 저장

    for date in bdays:
        srow = contract_schedule.loc[date]
        rrow = roll_weights.loc[date]

        lead_t: str = srow["lead_ticker"]
        next_t: str = srow["next_ticker"]
        bd: int    = int(srow["bd_of_month"])
        lead_w: float = rrow["lead_weight"]
        next_w: float = rrow["next_weight"]
        is_mde: bool  = rrow["is_mde"]

        def _px(ticker: str) -> float:
            if ticker in prices_ffill.columns:
                v = prices_ffill.at[date, ticker]
                return float(v) if not np.isnan(v) else np.nan
            return np.nan

        lead_price: float = _px(lead_t)
        next_price: float = _px(next_t)

        # 블렌디드 가격 (NaN 계약은 0으로 처리하여 가중평균)
        lp  = lead_price if not np.isnan(lead_price) else 0.0
        np_ = next_price if not np.isnan(next_price) else 0.0
        blended = lead_w * lp + next_w * np_

        # 롤오버 감지
        lead_ticker_changed = (prev_lead_ticker is not None) and (lead_t != prev_lead_ticker)
        weight_changed      = (prev_lead_w is not None) and (lead_w != prev_lead_w)
        is_roll_day         = lead_ticker_changed or weight_changed

        row: dict = {
            "lead_ticker":         lead_t,
            "next_ticker":         next_t,
            "bd_of_month":         bd,
            "lead_weight":         lead_w,
            "next_weight":         next_w,
            "is_mde":              is_mde,
            "lead_price":          lead_price,
            "next_price":          next_price,
            "blended_price":       blended,
            "is_roll_day":         is_roll_day,
            "lead_ticker_changed": lead_ticker_changed,
        }

        if notional is not None:
            if blended > 0.0:
                if fix_contracts:
                    # 계약 수 고정 방식: 초일에 한 번만 계산, 이후 재사용
                    if fixed_total_c is None:
                        fixed_total_c = notional / (blended * lot_size)
                    total_c = fixed_total_c
                    notional_val = total_c * blended * lot_size
                else:
                    # Notional 고정 방식: 매일 재계산
                    total_c = notional / (blended * lot_size)

                lead_c = lead_w * total_c
                next_c = next_w * total_c
            else:
                total_c = lead_c = next_c = np.nan
                notional_val = np.nan

            lead_traded = (lead_c - prev_lead_c) if not np.isnan(lead_c) else np.nan
            next_traded = (next_c - prev_next_c) if not np.isnan(next_c) else np.nan

            row["total_contracts"]       = total_c
            row["lead_contracts"]        = lead_c
            row["next_contracts"]        = next_c
            row["lead_contracts_traded"] = lead_traded
            row["next_contracts_traded"] = next_traded
            if fix_contracts:
                row["notional_value"] = notional_val

            prev_lead_c = lead_c if not np.isnan(lead_c) else prev_lead_c
            prev_next_c = next_c if not np.isnan(next_c) else prev_next_c

        rows.append(row)
        prev_lead_ticker = lead_t
        prev_lead_w      = lead_w

    base_cols = [
        "lead_ticker", "next_ticker", "bd_of_month",
        "lead_weight", "next_weight", "is_mde",
        "lead_price", "next_price", "blended_price",
        "is_roll_day", "lead_ticker_changed",
    ]
    if notional is not None:
        extra_cols = [
            "total_contracts",
            "lead_contracts", "next_contracts",
            "lead_contracts_traded", "next_contracts_traded",
        ]
        if fix_contracts:
            extra_cols.append("notional_value")
    else:
        extra_cols = []

    return pd.DataFrame(rows, index=bdays)[base_cols + extra_cols]


# ============================================================
# 파트 11: 메인 파이프라인 오케스트레이터
# 위에서 정의한 모든 모듈을 순서대로 호출하여 Bloomberg WTI
# Single ER/TR 인덱스 전체 계산을 실행합니다.
# 데이터 로딩 → CME 캘린더 → 계약 스케줄 → 롤 비중 →
# WAV/PWAV → ER → IR → TR → 포지션 추적 순서로 처리됩니다.
# ============================================================

def run_index_calculation(
    futures_prices_source,
    tbill_rates_source,
    start_date: str = "2016-01-02",
    end_date: str = "2026-03-18",
    base_er_level: float = BASE_ER_LEVEL,
    base_tr_level: float = BASE_TR_LEVEL,
    commodity_prefix: str = COMMODITY_PREFIX,
    lot_size: float = LOT_SIZE,
    output_path: Optional[str] = None,
    notional: Optional[float] = None,
    fix_contracts: bool = False,
    positions_output_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    Bloomberg WTI Crude Oil Single ER/TR 인덱스 전체 계산 파이프라인.

    Parameters
    ----------
    futures_prices_source : str, list[str], 또는 pd.DataFrame
        WTI 선물 결제가격 데이터 (Databento Long 형식 CSV)
        - str      : 단일 CSV 파일 경로
        - list[str]: 연도별 분할 CSV 파일 목록 (자동 병합)
          예) ["cl_settlements_2016.csv", "cl_settlements_2017.csv", ...]
        - pd.DataFrame: 이미 로드된 데이터프레임
    tbill_rates_source : str 또는 pd.DataFrame
        13주 T-Bill 월간 고할인율 (Moody's Analytics CSV 형식)
    start_date : str
        인덱스 계산 시작일. 해당일의 인덱스 레벨은 base_er_level / base_tr_level로 초기화됨.
    end_date : str
        인덱스 계산 종료일
    base_er_level : float
        start_date 당일의 ER 인덱스 초기값.
        Bloomberg 공식 Base Date(2010-01-04)가 아닌 날짜부터 계산할 경우,
        Bloomberg 터미널에서 해당 날짜의 실제 ER 인덱스 레벨을 조회하여 입력하세요.
        (기본값 = 100.0 — 공식 Base Date 기준)
    base_tr_level : float
        start_date 당일의 TR 인덱스 초기값.
        Bloomberg 터미널에서 해당 날짜의 실제 TR 인덱스 레벨을 조회하여 입력하세요.
        (기본값 = 100.0 — 공식 Base Date 기준)
    commodity_prefix : str
        선물 티커 접두사 (WTI = 'CL')
    lot_size : float
        선물 계약 단위 (WTI = 1,000배럴; DER 계산 시 약분되어 인덱스에 영향 없음)
    output_path : str, optional
        인덱스 결과 CSV 저장 경로 (None이면 저장 생략)
    notional : float, optional
        포지션 계산용 가상 운용 자산 규모 (USD)
        예) 10_000_000 → $10,000,000 기준 계약 수 산출
        None이면 계약 수 컬럼 없이 가격·비중·롤오버 정보만 기록
    positions_output_path : str, optional
        포지션 추적 결과 CSV 저장 경로 (None이면 저장 생략)

    Returns
    -------
    pd.DataFrame
        columns : lead_ticker, next_ticker, bd_of_month,
                  lead_weight, next_weight, is_mde,
                  wav, pwav, der,
                  er_index, ir, tr_index
    """
    start_ts = pd.Timestamp(start_date)
    end_ts   = pd.Timestamp(end_date)

    # Step 1: CME 영업일 캘린더
    print(f"[1/8] CME 영업일 캘린더 생성 ({start_date} ~ {end_date}) ...")
    bdays = get_cme_business_days(start_ts, end_ts)
    print(f"      → {len(bdays):,}개 CME 영업일")

    # Step 2: 선물 가격 로드 (Databento Long 형식)
    print("[2/8] 선물 결제가격 데이터 로딩 ...")
    prices = load_futures_prices(futures_prices_source)
    print(f"      → {len(prices):,}행, {len(prices.columns)}개 계약 티커")

    # Step 3: T-Bill 금리 로드 및 정렬 (Moody's Analytics 형식)
    print("[3/8] T-Bill 금리 데이터 로딩 및 영업일 정렬 ...")
    tbill_raw = load_tbill_rates(tbill_rates_source)
    tbill = align_tbill(tbill_raw, bdays)
    print(f"      → {len(tbill):,}개 영업일 금리 적용")

    # Step 4: 계약 스케줄
    print("[4/8] Lead/Next 계약 스케줄 생성 ...")
    sched = build_contract_schedule(bdays, prefix=commodity_prefix)

    # Step 5: 롤 비중 + MDE
    print("[5/8] 롤 비중 계산 (MDE 처리 포함) ...")
    rw = calculate_roll_weights(sched, prices)
    n_mde = int(rw["is_mde"].sum())
    if n_mde:
        print(f"      → MDE 감지: {n_mde}개 영업일에서 롤 유예 적용")
    else:
        print("      → MDE 없음")

    # Step 6: WAV / PWAV / ER
    print("[6/8] WAV / PWAV → DER → ER 인덱스 계산 ...")
    wp  = calculate_wav_pwav(bdays, sched, rw, prices, lot_size)
    er  = calculate_er_index(wp, base_level=base_er_level)

    # Step 7: IR / TR
    print("[7/8] T-Bill IR → TR 인덱스 계산 ...")
    ir  = calculate_ir(bdays, tbill)
    tr  = calculate_tr_index(er, ir, base_level=base_tr_level)

    # Step 8: 포지션 추적
    print("[8/8] 일일 선물 포지션 및 롤오버 이벤트 생성 ...")
    positions = calculate_positions(sched, rw, prices, notional=notional, fix_contracts=fix_contracts, lot_size=lot_size)
    n_rolls = int(positions["is_roll_day"].sum())
    n_ticker_changes = int(positions["lead_ticker_changed"].sum())
    print(f"      → 롤오버 발생일: {n_rolls}일 | Lead 계약 교체: {n_ticker_changes}회")
    if positions_output_path:
        positions.to_csv(positions_output_path)
        print(f"      → 포지션 파일 저장: {positions_output_path}")

    # 결과 통합
    result = sched.copy()
    result["lead_weight"] = rw["lead_weight"]
    result["next_weight"]  = rw["next_weight"]
    result["is_mde"]       = rw["is_mde"]
    result["wav"]          = wp["wav"]
    result["pwav"]         = wp["pwav"]
    result["der"]          = (wp["wav"] / wp["pwav"]) - 1.0
    result["er_index"]     = er
    result["ir"]           = ir
    result["tr_index"]     = tr

    result = result[
        [
            "lead_ticker", "next_ticker", "bd_of_month",
            "lead_weight", "next_weight", "is_mde",
            "wav", "pwav", "der",
            "er_index", "ir", "tr_index",
        ]
    ]

    # 저장
    if output_path:
        result.to_csv(output_path)
        print(f"\n결과 저장: {output_path}")

    # 요약
    print("\n" + "=" * 60)
    print("Bloomberg WTI Crude Oil Single Index — 계산 완료")
    print("=" * 60)
    print(f"기간      : {result.index[0].date()} ~ {result.index[-1].date()}")
    print(f"영업일 수 : {len(result):,}일")
    print(
        f"ER 인덱스 : {result['er_index'].iloc[0]:.8f} → {result['er_index'].iloc[-1]:.8f}"
    )
    print(
        f"TR 인덱스 : {result['tr_index'].iloc[0]:.8f} → {result['tr_index'].iloc[-1]:.8f}"
    )

    return result


# ============================================================
# 파트 11: 더미 데이터 생성 및 파이프라인 테스트
# 실제 데이터가 없는 경우 코드 구조와 계산 로직을 검증하기 위한
# 합성(synthetic) 선물 가격 및 T-Bill 금리 데이터를 생성합니다.
# 이 섹션은 실제 운영 시 제거하거나 별도 테스트 파일로 분리하세요.
# ============================================================

def create_dummy_data(
    start: str = "2010-01-04",
    end: str = "2010-06-30",
    seed: int = 42,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    테스트용 합성 선물 가격 및 T-Bill 금리 데이터를 생성합니다.

    선물 가격: 월별 계약(CLG10~CLZ10, CLF11)에 대한 랜덤 워크
    T-Bill   : 주간 고할인율 (퍼센트 단위, 0.05~0.25% 범위)

    Returns
    -------
    futures_df : pd.DataFrame  (Wide format)
    tbill_df   : pd.DataFrame  (date, tbr 열)
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, end=end)

    # 2010년 계약: CLG10 ~ CLZ10, 다음 해 CLF11, CLG11
    contracts_2010 = [f"CL{c}10" for c in ALL_MONTH_CODES[1:]]   # G~Z
    contracts_2011 = [f"CL{c}11" for c in ALL_MONTH_CODES[:3]]   # F, G, H

    price_data: dict[str, np.ndarray] = {}
    base = 80.0
    for i, tkr in enumerate(contracts_2010 + contracts_2011):
        # 콘탱고 구조 반영: 원월물일수록 약간 높은 가격
        level = base * (1 + i * 0.002)
        rets  = rng.normal(0.0, 0.015, len(dates))
        price_data[tkr] = level * np.exp(np.cumsum(rets))

    futures_df = pd.DataFrame(price_data, index=dates)

    # 주간 T-Bill 금리 (퍼센트 단위)
    weekly = pd.date_range(start=start, end=end, freq="W-MON")
    tbill_df = pd.DataFrame(
        {"date": weekly, "tbr": rng.uniform(0.05, 0.25, len(weekly))}
    )

    return futures_df, tbill_df


# ============================================================
# 진입점 (Entry Point)
# 스크립트를 직접 실행하면 더미 데이터로 파이프라인 동작을 검증합니다.
# 실제 데이터 사용 시 아래 예시처럼 소스를 교체하여 실행하세요.
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Bloomberg WTI Crude Oil Single ER/TR Index Replication")
    print("=" * 60)

    # --------------------------------------------------------
    # 실제 데이터 파일 경로 설정
    # --------------------------------------------------------
    # 선물 결제가격: 단일 파일 또는 연도별 파일 목록으로 지정 가능
    #   - 단일 파일 (2016년 테스트용):
    FUTURES_SOURCE = "cl_settlements_2016-2026.csv"
    #   - 여러 연도 파일을 합칠 경우 리스트로 지정:
    #     FUTURES_SOURCE = [
    #         "cl_settlements_2016.csv",
    #         "cl_settlements_2017.csv",
    #         # ... 추가 연도 파일
    #     ]

    # T-Bill 금리: Moody's Analytics CSV 형식
    TBILL_SOURCE = "13 Week Treasury Auction Rate High.csv"

    # 계산 기간 (보유 중인 선물 데이터 범위에 맞게 조정)
    START_DATE = "2016-01-02"   
    END_DATE   = "2026-03-18"   

    # --------------------------------------------------------
    # Bloomberg 공식 Base Date(2010-01-04)가 아닌 날짜부터
    # 계산을 시작하는 경우, Bloomberg 터미널에서 START_DATE의
    # 실제 인덱스 레벨을 조회하여 아래 값을 교체하세요.
    #
    # 조회 방법 (Bloomberg 터미널):
    #   ER: BCLSE Index  → 해당 날짜 값
    #   TR: BCLST Index  → 해당 날짜 값
    #
    # 예) START_DATE = "2016-01-04" 이면:
    #   BASE_ER = <Bloomberg에서 조회한 2016-01-04의 BCOMCLEP 값>
    #   BASE_TR = <Bloomberg에서 조회한 2016-01-04의 BCOMCLET 값>
    # --------------------------------------------------------
    BASE_ER = 100   # ← Bloomberg에서 조회한 START_DATE의 ER 레벨로 교체
    BASE_TR = 100   # ← Bloomberg에서 조회한 START_DATE의 TR 레벨로 교체

    # --------------------------------------------------------
    # 포지션 추적 설정
    # notional: 가상 운용 자산 규모 (USD). 계약 수를 계산하려면 값 설정.
    #   None이면 포지션 파일에 가격·비중·롤오버 정보만 기록되고 계약 수는 제외.
    # --------------------------------------------------------
    NOTIONAL = 10_000_000  # 예시: $10,000,000. 계약 수 불필요 시 None으로 설정.
    # FIX_CONTRACTS: 계약 수 고정 방식 선택
    #   False: Notional 고정 — 매일 notional/(blended_price×1000)으로 계약 수 재계산
    #          (전략 간 동일 자본 투입 비교에 적합)
    #   True : 계약 수 고정 — 첫 영업일의 계약 수를 전 기간 유지, notional_value가 변동
    #          (실제 ETN 헷징 운용 방식에 가장 근접)
    FIX_CONTRACTS = False

    result = run_index_calculation(
        futures_prices_source=FUTURES_SOURCE,
        tbill_rates_source=TBILL_SOURCE,
        start_date=START_DATE,
        end_date=END_DATE,
        base_er_level=BASE_ER,
        base_tr_level=BASE_TR,
        output_path="wti_index_output_2016-2026.csv",
        notional=NOTIONAL,
        fix_contracts=FIX_CONTRACTS,
        positions_output_path="wti_positions_2016-2026.csv",
    )

    cols = ["lead_ticker", "next_ticker", "lead_weight", "next_weight",
            "der", "er_index", "ir", "tr_index"]
    print("\n--- 결과 샘플 (처음 15행) ---")
    print(result[cols].head(15).to_string())

    # --------------------------------------------------------
    # 전체 기간(2016~2026) 실행 예시:
    #
    # result = run_index_calculation(
    #     futures_prices_source=[
    #         "cl_settlements_2016.csv",
    #         "cl_settlements_2017_2026.csv",   # 추후 추가 파일
    #     ],
    #     tbill_rates_source="13 Week Treasury Auction Rate High.csv",
    #     start_date="2016-01-04",
    #     end_date="2026-03-19",
    #     output_path="wti_index_output_full.csv",
    # )
    # --------------------------------------------------------
