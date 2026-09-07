"""
ws_1_index.py — WTI Crude Oil Index with DRA Dynamic Roll

index_calculator.py에 DRA(3) 동적 롤오버 방법론을 추가한 버전.

설계 원칙:
  - 파트 1~10 : index_calculator.py와 완전히 동일 (상수·캘린더·계약 스케줄·롤 비중·
                데이터 로딩·WAV/PWAV·ER·T-Bill IR·TR·포지션 트래킹)
  - 파트 11   : 정적 메인 파이프라인 run_index_calculation() — index_calculator.py 동일
  - 파트 12~16: DRA 전용 함수 및 파이프라인 추가
                  build_dra_contract_schedule()   — DRA 계약 스케줄
                  calculate_dra_roll_weights()    — DRA 롤 비중
                  calculate_dra_positions()       — lot_size 포함 계약 수 계산
                  _compute_monthly_comparison()   — 정적 vs 동적 IRY 비교
                  run_dra_index_calculation()     — DRA 전체 파이프라인

버그 수정:
  [ws_index.py 버그] calculate_dra_positions()에서
    total_contracts = notional / blended_price          ← lot_size 누락
  → total_contracts = notional / (blended_price * lot_size)  ← 수정 완료

  [ws_index.py 버그] run_dra_index_calculation()에 tbill_rates_source 파라미터 없음
  → 추가하여 ER/TR 인덱스를 정적 파이프라인과 동일하게 계산

Base Date : 2010-01-04  |  Base Level : 100.0 (both ER and TR)
Commodity : WTI Crude Oil  |  Bloomberg Code : CL  |  Exchange : CME/NYMEX
"""

from __future__ import annotations

import warnings
from typing import Optional, Tuple

import numpy as np
import pandas as pd


# ============================================================
# 파트 1: 상수 및 설정 정의
# index_calculator.py와 완전히 동일
# ============================================================

BASE_DATE: pd.Timestamp = pd.Timestamp("2010-01-04")
BASE_ER_LEVEL: float = 100.0
BASE_TR_LEVEL: float = 100.0

LOT_SIZE: float = 1000.0  # WTI 원유 선물 계약 단위: 1계약 = 1,000배럴
                           # 주의: DER = WAV/PWAV - 1 계산 시 L이 분자·분모에서 약분되므로
                           # ER·TR 인덱스 레벨 자체에는 영향이 없음. WAV/PWAV 절대값에만 반영됨.
CIM: float = 1.0        # Commodity Index Multiplier (연간 리밸런싱 없음 → 항상 1)

MONTH_TO_LEAD_CODE: dict[int, str] = {
    1: "G", 2: "H", 3: "J", 4: "K", 5: "M", 6: "N",
    7: "Q", 8: "U", 9: "V", 10: "X", 11: "Z", 12: "F",
}

FUTURES_CODE_TO_MONTH: dict[str, int] = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

ALL_MONTH_CODES: list[str] = ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]

ROLL_START_BD: int = 6
ROLL_END_BD: int = 10
WEIGHT_STEP: float = 0.20

TBILL_MATURITY: int = 91
TBILL_DAY_COUNT: int = 360

COMMODITY_PREFIX: str = "CL"


# ============================================================
# 파트 2: CME 영업일 캘린더 생성
# index_calculator.py와 완전히 동일
# ============================================================

def get_cme_business_days(
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DatetimeIndex:
    """지정 기간의 CME 영업일 목록을 반환합니다."""
    try:
        import pandas_market_calendars as mcal  # type: ignore

        cal = mcal.get_calendar("CMEGlobex_CL")
        schedule = cal.schedule(start_date=start_date, end_date=end_date)
        bdays = mcal.date_range(schedule, frequency="1D").normalize().tz_localize(None)
        return bdays
    except (ImportError, RuntimeError, TypeError) as e:
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
        holidays.append(obs(pd.Timestamp(yr, 1, 1)))
        holidays.append(pd.Timestamp(yr, 1, 1) + relativedelta(weekday=MO(3)))
        holidays.append(pd.Timestamp(yr, 2, 1) + relativedelta(weekday=MO(3)))
        holidays.append(pd.Timestamp(easter(yr)) - pd.Timedelta(days=2))
        holidays.append(pd.Timestamp(yr, 5, 31) + relativedelta(weekday=MO(-1)))
        if yr >= 2022:
            holidays.append(obs(pd.Timestamp(yr, 6, 19)))
        holidays.append(obs(pd.Timestamp(yr, 7, 4)))
        holidays.append(pd.Timestamp(yr, 9, 1) + relativedelta(weekday=MO(1)))
        holidays.append(pd.Timestamp(yr, 11, 1) + relativedelta(weekday=TH(4)))
        holidays.append(obs(pd.Timestamp(yr, 12, 25)))

    return pd.DatetimeIndex(sorted(set(holidays)))


# ============================================================
# 파트 3: 계약 티커 및 스케줄 생성
# index_calculator.py와 완전히 동일
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
# index_calculator.py와 완전히 동일 (정적 롤오버용)
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
    pending: int = 0
    last_ym: tuple = (0, 0)

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

        if ym != last_ym:
            cur_lead_w = 1.0
            cur_next_w = 0.0
            pending = 0
            last_ym = ym

        lead_t: str = row["lead_ticker"]
        next_t: str = row["next_ticker"]

        is_mde: bool = False

        if ROLL_START_BD <= bd <= ROLL_END_BD:
            if not has_price(lead_t, date) or not has_price(next_t, date):
                is_mde = True
                pending += 1
            else:
                rolls = 1 + pending
                cur_next_w = min(1.0, cur_next_w + WEIGHT_STEP * rolls)
                cur_lead_w = max(0.0, 1.0 - cur_next_w)
                pending = 0

        elif bd > ROLL_END_BD and pending > 0:
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
# index_calculator.py와 완전히 동일
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

    Returns
    -------
    pd.DataFrame  (index=거래일, columns=내부 2자리연도 티커, dtype=float)
    """
    if isinstance(source, list):
        frames = [pd.read_csv(f) for f in source]
        df = pd.concat(frames, ignore_index=True)
    elif isinstance(source, str):
        df = pd.read_csv(source)
    else:
        df = source.copy()

    for col in [trade_date_col, symbol_col, price_col, expiration_col]:
        if col not in df.columns:
            raise ValueError(
                f"필수 컬럼 '{col}'이(가) 데이터에 없습니다. "
                f"실제 컬럼: {list(df.columns)}"
            )

    df[trade_date_col] = pd.to_datetime(df[trade_date_col])

    df[expiration_col] = (
        pd.to_datetime(df[expiration_col], utc=True).dt.tz_localize(None)
    )

    # [버그 수정] Symbol 월 코드 + 인도 연도(delivery year)로 내부 티커 생성
    # WTI 선물은 인도월(delivery month) 전달에 만기 → 만기월로 코드 유도 시 1달 어긋남
    # 예) CLF7(1월 인도) 만기=2016-12: "F"+2016 → CLF16 충돌 → 인도 연도 보정 필요
    # 해결: delivery_month > exp_month이면 같은 해, 아니면(F계약 등) 다음 해로 보정
    df["_month_code"] = df[symbol_col].str.extract(r"^CL([A-Z])")[0]
    exp_year  = df[expiration_col].dt.year
    exp_month = df[expiration_col].dt.month
    delivery_month_num = df["_month_code"].map(FUTURES_CODE_TO_MONTH)
    delivery_year = exp_year.where(delivery_month_num > exp_month, exp_year + 1)
    df["_internal_ticker"] = (
        COMMODITY_PREFIX + df["_month_code"] + delivery_year.astype(str).str[-2:]
    )

    df[price_col] = pd.to_numeric(df[price_col], errors="coerce")
    if df[price_col].isna().all():
        raise ValueError("결제가격 컬럼에 유효한 숫자 데이터가 없습니다.")

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

    Returns
    -------
    pd.Series  (index=월초 날짜, values=소수점 금리)
    """
    if isinstance(source, str):
        raw = pd.read_csv(source, header=None, skiprows=5)
    else:
        raw = source.copy()
        raw.columns = range(len(raw.columns))

    def _parse_tbill_date(date_str: str) -> Optional[pd.Timestamp]:
        s = str(date_str).strip()
        parts = s.split("-") if "-" in s else s.split(".")
        if len(parts) != 2:
            return None
        a, b = parts[0].strip(), parts[1].strip()
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
    s = s[s.index.notna()]
    s = s.dropna()
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
# index_calculator.py와 완전히 동일
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

    Returns
    -------
    pd.DataFrame  columns: wav, pwav
    """
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

        ylrw: float = roll_weights.at[prev, "lead_weight"]
        ynrw: float = roll_weights.at[prev, "next_weight"]

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
# index_calculator.py와 완전히 동일
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
# index_calculator.py와 완전히 동일
# ============================================================

def calculate_ir(
    business_days: pd.DatetimeIndex,
    tbill_aligned: pd.Series,
) -> pd.Series:
    """
    T-Bill 일일 수익률(IR_t)을 계산합니다.

    Bloomberg 공식:
      IR_t = [1 / (1 - (91/360) * TBR_{t-1})]^(D/91) - 1
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
# index_calculator.py와 완전히 동일
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
# index_calculator.py와 완전히 동일
# ============================================================

def calculate_positions(
    contract_schedule: pd.DataFrame,
    roll_weights: pd.DataFrame,
    prices_df: pd.DataFrame,
    notional: Optional[float] = None,
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
    lot_size          : 계약당 배럴 수 (WTI = 1,000)

    포지션 계산 공식:
        blended_price   = lead_weight × lead_price + next_weight × next_price
        total_contracts = notional / (blended_price × lot_size)
        lead_contracts  = lead_weight × total_contracts
        next_contracts  = next_weight × total_contracts

    Returns
    -------
    pd.DataFrame  (index=영업일)
    """
    bdays = contract_schedule.index
    prices_ffill = prices_df.reindex(bdays).ffill()

    rows = []
    prev_lead_ticker: Optional[str] = None
    prev_lead_w: Optional[float] = None
    prev_lead_c: float = 0.0
    prev_next_c: float = 0.0

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

        lp  = lead_price if not np.isnan(lead_price) else 0.0
        np_ = next_price if not np.isnan(next_price) else 0.0
        blended = lead_w * lp + next_w * np_

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
                total_c = notional / (blended * lot_size)
                lead_c  = lead_w * total_c
                next_c  = next_w * total_c
            else:
                total_c = lead_c = next_c = np.nan

            lead_traded = (lead_c - prev_lead_c) if not np.isnan(lead_c) else np.nan
            next_traded = (next_c - prev_next_c) if not np.isnan(next_c) else np.nan

            row["total_contracts"]        = total_c
            row["lead_contracts"]         = lead_c
            row["next_contracts"]         = next_c
            row["lead_contracts_traded"]  = lead_traded
            row["next_contracts_traded"]  = next_traded

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
    extra_cols = (
        ["total_contracts",
         "lead_contracts", "next_contracts",
         "lead_contracts_traded", "next_contracts_traded"]
        if notional is not None else []
    )

    return pd.DataFrame(rows, index=bdays)[base_cols + extra_cols]


# ============================================================
# 파트 11: 정적 메인 파이프라인 오케스트레이터
# index_calculator.py와 완전히 동일
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
    positions_output_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    Bloomberg WTI Crude Oil Single ER/TR 인덱스 전체 계산 파이프라인.
    정적 롤오버(Lead→Next 고정) 방식. index_calculator.py와 완전히 동일.

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

    print(f"[1/8] CME 영업일 캘린더 생성 ({start_date} ~ {end_date}) ...")
    bdays = get_cme_business_days(start_ts, end_ts)
    print(f"      → {len(bdays):,}개 CME 영업일")

    print("[2/8] 선물 결제가격 데이터 로딩 ...")
    prices = load_futures_prices(futures_prices_source)
    print(f"      → {len(prices):,}행, {len(prices.columns)}개 계약 티커")

    print("[3/8] T-Bill 금리 데이터 로딩 및 영업일 정렬 ...")
    tbill_raw = load_tbill_rates(tbill_rates_source)
    tbill = align_tbill(tbill_raw, bdays)
    print(f"      → {len(tbill):,}개 영업일 금리 적용")

    print("[4/8] Lead/Next 계약 스케줄 생성 ...")
    sched = build_contract_schedule(bdays, prefix=commodity_prefix)

    print("[5/8] 롤 비중 계산 (MDE 처리 포함) ...")
    rw = calculate_roll_weights(sched, prices)
    n_mde = int(rw["is_mde"].sum())
    if n_mde:
        print(f"      → MDE 감지: {n_mde}개 영업일에서 롤 유예 적용")
    else:
        print("      → MDE 없음")

    print("[6/8] WAV / PWAV → DER → ER 인덱스 계산 ...")
    wp  = calculate_wav_pwav(bdays, sched, rw, prices, lot_size)
    er  = calculate_er_index(wp, base_level=base_er_level)

    print("[7/8] T-Bill IR → TR 인덱스 계산 ...")
    ir  = calculate_ir(bdays, tbill)
    tr  = calculate_tr_index(er, ir, base_level=base_tr_level)

    print("[8/8] 일일 선물 포지션 및 롤오버 이벤트 생성 ...")
    positions = calculate_positions(sched, rw, prices, notional=notional, lot_size=lot_size)
    n_rolls = int(positions["is_roll_day"].sum())
    n_ticker_changes = int(positions["lead_ticker_changed"].sum())
    print(f"      → 롤오버 발생일: {n_rolls}일 | Lead 계약 교체: {n_ticker_changes}회")
    if positions_output_path:
        positions.to_csv(positions_output_path)
        print(f"      → 포지션 파일 저장: {positions_output_path}")

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

    if output_path:
        result.to_csv(output_path)
        print(f"\n결과 저장: {output_path}")

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
# 파트 12: DRA 기반 계약 스케줄 생성
# ws_index.py와 동일
# ============================================================

def _get_contracts_for_date(prices_wide: pd.DataFrame, expiration_map: dict,
                             date: pd.Timestamp, n: int = 12):
    """특정 날짜 기준 만기 오름차순 월물 리스트 반환. [(심볼, 가격, 만기일), ...]"""
    if date not in prices_wide.index:
        return []
    row = prices_wide.loc[date]
    result = []
    for symbol, expiry in expiration_map.items():
        if pd.isna(expiry) or expiry <= date:
            continue
        if symbol not in row.index or pd.isna(row[symbol]):
            continue
        result.append((symbol, row[symbol], expiry))
    result.sort(key=lambda x: x[2])
    return result[:n]


def _calculate_iry(contracts):
    """연속 페어(Consecutive Pair) IRY 계산, 내림차순 정렬."""
    if len(contracts) < 2:
        return []
    iry_list = []
    for i in range(len(contracts) - 1):
        sym_prev, price_prev, exp_prev = contracts[i]
        sym_curr, price_curr, exp_curr = contracts[i + 1]
        if price_curr == 0:
            continue
        interval_d = (exp_curr.year - exp_prev.year) * 12 + (exp_curr.month - exp_prev.month)
        if interval_d <= 0:
            interval_d = 1
        iry = (price_prev - price_curr) / (price_curr * interval_d)
        iry_list.append((sym_curr, iry))
    iry_list.sort(key=lambda x: x[1], reverse=True)
    return iry_list


def _run_dra(ranked_contracts, current_contract, rank_order=3):
    """DRA(3) — Dynamic Roll Parity Principle 적용."""
    top_n = min(rank_order, len(ranked_contracts))
    optimum_set = [sym for sym, _ in ranked_contracts[:top_n]]
    if not optimum_set:
        return {"roll_in": current_contract, "roll_required": False, "selected_iry": float('nan')}
    best_sym = optimum_set[0]
    best_iry = ranked_contracts[0][1]
    if current_contract in optimum_set:
        iry_map = {sym: iry for sym, iry in ranked_contracts[:top_n]}
        return {"roll_in": current_contract, "roll_required": False,
                "selected_iry": iry_map[current_contract]}
    return {"roll_in": best_sym, "roll_required": True, "selected_iry": best_iry}


def build_dra_contract_schedule(
    business_days: pd.DatetimeIndex,
    prices_wide: pd.DataFrame,
    expiration_map: dict,
    prefix: str = COMMODITY_PREFIX,
) -> pd.DataFrame:
    """
    DRA(3) 기반 계약 스케줄 생성.

    Returns
    -------
    pd.DataFrame  index=business_days
        columns: lead_ticker, next_ticker, bd_of_month, roll_required
    """
    months = sorted(set((d.year, d.month) for d in business_days))

    first_yr, first_mo = months[0]
    lc = MONTH_TO_LEAD_CODE[first_mo]
    c_yr = first_yr + 1 if FUTURES_CODE_TO_MONTH[lc] <= first_mo else first_yr
    current_holding = f"{prefix}{lc}{str(c_yr)[-2:]}"

    month_decisions: dict = {}
    for yr, mo in months:
        month_bdays = [d for d in business_days if d.year == yr and d.month == mo]
        if len(month_bdays) < ROLL_START_BD:
            month_decisions[(yr, mo)] = {
                "roll_required": False,
                "roll_out": current_holding,
                "roll_in": current_holding,
            }
            continue

        det_date = month_bdays[ROLL_START_BD - 1]
        contracts = _get_contracts_for_date(prices_wide, expiration_map, det_date, n=12)
        if len(contracts) < 2:
            month_decisions[(yr, mo)] = {
                "roll_required": False,
                "roll_out": current_holding,
                "roll_in": current_holding,
            }
            continue

        ranked = _calculate_iry(contracts)
        dra = _run_dra(ranked, current_holding)

        month_decisions[(yr, mo)] = {
            "roll_required": dra["roll_required"],
            "roll_out": current_holding,
            "roll_in": dra["roll_in"],
        }
        current_holding = dra["roll_in"]

    records = []
    for d in business_days:
        yr, mo = d.year, d.month
        month_bdays = [b for b in business_days if b.year == yr and b.month == mo]
        bd = month_bdays.index(d) + 1

        dec = month_decisions.get((yr, mo), {})
        roll_required = dec.get("roll_required", False)
        roll_out = dec.get("roll_out", current_holding)
        roll_in  = dec.get("roll_in",  current_holding)

        if roll_required:
            lead = roll_out
            nxt  = roll_in
        else:
            lead = roll_out
            nxt  = roll_out

        records.append({
            "lead_ticker":   lead,
            "next_ticker":   nxt,
            "bd_of_month":   bd,
            "roll_required": roll_required,
        })

    df = pd.DataFrame(records, index=business_days)
    return df


# ============================================================
# 파트 13: DRA 롤 비중 계산
# ws_index.py와 동일
# ============================================================

def calculate_dra_roll_weights(
    dra_schedule: pd.DataFrame,
    prices_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    DRA 기반 롤 비중 계산.
    roll_required=False 달은 lead_weight=1.0 유지.
    MDE 로직은 roll_required=True 달에만 적용.

    Returns
    -------
    pd.DataFrame  columns: lead_weight, next_weight, is_mde
    """
    lead_w_dict: dict = {}
    next_w_dict: dict = {}
    mde_dict:    dict = {}

    cur_lead_w: float = 1.0
    cur_next_w: float = 0.0
    pending:    int   = 0
    last_ym: tuple    = (0, 0)

    def has_price(ticker: str, date: pd.Timestamp) -> bool:
        if ticker not in prices_df.columns:
            return False
        if date not in prices_df.index:
            return False
        return not np.isnan(prices_df.at[date, ticker])

    for date in dra_schedule.index:
        row = dra_schedule.loc[date]
        bd            = int(row["bd_of_month"])
        roll_required = bool(row["roll_required"])
        ym            = (date.year, date.month)

        if ym != last_ym:
            cur_lead_w = 1.0
            cur_next_w = 0.0
            pending    = 0
            last_ym    = ym

        lead_t = row["lead_ticker"]
        next_t = row["next_ticker"]
        is_mde = False

        if roll_required and ROLL_START_BD <= bd <= ROLL_END_BD:
            if not has_price(lead_t, date) or not has_price(next_t, date):
                is_mde  = True
                pending += 1
            else:
                rolls      = 1 + pending
                cur_next_w = min(1.0, cur_next_w + WEIGHT_STEP * rolls)
                cur_lead_w = max(0.0, 1.0 - cur_next_w)
                pending    = 0
        elif roll_required and bd > ROLL_END_BD and pending > 0:
            if not has_price(lead_t, date) or not has_price(next_t, date):
                is_mde  = True
                pending += 1
            else:
                cur_next_w = min(1.0, cur_next_w + WEIGHT_STEP * pending)
                cur_lead_w = max(0.0, 1.0 - cur_next_w)
                pending    = 0

        lead_w_dict[date] = cur_lead_w
        next_w_dict[date] = cur_next_w
        mde_dict[date]    = is_mde

    return pd.DataFrame(
        {"lead_weight": lead_w_dict, "next_weight": next_w_dict, "is_mde": mde_dict}
    )


# ============================================================
# 파트 14: DRA 포지션 트래킹
# calculate_positions()와 동일한 구조 + lot_size 버그 수정
#
# [수정 내용]
# ws_index.py: total_contracts = notional / blended_price  (lot_size 누락)
# ws_1_index.py: total_contracts = notional / (blended_price * lot_size)  (수정)
# ============================================================

def calculate_dra_positions(
    contract_schedule: pd.DataFrame,
    roll_weights: pd.DataFrame,
    prices_df: pd.DataFrame,
    notional: Optional[float] = None,
    lot_size: float = LOT_SIZE,
) -> pd.DataFrame:
    """
    DRA 기반 포지션 트래킹. calculate_positions()와 동일한 인터페이스.

    포지션 계산 공식 (calculate_positions와 동일):
        blended_price   = lead_weight × lead_price + next_weight × next_price
        total_contracts = notional / (blended_price × lot_size)   ← lot_size 포함
        lead_contracts  = lead_weight × total_contracts
        next_contracts  = next_weight × total_contracts

    Returns
    -------
    pd.DataFrame  (index=영업일)
        항상 포함: lead_ticker, next_ticker, bd_of_month,
                   lead_weight, next_weight, is_mde, roll_required,
                   lead_price, next_price, blended_price,
                   is_roll_day, lead_ticker_changed
        notional 제공 시 추가: total_contracts, lead_contracts, next_contracts,
                               lead_contracts_traded, next_contracts_traded
    """
    bdays = contract_schedule.index
    prices_ffill = prices_df.reindex(bdays).ffill()

    rows = []
    prev_lead_ticker: Optional[str] = None
    prev_lead_w: Optional[float] = None
    prev_lead_c: float = 0.0
    prev_next_c: float = 0.0

    for date in bdays:
        srow = contract_schedule.loc[date]
        rrow = roll_weights.loc[date]

        lead_t: str        = srow["lead_ticker"]
        next_t: str        = srow["next_ticker"]
        bd: int            = int(srow["bd_of_month"])
        roll_req: bool     = bool(srow["roll_required"])
        lead_w: float      = rrow["lead_weight"]
        next_w: float      = rrow["next_weight"]
        is_mde: bool       = rrow["is_mde"]

        def _px(ticker: str) -> float:
            if ticker in prices_ffill.columns:
                v = prices_ffill.at[date, ticker]
                return float(v) if not np.isnan(v) else np.nan
            return np.nan

        lead_price: float = _px(lead_t)
        next_price: float = _px(next_t)

        lp  = lead_price if not np.isnan(lead_price) else 0.0
        np_ = next_price if not np.isnan(next_price) else 0.0
        blended = lead_w * lp + next_w * np_

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
            "roll_required":       roll_req,
            "lead_price":          lead_price,
            "next_price":          next_price,
            "blended_price":       blended,
            "is_roll_day":         is_roll_day,
            "lead_ticker_changed": lead_ticker_changed,
        }

        if notional is not None:
            if blended > 0.0:
                # [수정] lot_size 포함 — ws_index.py 버그 수정
                total_c = notional / (blended * lot_size)
                lead_c  = lead_w * total_c
                next_c  = next_w * total_c
            else:
                total_c = lead_c = next_c = np.nan

            lead_traded = (lead_c - prev_lead_c) if not np.isnan(lead_c) else np.nan
            next_traded = (next_c - prev_next_c) if not np.isnan(next_c) else np.nan

            row["total_contracts"]        = total_c
            row["lead_contracts"]         = lead_c
            row["next_contracts"]         = next_c
            row["lead_contracts_traded"]  = lead_traded
            row["next_contracts_traded"]  = next_traded

            prev_lead_c = lead_c if not np.isnan(lead_c) else prev_lead_c
            prev_next_c = next_c if not np.isnan(next_c) else prev_next_c

        rows.append(row)
        prev_lead_ticker = lead_t
        prev_lead_w      = lead_w

    base_cols = [
        "lead_ticker", "next_ticker", "bd_of_month",
        "lead_weight", "next_weight", "is_mde", "roll_required",
        "lead_price", "next_price", "blended_price",
        "is_roll_day", "lead_ticker_changed",
    ]
    extra_cols = (
        ["total_contracts",
         "lead_contracts", "next_contracts",
         "lead_contracts_traded", "next_contracts_traded"]
        if notional is not None else []
    )

    return pd.DataFrame(rows, index=bdays)[base_cols + extra_cols]


# ============================================================
# 파트 15: 월별 비교 계산 (정적 vs DRA IRY·체결금액)
# ws_index.py의 _compute_monthly_comparison과 동일
# ============================================================

def _compute_monthly_comparison(
    bdays: pd.DatetimeIndex,
    prices_wide: pd.DataFrame,
    expiration_map: dict,
    dra_sched: pd.DataFrame,
    prefix: str = COMMODITY_PREFIX,
) -> list:
    """월별 정적(ETN BM) vs 동적(DRA) 비교 결과 계산."""
    months = sorted(set((d.year, d.month) for d in bdays))

    results = []
    for yr, mo in months:
        month_bdays = [d for d in bdays if d.year == yr and d.month == mo]
        if len(month_bdays) < ROLL_START_BD:
            continue

        det_date = month_bdays[ROLL_START_BD - 1]

        month_rows = dra_sched[(dra_sched.index.year == yr) & (dra_sched.index.month == mo)]
        if month_rows.empty:
            continue

        roll_required    = bool(month_rows["roll_required"].iloc[0])
        dynamic_roll_in  = month_rows["next_ticker"].iloc[0]
        dynamic_roll_out = month_rows["lead_ticker"].iloc[0]

        contracts = _get_contracts_for_date(prices_wide, expiration_map, det_date, n=12)
        if len(contracts) < 2:
            continue

        m1_price = contracts[0][1]
        m2_price = contracts[1][1]
        market_condition = "Backwardation" if m1_price > m2_price else "Contango"

        ranked = _calculate_iry(contracts)
        dra_res = _run_dra(ranked, dynamic_roll_out)
        dynamic_iry  = dra_res["selected_iry"]
        optimum_set  = [s for s, _ in ranked[:3]]

        lead_code = MONTH_TO_LEAD_CODE[mo]
        lead_idx  = ALL_MONTH_CODES.index(lead_code)
        next_code = ALL_MONTH_CODES[(lead_idx + 1) % 12]

        def c_year(code):
            cm = FUTURES_CODE_TO_MONTH[code]
            return yr + 1 if cm <= mo else yr

        static_roll_out = f"{prefix}{lead_code}{str(c_year(lead_code))[-2:]}"
        static_roll_in  = f"{prefix}{next_code}{str(c_year(next_code))[-2:]}"

        sym_map = {sym: (px, exp) for sym, px, exp in contracts}
        static_iry = float('nan')
        if static_roll_out in sym_map and static_roll_in in sym_map:
            p_out, e_out = sym_map[static_roll_out]
            p_in,  e_in  = sym_map[static_roll_in]
            if p_in > 0:
                interval = (e_in.year - e_out.year) * 12 + (e_in.month - e_out.month)
                interval = max(interval, 1)
                static_iry = (p_out - p_in) / (p_in * interval)

        def _exec_amount(roll_in, roll_out, n=1000, cs=LOT_SIZE):
            roll_days = [month_bdays[i] for i in range(ROLL_START_BD - 1, min(ROLL_END_BD, len(month_bdays)))]
            avail = [d for d in roll_days
                     if roll_in in prices_wide.columns and d in prices_wide.index
                     and not pd.isna(prices_wide.at[d, roll_in])]
            if not avail:
                return 0.0
            w = 1.0 / len(avail)
            return sum(prices_wide.at[d, roll_in] * n * w * cs for d in avail)

        sta_amt = _exec_amount(static_roll_in,  static_roll_out)
        dyn_amt = _exec_amount(dynamic_roll_in, dynamic_roll_out) if roll_required else 0.0

        iry_saving = (
            (dynamic_iry - static_iry)
            if not (np.isnan(dynamic_iry) or np.isnan(static_iry))
            else float('nan')
        )

        results.append({
            "date":                     det_date,
            "year":                     yr,
            "month":                    mo,
            "market_condition":         market_condition,
            "static_roll_out_contract": static_roll_out,
            "static_roll_contract":     static_roll_in,
            "dynamic_roll_out_contract":dynamic_roll_out,
            "dynamic_roll_contract":    dynamic_roll_in,
            "roll_required":            roll_required,
            "optimum_set":              str(optimum_set),
            "static_iry":               static_iry,
            "dynamic_iry":              dynamic_iry,
            "iry_saving":               iry_saving,
            "static_execution_amount":  sta_amt,
            "dynamic_execution_amount": dyn_amt,
            "execution_amount_diff":    dyn_amt - sta_amt,
        })

    return results


# ============================================================
# 파트 16: 출력 파일 생성
# ws_index.py의 _save_all_outputs / _plot_charts와 동일
# ============================================================

def _save_all_outputs(monthly_results: list, positions: pd.DataFrame, output_dir: str):
    """
    monthly_comparison / annual_summary / cumulative_summary /
    dra_positions / comparison_chart 저장.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from pathlib import Path

    out = Path(output_dir)
    out.mkdir(exist_ok=True)

    monthly_df = pd.DataFrame(monthly_results)
    monthly_df["date"] = pd.to_datetime(monthly_df["date"])

    MONTHLY_KO = {
        "date":                     "롤결정일",
        "year":                     "연도",
        "month":                    "월",
        "market_condition":         "시장상황",
        "static_roll_out_contract": "정적_롤아웃_월물",
        "static_roll_contract":     "정적_롤인_월물",
        "dynamic_roll_out_contract":"동적_롤아웃_월물",
        "dynamic_roll_contract":    "동적_롤인_월물",
        "roll_required":            "롤실행여부",
        "optimum_set":              "최적월물셋(DRA)",
        "static_iry":               "정적_IRY",
        "dynamic_iry":              "동적_IRY",
        "iry_saving":               "IRY_절감액",
        "static_execution_amount":  "정적_체결금액(달러)",
        "dynamic_execution_amount": "동적_체결금액(달러)",
        "execution_amount_diff":    "체결금액_차이(달러)",
    }
    ANNUAL_KO = {
        "year":                   "연도",
        "static_iry_sum":         "정적_IRY_합계",
        "dynamic_iry_sum":        "동적_IRY_합계",
        "iry_saving_sum":         "IRY_절감액_합계",
        "static_amount_sum":      "정적_체결금액_합계(달러)",
        "dynamic_amount_sum":     "동적_체결금액_합계(달러)",
        "amount_saving_sum":      "체결금액_절감액(달러)",
        "saving_rate":            "절감률(%)",
        "contango_months":        "콘탱고_월수(시장상황)",
        "backwardation_months":   "백워데이션_월수(시장상황)",
        "dynamic_roll_executed":  "동적_실제롤실행횟수",
        "dynamic_roll_held":      "동적_월물유지횟수(거래없음)",
    }

    # 1. monthly_comparison.csv
    monthly_df.rename(columns=MONTHLY_KO).to_csv(
        out / "monthly_comparison.csv", index=False
    )
    print(f"  monthly_comparison.csv 저장 ({len(monthly_df)}행)")

    # 2. annual_summary.csv
    annual_rows = []
    for yr, grp in monthly_df.groupby("year"):
        annual_rows.append({
            "year":                  yr,
            "static_iry_sum":        grp["static_iry"].sum(),
            "dynamic_iry_sum":       grp["dynamic_iry"].sum(),
            "iry_saving_sum":        grp["iry_saving"].sum(),
            "static_amount_sum":     grp["static_execution_amount"].sum(),
            "dynamic_amount_sum":    grp["dynamic_execution_amount"].sum(),
            "amount_saving_sum":     grp["execution_amount_diff"].sum(),
            "saving_rate":           (
                grp["execution_amount_diff"].sum() / grp["static_execution_amount"].sum() * 100
                if grp["static_execution_amount"].sum() != 0 else float('nan')
            ),
            "contango_months":       (grp["market_condition"] == "Contango").sum(),
            "backwardation_months":  (grp["market_condition"] == "Backwardation").sum(),
            "dynamic_roll_executed": grp["roll_required"].sum(),
            "dynamic_roll_held":     (~grp["roll_required"]).sum(),
        })
    pd.DataFrame(annual_rows).rename(columns=ANNUAL_KO).to_csv(
        out / "annual_summary.csv", index=False
    )
    print(f"  annual_summary.csv 저장 ({len(annual_rows)}행)")

    # 3. cumulative_summary.csv
    tot_si  = monthly_df["static_iry"].sum()
    tot_di  = monthly_df["dynamic_iry"].sum()
    tot_sa  = monthly_df["static_execution_amount"].sum()
    tot_da  = monthly_df["dynamic_execution_amount"].sum()
    iry_sav = tot_di - tot_si
    amt_sav = tot_da - tot_sa
    sr      = amt_sav / tot_sa * 100 if tot_sa else float('nan')
    tm      = len(monthly_df)
    de      = monthly_df["roll_required"].sum()
    dh      = (~monthly_df["roll_required"]).sum()
    ct      = (monthly_df["market_condition"] == "Contango").sum()
    bw      = (monthly_df["market_condition"] == "Backwardation").sum()
    dce     = ((monthly_df["market_condition"] == "Contango")      & monthly_df["roll_required"]).sum()
    dbe     = ((monthly_df["market_condition"] == "Backwardation") & monthly_df["roll_required"]).sum()

    cumul_rows = [
        {"항목": "분석 기간 총 월수",               "정적(ETN벤치마크)": str(tm),                "동적(DRA전략)": str(tm)},
        {"항목": "총 롤오버 IRY 합계",               "정적(ETN벤치마크)": f"{tot_si:.4f}",        "동적(DRA전략)": f"{tot_di:.4f}"},
        {"항목": "총 체결 금액 ($)",                 "정적(ETN벤치마크)": f"${tot_sa:,.0f}",      "동적(DRA전략)": f"${tot_da:,.0f}"},
        {"항목": "IRY 절감액 합계",                  "정적(ETN벤치마크)": "-",                    "동적(DRA전략)": f"{iry_sav:.4f}"},
        {"항목": "체결 금액 절감액 ($)",              "정적(ETN벤치마크)": "-",                    "동적(DRA전략)": f"${amt_sav:,.0f}"},
        {"항목": "절감률 (%)",                        "정적(ETN벤치마크)": "-",                    "동적(DRA전략)": f"{sr:.2f}%"},
        {"항목": "── 시장 상황 ──",                  "정적(ETN벤치마크)": "",                     "동적(DRA전략)": ""},
        {"항목": "Contango 구간 월수",                "정적(ETN벤치마크)": str(ct),                "동적(DRA전략)": str(ct)},
        {"항목": "Backwardation 구간 월수",           "정적(ETN벤치마크)": str(bw),                "동적(DRA전략)": str(bw)},
        {"항목": "── 실제 롤 거래 실행 ──",           "정적(ETN벤치마크)": "",                     "동적(DRA전략)": ""},
        {"항목": "실제 롤 실행 횟수",                 "정적(ETN벤치마크)": str(tm),                "동적(DRA전략)": f"{de} ({de/tm*100:.0f}%)"},
        {"항목": "  └ Contango 중 실행",             "정적(ETN벤치마크)": str(ct),                "동적(DRA전략)": str(dce)},
        {"항목": "  └ Backwardation 중 실행",        "정적(ETN벤치마크)": str(bw),                "동적(DRA전략)": str(dbe)},
        {"항목": "월물 유지(거래 없음) 횟수",          "정적(ETN벤치마크)": "0",                    "동적(DRA전략)": f"{dh} ({dh/tm*100:.0f}%)"},
    ]
    pd.DataFrame(cumul_rows).to_csv(out / "cumulative_summary.csv", index=False)
    print("  cumulative_summary.csv 저장")

    # 4. dra_positions.csv
    positions.to_csv(out / "dra_positions.csv")
    print(f"  dra_positions.csv 저장 ({len(positions)}행)")

    # 6. comparison_chart.png
    _plot_charts(monthly_df, out / "comparison_chart.png")


def _plot_charts(monthly_df: pd.DataFrame, output_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 2, figsize=(16, 18))
    fig.suptitle("WTI DRA vs Static Roll Comparison (ws_1_index — 2016–2026)",
                 fontsize=14, fontweight="bold")

    dates = monthly_df["date"]

    ax = axes[0, 0]
    ax.plot(dates, monthly_df["static_iry"],  label="Static (ETN BM)", alpha=0.8, linewidth=1)
    ax.plot(dates, monthly_df["dynamic_iry"], label="Dynamic (DRA)",   alpha=0.8, linewidth=1)
    ax.axhline(0, color="black", linewidth=0.5, linestyle="--")
    ax.set_title("Monthly IRY Comparison")
    ax.set_ylabel("IRY")
    ax.legend(fontsize=8)
    ax.tick_params(axis="x", rotation=30)

    ax = axes[0, 1]
    ax.plot(dates, monthly_df["iry_saving"].cumsum(), color="green", linewidth=1.5)
    ax.axhline(0, color="black", linewidth=0.5, linestyle="--")
    ax.set_title("Cumulative IRY Saving (Dynamic − Static)")
    ax.set_ylabel("Cumulative IRY")
    ax.tick_params(axis="x", rotation=30)

    ax = axes[1, 0]
    ax.plot(dates, monthly_df["static_execution_amount"]  / 1e6, label="Static", alpha=0.8, linewidth=1)
    ax.plot(dates, monthly_df["dynamic_execution_amount"] / 1e6, label="Dynamic", alpha=0.8, linewidth=1)
    ax.set_title("Monthly Execution Amount (USD M)")
    ax.set_ylabel("USD Million")
    ax.legend(fontsize=8)
    ax.tick_params(axis="x", rotation=30)

    ax = axes[1, 1]
    def sym_month(s):
        if isinstance(s, str) and len(s) >= 3:
            return FUTURES_CODE_TO_MONTH.get(s[2], 0)
        return 0
    nums = [sym_month(s) for s in monthly_df["dynamic_roll_contract"]]
    ax.hist(nums, bins=range(1, 14), align="left", rwidth=0.8,
            color="steelblue", edgecolor="white")
    ax.set_title("Dynamic Roll Contract Distribution")
    ax.set_xlabel("Contract Month Number")
    ax.set_ylabel("Count")
    ax.set_xticks(range(1, 13))

    ax = axes[2, 0]
    c_saving = monthly_df[monthly_df["market_condition"] == "Contango"]["iry_saving"]
    b_saving = monthly_df[monthly_df["market_condition"] == "Backwardation"]["iry_saving"]
    ax.boxplot([c_saving.dropna(), b_saving.dropna()],
               tick_labels=["Contango", "Backwardation"])
    ax.axhline(0, color="red", linewidth=0.5, linestyle="--")
    ax.set_title("IRY Saving by Market Condition")
    ax.set_ylabel("IRY Saving")

    ax = axes[2, 1]
    ann = monthly_df.groupby("year").agg(
        static=("static_execution_amount",  "sum"),
        dynamic=("dynamic_execution_amount", "sum"),
    ).reset_index()
    x = range(len(ann))
    w = 0.35
    ax.bar([i - w/2 for i in x], ann["static"]  / 1e6, w, label="Static",  color="steelblue")
    ax.bar([i + w/2 for i in x], ann["dynamic"] / 1e6, w, label="Dynamic", color="darkorange")
    ax.set_xticks(list(x))
    ax.set_xticklabels(ann["year"].astype(str), rotation=30)
    ax.set_title("Annual Execution Amount (USD M)")
    ax.set_ylabel("USD Million")
    ax.legend(fontsize=8)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  comparison_chart.png 저장")


# ============================================================
# 파트 17: DRA 메인 파이프라인
# run_index_calculation()과 완전히 동일한 구조 + DRA 롤오버
# tbill_rates_source 파라미터 추가 (ws_index.py 버그 수정)
# ============================================================

def run_dra_index_calculation(
    futures_prices_source,
    tbill_rates_source,
    start_date: str = "2016-01-04",
    end_date: str   = "2026-03-18",
    base_er_level: float = BASE_ER_LEVEL,
    base_tr_level: float = BASE_TR_LEVEL,
    commodity_prefix: str = COMMODITY_PREFIX,
    lot_size: float = LOT_SIZE,
    notional: Optional[float] = None,
    output_path: Optional[str] = None,
    positions_output_path: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> pd.DataFrame:
    """
    DRA(3) 롤오버 방식 WTI ER/TR 인덱스 계산 파이프라인.
    run_index_calculation()과 동일한 인터페이스.

    Parameters
    ----------
    futures_prices_source : str, list[str], 또는 pd.DataFrame
    tbill_rates_source : str 또는 pd.DataFrame
        [추가] ws_index.py 대비 추가 — ER/TR 인덱스 계산에 필요
    start_date : str
    end_date   : str
    base_er_level : float
    base_tr_level : float
    commodity_prefix : str
    lot_size : float
        [수정] ws_index.py 대비 추가 — calculate_dra_positions 계약 수 정상화
    notional : float, optional
    output_path : str, optional
        인덱스(ER/TR) 결과 CSV 저장 경로
    positions_output_path : str, optional
        DRA 포지션 추적 결과 CSV 저장 경로
    output_dir : str, optional
        월별 비교 분석 결과 폴더 경로

    Returns
    -------
    pd.DataFrame
        columns : lead_ticker, next_ticker, bd_of_month,
                  lead_weight, next_weight, is_mde, roll_required,
                  wav, pwav, der,
                  er_index, ir, tr_index
    """
    start_ts = pd.Timestamp(start_date)
    end_ts   = pd.Timestamp(end_date)

    print(f"[1/9] CME 영업일 캘린더 생성 ({start_date} ~ {end_date}) ...")
    bdays = get_cme_business_days(start_ts, end_ts)
    print(f"      → {len(bdays):,}개 CME 영업일")

    print("[2/9] 선물 결제가격 데이터 로딩 ...")
    prices_wide = load_futures_prices(futures_prices_source)
    print(f"      → {len(prices_wide):,}행, {len(prices_wide.columns)}개 계약 티커")

    # T-Bill 로드 — run_index_calculation과 동일 (ws_index.py 버그 수정)
    print("[3/9] T-Bill 금리 데이터 로딩 및 영업일 정렬 ...")
    tbill_raw = load_tbill_rates(tbill_rates_source)
    tbill = align_tbill(tbill_raw, bdays)
    print(f"      → {len(tbill):,}개 영업일 금리 적용")

    # expiration_map 구성 (DRA 스케줄에 필요)
    if isinstance(futures_prices_source, str):
        raw_df = pd.read_csv(futures_prices_source)
    elif isinstance(futures_prices_source, list):
        raw_df = pd.concat([pd.read_csv(f) for f in futures_prices_source], ignore_index=True)
    else:
        raw_df = futures_prices_source.copy()
    raw_df["expiration"] = pd.to_datetime(raw_df["expiration"], utc=True).dt.tz_localize(None)
    # [버그 수정] load_futures_prices와 동일한 로직으로 내부 티커 생성
    # 만기월 기반 코드 유도 대신 Symbol 월 코드 + delivery_year 보정 사용
    raw_df["_month_code"] = raw_df["Symbol"].str.extract(r"^CL([A-Z])")[0]
    _exp_year  = raw_df["expiration"].dt.year
    _exp_month = raw_df["expiration"].dt.month
    _deliv_month_num = raw_df["_month_code"].map(FUTURES_CODE_TO_MONTH)
    _delivery_year = _exp_year.where(_deliv_month_num > _exp_month, _exp_year + 1)
    raw_df["_ticker"] = (
        COMMODITY_PREFIX + raw_df["_month_code"] + _delivery_year.astype(str).str[-2:]
    )
    expiration_map = (
        raw_df[["_ticker", "expiration"]]
        .drop_duplicates(subset=["_ticker"])
        .set_index("_ticker")["expiration"]
        .to_dict()
    )

    print("[4/9] DRA(3) 계약 스케줄 생성 ...")
    dra_sched = build_dra_contract_schedule(bdays, prices_wide, expiration_map, commodity_prefix)
    monthly_flags = dra_sched.groupby([dra_sched.index.year, dra_sched.index.month])["roll_required"].first()
    print(f"      → 실제 롤 실행: {int(monthly_flags.sum())}개월 / 월물 유지: {int((~monthly_flags).sum())}개월")

    print("[5/9] DRA 롤 비중 계산 (MDE 처리 포함) ...")
    dra_rw = calculate_dra_roll_weights(dra_sched, prices_wide)
    n_mde = int(dra_rw["is_mde"].sum())
    if n_mde:
        print(f"      → MDE 감지: {n_mde}개 영업일에서 롤 유예 적용")
    else:
        print("      → MDE 없음")

    # WAV/PWAV/ER — run_index_calculation과 동일한 함수 사용
    print("[6/9] WAV / PWAV → DER → ER 인덱스 계산 ...")
    wp  = calculate_wav_pwav(bdays, dra_sched, dra_rw, prices_wide, lot_size)
    er  = calculate_er_index(wp, base_level=base_er_level)

    # IR/TR — run_index_calculation과 동일한 함수 사용
    print("[7/9] T-Bill IR → TR 인덱스 계산 ...")
    ir  = calculate_ir(bdays, tbill)
    tr  = calculate_tr_index(er, ir, base_level=base_tr_level)

    print("[8/9] DRA 포지션 및 롤오버 이벤트 생성 ...")
    positions = calculate_dra_positions(dra_sched, dra_rw, prices_wide,
                                        notional=notional, lot_size=lot_size)
    n_rolls = int(positions["is_roll_day"].sum())
    n_ticker_changes = int(positions["lead_ticker_changed"].sum())
    print(f"      → 롤오버 발생일: {n_rolls}일 | Lead 계약 교체: {n_ticker_changes}회")
    if positions_output_path:
        positions.to_csv(positions_output_path)
        print(f"      → 포지션 파일 저장: {positions_output_path}")

    print("[9/9] 월별 비교 분석 (정적 vs DRA) ...")
    monthly_results = _compute_monthly_comparison(
        bdays, prices_wide, expiration_map, dra_sched, commodity_prefix
    )
    if output_dir:
        _save_all_outputs(monthly_results, positions, output_dir)

    # 결과 통합 — run_index_calculation과 동일한 컬럼 구조 + roll_required
    result = dra_sched.copy()
    result["lead_weight"] = dra_rw["lead_weight"]
    result["next_weight"]  = dra_rw["next_weight"]
    result["is_mde"]       = dra_rw["is_mde"]
    result["wav"]          = wp["wav"]
    result["pwav"]         = wp["pwav"]
    result["der"]          = (wp["wav"] / wp["pwav"]) - 1.0
    result["er_index"]     = er
    result["ir"]           = ir
    result["tr_index"]     = tr

    result = result[
        [
            "lead_ticker", "next_ticker", "bd_of_month",
            "lead_weight", "next_weight", "is_mde", "roll_required",
            "wav", "pwav", "der",
            "er_index", "ir", "tr_index",
        ]
    ]

    if output_path:
        result.to_csv(output_path)
        print(f"\n결과 저장: {output_path}")

    print("\n" + "=" * 60)
    print("WTI DRA(3) Index — 계산 완료")
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
# 파트 18: 더미 데이터 생성 (테스트용)
# index_calculator.py와 완전히 동일
# ============================================================

def create_dummy_data(
    start: str = "2010-01-04",
    end: str = "2010-06-30",
    seed: int = 42,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    테스트용 합성 선물 가격 및 T-Bill 금리 데이터를 생성합니다.

    Returns
    -------
    futures_df : pd.DataFrame  (Wide format)
    tbill_df   : pd.DataFrame  (date, tbr 열)
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, end=end)

    contracts_2010 = [f"CL{c}10" for c in ALL_MONTH_CODES[1:]]
    contracts_2011 = [f"CL{c}11" for c in ALL_MONTH_CODES[:3]]

    price_data: dict[str, np.ndarray] = {}
    base = 80.0
    for i, tkr in enumerate(contracts_2010 + contracts_2011):
        level = base * (1 + i * 0.002)
        rets  = rng.normal(0.0, 0.015, len(dates))
        price_data[tkr] = level * np.exp(np.cumsum(rets))

    futures_df = pd.DataFrame(price_data, index=dates)

    weekly = pd.date_range(start=start, end=end, freq="W-MON")
    tbill_df = pd.DataFrame(
        {"date": weekly, "tbr": rng.uniform(0.05, 0.25, len(weekly))}
    )

    return futures_df, tbill_df


# ============================================================
# 진입점 (Entry Point)
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("WTI Crude Oil Index with DRA(3) Dynamic Roll")
    print("=" * 60)

    from pathlib import Path
    _DATA_DIR = Path(__file__).parent.parent / "data"
    FUTURES_SOURCE = str(_DATA_DIR / "cl_settlements_2016-2026.csv")
    TBILL_SOURCE   = str(_DATA_DIR / "13 Week Treasury Auction Rate High.csv")

    START_DATE = "2016-01-04"
    END_DATE   = "2026-03-18"

    BASE_ER = 100
    BASE_TR = 100

    NOTIONAL = 10_000_000

    # ── 정적 파이프라인 (index_calculator.py와 동일) ─────────
    print("\n[정적 롤오버 — ETN 추종 지수]")
    static_result = run_index_calculation(
        futures_prices_source=FUTURES_SOURCE,
        tbill_rates_source=TBILL_SOURCE,
        start_date=START_DATE,
        end_date=END_DATE,
        base_er_level=BASE_ER,
        base_tr_level=BASE_TR,
        output_path="ws1_static_index.csv",
        notional=NOTIONAL,
        positions_output_path="ws1_static_positions.csv",
    )

    # ── DRA 파이프라인 ────────────────────────────────────────
    print("\n[DRA(3) 동적 롤오버]")
    dra_result = run_dra_index_calculation(
        futures_prices_source=FUTURES_SOURCE,
        tbill_rates_source=TBILL_SOURCE,
        start_date=START_DATE,
        end_date=END_DATE,
        base_er_level=BASE_ER,
        base_tr_level=BASE_TR,
        lot_size=1000.0,
        notional=NOTIONAL,
        output_path="ws1_dra_index.csv",
        positions_output_path="ws1_dra_positions.csv",
        output_dir="ws1_output",
    )

    # ── 정적 vs DRA 최종 수익률 비교 ─────────────────────────
    print("\n" + "=" * 60)
    print("정적 ETN vs DRA 최종 수익률 비교")
    print("=" * 60)
    s_er = static_result["er_index"].iloc[-1]
    s_tr = static_result["tr_index"].iloc[-1]
    d_er = dra_result["er_index"].iloc[-1]
    d_tr = dra_result["tr_index"].iloc[-1]
    print(f"정적 ER : {static_result['er_index'].iloc[0]:.4f} → {s_er:.4f}  ({(s_er/BASE_ER - 1)*100:+.2f}%)")
    print(f"DRA  ER : {dra_result['er_index'].iloc[0]:.4f} → {d_er:.4f}  ({(d_er/BASE_ER - 1)*100:+.2f}%)")
    print(f"정적 TR : {static_result['tr_index'].iloc[0]:.4f} → {s_tr:.4f}  ({(s_tr/BASE_TR - 1)*100:+.2f}%)")
    print(f"DRA  TR : {dra_result['tr_index'].iloc[0]:.4f} → {d_tr:.4f}  ({(d_tr/BASE_TR - 1)*100:+.2f}%)")
