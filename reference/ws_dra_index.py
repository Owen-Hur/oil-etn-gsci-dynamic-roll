"""
ws_dra_index.py — WTI Crude Oil DRA(3) Dynamic Roll Index

Bloomberg WTI 원유 선물 지수 — 동적 롤오버(DRA) 전용 구현.

방법론:
  - DRA(Dynamic Roll — Rank 3): 매월 롤결정일(BD6)에 IRY 기준 상위 3개 월물 셋을
    산출하고, 현재 보유 월물이 해당 셋에 없을 때만 최적 월물로 롤오버 실행.
  - 롤오버 없는 달은 현재 보유 월물을 그대로 유지(거래 없음).
  - ER/TR 인덱스 계산 방식은 Bloomberg 공식과 동일.

Base Date : 2016-01-04  |  Base Level : 100.0 (ER & TR)
Commodity : WTI Crude Oil  |  Bloomberg Code : CL  |  Exchange : CME/NYMEX
"""

from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
import pandas as pd


# ============================================================
# 파트 1: 상수 및 설정
# ============================================================

BASE_DATE:     pd.Timestamp = pd.Timestamp("2010-01-04")
BASE_ER_LEVEL: float        = 100.0
BASE_TR_LEVEL: float        = 100.0

LOT_SIZE: float = 1000.0   # WTI 선물 1계약 = 1,000 배럴
CIM:      float = 1.0      # Commodity Index Multiplier (리밸런싱 없음 → 항상 1)

# 월 번호 → 선물 월 코드 (인도월 기준)
MONTH_TO_LEAD_CODE: dict[int, str] = {
     1: "G",  2: "H",  3: "J",  4: "K",
     5: "M",  6: "N",  7: "Q",  8: "U",
     9: "V", 10: "X", 11: "Z", 12: "F",
}

# 선물 월 코드 → 월 번호
FUTURES_CODE_TO_MONTH: dict[str, int] = {
    "F":  1, "G":  2, "H":  3, "J":  4,
    "K":  5, "M":  6, "N":  7, "Q":  8,
    "U":  9, "V": 10, "X": 11, "Z": 12,
}

ALL_MONTH_CODES: list[str] = [
    "F", "G", "H", "J", "K", "M",
    "N", "Q", "U", "V", "X", "Z",
]

ROLL_START_BD: int   = 6     # 롤 기간 시작 영업일
ROLL_END_BD:   int   = 10    # 롤 기간 종료 영업일
WEIGHT_STEP:   float = 0.20  # 1영업일당 비중 이전량 (20%)

TBILL_MATURITY:   int = 91   # 13주 T-Bill 만기 (일)
TBILL_DAY_COUNT:  int = 360  # T-Bill 할인율 day count convention

COMMODITY_PREFIX: str = "CL"


# ============================================================
# 파트 2: CME 영업일 캘린더 생성
# ============================================================

def get_cme_business_days(
    start_date: pd.Timestamp,
    end_date:   pd.Timestamp,
) -> pd.DatetimeIndex:
    """지정 기간의 CME 영업일 목록을 반환합니다."""
    try:
        import pandas_market_calendars as mcal  # type: ignore

        cal      = mcal.get_calendar("CMEGlobex_CL")
        schedule = cal.schedule(start_date=start_date, end_date=end_date)
        bdays    = mcal.date_range(schedule, frequency="1D").normalize().tz_localize(None)
        return bdays

    except (ImportError, RuntimeError, TypeError) as e:
        warnings.warn(
            f"pandas_market_calendars 로드 실패 ({e}) → 내장 CME 공휴일 목록으로 대체.\n"
            "미설치 시: pip install pandas-market-calendars",
            UserWarning,
            stacklevel=2,
        )
        return _cme_fallback(start_date, end_date)


def _cme_fallback(
    start_date: pd.Timestamp,
    end_date:   pd.Timestamp,
) -> pd.DatetimeIndex:
    """pandas_market_calendars 없이 CME 영업일을 계산하는 대체 함수."""
    all_weekdays = pd.bdate_range(start=start_date, end=end_date)
    holidays     = _build_cme_holidays(start_date.year, end_date.year)
    return all_weekdays[~all_weekdays.isin(holidays)]


def _build_cme_holidays(start_year: int, end_year: int) -> pd.DatetimeIndex:
    """
    CME가 준수하는 미국 연방 공휴일 목록을 생성합니다.

    포함 공휴일:
        New Year's Day, MLK Day, Presidents Day, Good Friday,
        Memorial Day, Juneteenth (2022~), Independence Day,
        Labor Day, Thanksgiving, Christmas Day
    """
    try:
        from dateutil.easter import easter
        from dateutil.relativedelta import MO, TH, relativedelta
    except ImportError:
        raise ImportError("python-dateutil 필요: pip install python-dateutil")

    holidays: list[pd.Timestamp] = []

    def obs(d: pd.Timestamp) -> pd.Timestamp:
        """주말 공휴일의 대체 관측일 반환 (토 → 금, 일 → 월)."""
        if d.dayofweek == 5:
            return d - pd.Timedelta(days=1)
        if d.dayofweek == 6:
            return d + pd.Timedelta(days=1)
        return d

    for yr in range(start_year, end_year + 1):
        holidays.append(obs(pd.Timestamp(yr, 1,  1)))                          # New Year's Day
        holidays.append(pd.Timestamp(yr, 1,  1) + relativedelta(weekday=MO(3)))# MLK Day
        holidays.append(pd.Timestamp(yr, 2,  1) + relativedelta(weekday=MO(3)))# Presidents Day
        holidays.append(pd.Timestamp(easter(yr)) - pd.Timedelta(days=2))       # Good Friday
        holidays.append(pd.Timestamp(yr, 5, 31) + relativedelta(weekday=MO(-1)))# Memorial Day
        if yr >= 2022:
            holidays.append(obs(pd.Timestamp(yr, 6, 19)))                      # Juneteenth
        holidays.append(obs(pd.Timestamp(yr, 7,  4)))                          # Independence Day
        holidays.append(pd.Timestamp(yr, 9,  1) + relativedelta(weekday=MO(1)))# Labor Day
        holidays.append(pd.Timestamp(yr, 11, 1) + relativedelta(weekday=TH(4)))# Thanksgiving
        holidays.append(obs(pd.Timestamp(yr, 12, 25)))                         # Christmas Day

    return pd.DatetimeIndex(sorted(set(holidays)))


# ============================================================
# 파트 3: 데이터 로딩 및 검증
# ============================================================

def load_futures_prices(
    source,
    trade_date_col: str = "Trade date",
    symbol_col:     str = "Symbol",
    price_col:      str = "Settlement price",
    expiration_col: str = "expiration",
) -> pd.DataFrame:
    """
    WTI 선물 결제가격 데이터를 로드합니다. (Databento Long 형식 전용)

    Parameters
    ----------
    source : str | list[str] | pd.DataFrame

    Returns
    -------
    pd.DataFrame  (index=거래일, columns=내부 2자리연도 티커, dtype=float)
    """
    if isinstance(source, list):
        frames = [pd.read_csv(f) for f in source]
        df     = pd.concat(frames, ignore_index=True)
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
    df[expiration_col] = pd.to_datetime(df[expiration_col], utc=True).dt.tz_localize(None)

    # 내부 티커 생성: CL + 월코드 + 인도연도 2자리  (예: CLH25)
    # WTI 선물은 인도월 전달에 만기 → delivery_month > exp_month이면 같은 해,
    # 아니면(F 계약 등) 다음 해로 보정
    df["_month_code"]      = df[symbol_col].str.extract(r"^CL([A-Z])")[0]
    exp_year               = df[expiration_col].dt.year
    exp_month              = df[expiration_col].dt.month
    delivery_month_num     = df["_month_code"].map(FUTURES_CODE_TO_MONTH)
    delivery_year          = exp_year.where(delivery_month_num > exp_month, exp_year + 1)
    df["_internal_ticker"] = COMMODITY_PREFIX + df["_month_code"] + delivery_year.astype(str).str[-2:]

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
    (Moody's Analytics CSV 내보내기 형식 전용)

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
        s     = str(date_str).strip()
        parts = s.split("-") if "-" in s else s.split(".")
        if len(parts) != 2:
            return None
        a, b = parts[0].strip(), parts[1].strip()
        year_str, month_str = (a, b) if a.isdigit() else (b, a)
        try:
            yr2  = int(year_str)
            year = 2000 + yr2 if yr2 <= 30 else 1900 + yr2
            return pd.Timestamp(f"{year}-{month_str}-01")
        except Exception:
            return None

    dates = raw[0].apply(_parse_tbill_date)
    rates = pd.to_numeric(raw[rate_col_index], errors="coerce") / 100.0

    s = pd.Series(rates.values, index=dates, name="tbr")
    s = s[s.index.notna()].dropna().sort_index()

    if s.empty:
        raise ValueError("T-Bill 금리 데이터가 비어 있습니다.")
    if (s < 0).any():
        warnings.warn("음수 T-Bill 금리가 감지되었습니다.", UserWarning)

    return s


def align_tbill(
    tbill:        pd.Series,
    business_days: pd.DatetimeIndex,
) -> pd.Series:
    """월간 T-Bill 금리를 영업일 기준으로 재인덱싱하고 forward-fill을 적용합니다."""
    aligned = tbill.reindex(business_days, method="ffill")
    n_miss  = aligned.isna().sum()
    if n_miss:
        warnings.warn(
            f"T-Bill 금리 {n_miss}개 영업일에 데이터가 없습니다. 범위를 확인하세요.",
            UserWarning,
        )
    return aligned


# ============================================================
# 파트 4: WAV 및 PWAV 계산
# ============================================================

def calculate_wav_pwav(
    business_days:    pd.DatetimeIndex,
    contract_schedule: pd.DataFrame,
    roll_weights:      pd.DataFrame,
    prices_df:         pd.DataFrame,
    lot_size:          float = LOT_SIZE,
) -> pd.DataFrame:
    """
    각 영업일의 WAV(당일 기준)와 PWAV(전일 기준)를 계산합니다.

    Bloomberg 공식:
        WAV_t  = CIM × YLRW × (LCSP_t  / L) + CIM × YNRW × (NCSP_t  / L)
        PWAV_t = CIM × YLRW × (LCSP_t-1 / L) + CIM × YNRW × (NCSP_t-1 / L)

    Returns
    -------
    pd.DataFrame  columns: wav, pwav
    """
    prices_ffill = prices_df.reindex(business_days).ffill()
    dates        = list(business_days)
    wav_vals:  list[float] = []
    pwav_vals: list[float] = []

    for i, date in enumerate(dates):
        if i == 0:
            wav_vals.append(np.nan)
            pwav_vals.append(np.nan)
            continue

        prev  = dates[i - 1]
        ylrw  = roll_weights.at[prev, "lead_weight"]
        ynrw  = roll_weights.at[prev, "next_weight"]
        lead  = contract_schedule.at[prev, "lead_ticker"]
        nxt   = contract_schedule.at[prev, "next_ticker"]

        def safe_price(ticker: str, dt: pd.Timestamp) -> float:
            if ticker not in prices_ffill.columns:
                return np.nan
            if dt not in prices_ffill.index:
                return np.nan
            return prices_ffill.at[dt, ticker]

        lcsp_t   = safe_price(lead, date)
        ncsp_t   = safe_price(nxt,  date)
        lcsp_tm1 = safe_price(lead, prev)
        ncsp_tm1 = safe_price(nxt,  prev)

        wav  = CIM * ylrw * (lcsp_t   / lot_size) + CIM * ynrw * (ncsp_t   / lot_size)
        pwav = CIM * ylrw * (lcsp_tm1 / lot_size) + CIM * ynrw * (ncsp_tm1 / lot_size)

        wav_vals.append(wav)
        pwav_vals.append(pwav)

    return pd.DataFrame({"wav": wav_vals, "pwav": pwav_vals}, index=dates)


# ============================================================
# 파트 5: ER(초과 수익) 인덱스 계산
# ============================================================

def calculate_er_index(
    wav_pwav:   pd.DataFrame,
    base_level: float = BASE_ER_LEVEL,
) -> pd.Series:
    """
    ER 인덱스 레벨을 계산합니다.

    Bloomberg 공식:
        DER_t     = WAV_t / PWAV_t - 1
        IndexER_t = IndexER_{t-1} × (1 + DER_t)   [Zero Floor, 8dp]
    """
    er:         list[float] = []
    prev_level: float       = base_level

    for wav, pwav in zip(wav_pwav["wav"], wav_pwav["pwav"]):
        if np.isnan(wav) or np.isnan(pwav) or pwav == 0.0:
            er.append(round(prev_level, 8))
            continue

        der       = wav / pwav - 1.0
        new_level = round(max(0.0, prev_level * (1.0 + der)), 8)
        er.append(new_level)
        prev_level = new_level

    return pd.Series(er, index=wav_pwav.index, name="er_index")


# ============================================================
# 파트 6: T-Bill 일일 수익률(IR) 계산
# ============================================================

def calculate_ir(
    business_days:  pd.DatetimeIndex,
    tbill_aligned:  pd.Series,
) -> pd.Series:
    """
    T-Bill 일일 수익률(IR_t)을 계산합니다.

    Bloomberg 공식:
        IR_t = [1 / (1 - (91/360) × TBR_{t-1})]^(D/91) - 1
    """
    dates:    list[pd.Timestamp] = list(business_days)
    ir_vals:  list[float]        = []

    for i, date in enumerate(dates):
        if i == 0:
            ir_vals.append(0.0)
            continue

        prev = dates[i - 1]
        D    = (date - prev).days
        tbr  = tbill_aligned.get(prev, np.nan)

        if np.isnan(tbr):
            warnings.warn(f"{prev.date()} T-Bill 금리 누락 → IR=0 처리", UserWarning, stacklevel=2)
            ir_vals.append(0.0)
            continue

        denom = 1.0 - (TBILL_MATURITY / TBILL_DAY_COUNT) * tbr
        if denom <= 0.0:
            warnings.warn(f"{date.date()} IR 계산 오류: 분모 ≤ 0 → IR=0 처리", UserWarning, stacklevel=2)
            ir_vals.append(0.0)
            continue

        ir_vals.append((1.0 / denom) ** (D / TBILL_MATURITY) - 1.0)

    return pd.Series(ir_vals, index=dates, name="ir")


# ============================================================
# 파트 7: TR(총 수익) 인덱스 계산
# ============================================================

def calculate_tr_index(
    er_index:   pd.Series,
    ir:         pd.Series,
    base_level: float = BASE_TR_LEVEL,
) -> pd.Series:
    """
    TR 인덱스 레벨을 계산합니다.

    Bloomberg 공식:
        IndexTR_t = IndexTR_{t-1} × (IndexER_t / IndexER_{t-1} + IR_t)
        [Zero Floor, 8dp]
    """
    dates:   list[pd.Timestamp] = er_index.index.tolist()
    tr:      list[float]        = []
    prev_tr: float              = base_level

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

        new_tr  = round(max(0.0, prev_tr * (er_t / er_tm1 + ir_t)), 8)
        tr.append(new_tr)
        prev_tr = new_tr

    return pd.Series(tr, index=dates, name="tr_index")


# ============================================================
# 파트 8: DRA 계약 스케줄 생성
# ============================================================

def _get_contracts_for_date(
    prices_wide:    pd.DataFrame,
    expiration_map: dict,
    date:           pd.Timestamp,
    n:              int = 12,
) -> list:
    """특정 날짜 기준 만기 오름차순 월물 리스트 반환. [(심볼, 가격, 만기일), ...]"""
    if date not in prices_wide.index:
        return []
    row    = prices_wide.loc[date]
    result = []
    for symbol, expiry in expiration_map.items():
        if pd.isna(expiry) or expiry <= date:
            continue
        if symbol not in row.index or pd.isna(row[symbol]):
            continue
        result.append((symbol, row[symbol], expiry))
    result.sort(key=lambda x: x[2])
    return result[:n]


def _calculate_iry(contracts: list) -> list:
    """
    연속 페어(Consecutive Pair) IRY 계산 후 내림차순 정렬.

    IRY = (P_near - P_far) / (P_far × 월간격)
    """
    if len(contracts) < 2:
        return []
    iry_list = []
    for i in range(len(contracts) - 1):
        sym_prev, price_prev, exp_prev = contracts[i]
        sym_curr, price_curr, exp_curr = contracts[i + 1]
        if price_curr == 0:
            continue
        interval = (exp_curr.year - exp_prev.year) * 12 + (exp_curr.month - exp_prev.month)
        if interval <= 0:
            interval = 1
        iry = (price_prev - price_curr) / (price_curr * interval)
        iry_list.append((sym_curr, iry))
    iry_list.sort(key=lambda x: x[1], reverse=True)
    return iry_list


def _run_dra(
    ranked_contracts: list,
    current_contract: str,
    rank_order:       int = 3,
) -> dict:
    """
    DRA(3) — Dynamic Roll Parity Principle 적용.

    현재 보유 계약이 Optimum Set 내 → PASS(유지)
    현재 보유 계약이 Optimum Set 밖 → ROLL(Best(1)로 교체)
    """
    top_n        = min(rank_order, len(ranked_contracts))
    optimum_set  = [sym for sym, _ in ranked_contracts[:top_n]]

    if not optimum_set:
        return {"roll_in": current_contract, "roll_required": False, "selected_iry": float("nan")}

    best_sym = optimum_set[0]
    best_iry = ranked_contracts[0][1]

    if current_contract in optimum_set:
        iry_map = {sym: iry for sym, iry in ranked_contracts[:top_n]}
        return {"roll_in": current_contract, "roll_required": False,
                "selected_iry": iry_map[current_contract]}

    return {"roll_in": best_sym, "roll_required": True, "selected_iry": best_iry}


def build_dra_contract_schedule(
    business_days:  pd.DatetimeIndex,
    prices_wide:    pd.DataFrame,
    expiration_map: dict,
    prefix:         str = COMMODITY_PREFIX,
) -> pd.DataFrame:
    """
    DRA(3) 기반 월별 계약 스케줄 생성.

    Returns
    -------
    pd.DataFrame  index=business_days
        columns: lead_ticker, next_ticker, bd_of_month, roll_required
    """
    months = sorted(set((d.year, d.month) for d in business_days))

    # 초기 보유 계약 설정
    first_yr, first_mo = months[0]
    lc              = MONTH_TO_LEAD_CODE[first_mo]
    c_yr            = first_yr + 1 if FUTURES_CODE_TO_MONTH[lc] <= first_mo else first_yr
    current_holding = f"{prefix}{lc}{str(c_yr)[-2:]}"

    # 월별 롤 결정
    month_decisions: dict = {}
    for yr, mo in months:
        month_bdays = [d for d in business_days if d.year == yr and d.month == mo]

        if len(month_bdays) < ROLL_START_BD:
            month_decisions[(yr, mo)] = {
                "roll_required": False,
                "roll_out": current_holding,
                "roll_in":  current_holding,
            }
            continue

        det_date  = month_bdays[ROLL_START_BD - 1]
        contracts = _get_contracts_for_date(prices_wide, expiration_map, det_date, n=12)

        if len(contracts) < 2:
            month_decisions[(yr, mo)] = {
                "roll_required": False,
                "roll_out": current_holding,
                "roll_in":  current_holding,
            }
            continue

        ranked = _calculate_iry(contracts)
        dra    = _run_dra(ranked, current_holding)

        month_decisions[(yr, mo)] = {
            "roll_required": dra["roll_required"],
            "roll_out":      current_holding,
            "roll_in":       dra["roll_in"],
        }
        current_holding = dra["roll_in"]

    # 일별 스케줄 생성
    records = []
    for d in business_days:
        yr, mo      = d.year, d.month
        month_bdays = [b for b in business_days if b.year == yr and b.month == mo]
        bd          = month_bdays.index(d) + 1

        dec           = month_decisions.get((yr, mo), {})
        roll_required = dec.get("roll_required", False)
        roll_out      = dec.get("roll_out", current_holding)
        roll_in       = dec.get("roll_in",  current_holding)

        lead = roll_out
        nxt  = roll_in if roll_required else roll_out

        records.append({
            "lead_ticker":   lead,
            "next_ticker":   nxt,
            "bd_of_month":   bd,
            "roll_required": roll_required,
        })

    return pd.DataFrame(records, index=business_days)


# ============================================================
# 파트 9: DRA 롤 비중 계산
# ============================================================

def calculate_dra_roll_weights(
    dra_schedule: pd.DataFrame,
    prices_df:    pd.DataFrame,
) -> pd.DataFrame:
    """
    DRA 기반 일별 롤 비중 계산.

    - roll_required=False 달: lead_weight=1.0 고정
    - roll_required=True 달: BD6~BD10 동안 매일 20%씩 next로 이전
    - 가격 데이터 누락(MDE) 시 해당 일 롤 유예 → 다음 영업일에 누적 적용

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
    last_ym:    tuple = (0, 0)

    def has_price(ticker: str, date: pd.Timestamp) -> bool:
        if ticker not in prices_df.columns:
            return False
        if date not in prices_df.index:
            return False
        return not np.isnan(prices_df.at[date, ticker])

    for date in dra_schedule.index:
        row           = dra_schedule.loc[date]
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
                is_mde   = True
                pending += 1
            else:
                rolls      = 1 + pending
                cur_next_w = min(1.0, cur_next_w + WEIGHT_STEP * rolls)
                cur_lead_w = max(0.0, 1.0 - cur_next_w)
                pending    = 0

        elif roll_required and bd > ROLL_END_BD and pending > 0:
            if not has_price(lead_t, date) or not has_price(next_t, date):
                is_mde   = True
                pending += 1
            else:
                cur_next_w = min(1.0, cur_next_w + WEIGHT_STEP * pending)
                cur_lead_w = max(0.0, 1.0 - cur_next_w)
                pending    = 0

        lead_w_dict[date] = cur_lead_w
        next_w_dict[date] = cur_next_w
        mde_dict[date]    = is_mde

    return pd.DataFrame({
        "lead_weight": lead_w_dict,
        "next_weight": next_w_dict,
        "is_mde":      mde_dict,
    })


# ============================================================
# 파트 10: DRA 포지션 트래킹
# ============================================================

def calculate_dra_positions(
    contract_schedule: pd.DataFrame,
    roll_weights:      pd.DataFrame,
    prices_df:         pd.DataFrame,
    notional:          Optional[float] = None,
    lot_size:          float           = LOT_SIZE,
) -> pd.DataFrame:
    """
    DRA 기반 포지션 트래킹.

    포지션 계산 공식:
        blended_price   = lead_weight × lead_price + next_weight × next_price
        total_contracts = notional / (blended_price × lot_size)
        lead_contracts  = lead_weight × total_contracts
        next_contracts  = next_weight × total_contracts

    Returns
    -------
    pd.DataFrame  (index=영업일)
        항상 포함 : lead_ticker, next_ticker, bd_of_month,
                    lead_weight, next_weight, is_mde, roll_required,
                    lead_price, next_price, blended_price,
                    is_roll_day, lead_ticker_changed
        notional 제공 시 추가 : total_contracts, lead_contracts, next_contracts,
                                lead_contracts_traded, next_contracts_traded
    """
    bdays        = contract_schedule.index
    prices_ffill = prices_df.reindex(bdays).ffill()

    rows: list[dict]          = []
    prev_lead_ticker: Optional[str]   = None
    prev_lead_w:      Optional[float] = None
    prev_lead_c:      float           = 0.0
    prev_next_c:      float           = 0.0

    for date in bdays:
        srow = contract_schedule.loc[date]
        rrow = roll_weights.loc[date]

        lead_t:   str   = srow["lead_ticker"]
        next_t:   str   = srow["next_ticker"]
        bd:       int   = int(srow["bd_of_month"])
        roll_req: bool  = bool(srow["roll_required"])
        lead_w:   float = rrow["lead_weight"]
        next_w:   float = rrow["next_weight"]
        is_mde:   bool  = rrow["is_mde"]

        def _px(ticker: str) -> float:
            if ticker in prices_ffill.columns:
                v = prices_ffill.at[date, ticker]
                return float(v) if not np.isnan(v) else np.nan
            return np.nan

        lead_price: float = _px(lead_t)
        next_price: float = _px(next_t)

        lp      = lead_price if not np.isnan(lead_price) else 0.0
        np_     = next_price if not np.isnan(next_price) else 0.0
        blended = lead_w * lp + next_w * np_

        lead_ticker_changed = (prev_lead_ticker is not None) and (lead_t != prev_lead_ticker)
        weight_changed      = (prev_lead_w      is not None) and (lead_w != prev_lead_w)
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
                total_c = notional / (blended * lot_size)
                lead_c  = lead_w * total_c
                next_c  = next_w * total_c
            else:
                total_c = lead_c = next_c = np.nan

            lead_traded = (lead_c - prev_lead_c) if not np.isnan(lead_c) else np.nan
            next_traded = (next_c - prev_next_c) if not np.isnan(next_c) else np.nan

            row["total_contracts"]       = total_c
            row["lead_contracts"]        = lead_c
            row["next_contracts"]        = next_c
            row["lead_contracts_traded"] = lead_traded
            row["next_contracts_traded"] = next_traded

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
        ["total_contracts", "lead_contracts", "next_contracts",
         "lead_contracts_traded", "next_contracts_traded"]
        if notional is not None else []
    )

    return pd.DataFrame(rows, index=bdays)[base_cols + extra_cols]


# ============================================================
# 파트 11: DRA 메인 파이프라인
# ============================================================

def run_dra_index_calculation(
    futures_prices_source,
    tbill_rates_source,
    start_date:       str            = "2016-01-04",
    end_date:         str            = "2026-03-18",
    base_er_level:    float          = BASE_ER_LEVEL,
    base_tr_level:    float          = BASE_TR_LEVEL,
    commodity_prefix: str            = COMMODITY_PREFIX,
    lot_size:         float          = LOT_SIZE,
    notional:         Optional[float] = None,
    output_path:      Optional[str]  = None,
    positions_output_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    DRA(3) 롤오버 방식 WTI ER/TR 인덱스 계산 파이프라인.

    Parameters
    ----------
    futures_prices_source : str | list[str] | pd.DataFrame
    tbill_rates_source    : str | pd.DataFrame
    start_date            : str
    end_date              : str
    base_er_level         : float
    base_tr_level         : float
    commodity_prefix      : str
    lot_size              : float
    notional              : float, optional
    output_path           : str, optional  — 인덱스(ER/TR) 결과 CSV 저장 경로
    positions_output_path : str, optional  — 포지션 추적 결과 CSV 저장 경로

    Returns
    -------
    pd.DataFrame
        columns: lead_ticker, next_ticker, bd_of_month,
                 lead_weight, next_weight, is_mde, roll_required,
                 wav, pwav, der, er_index, ir, tr_index
    """
    start_ts = pd.Timestamp(start_date)
    end_ts   = pd.Timestamp(end_date)

    # ── 1단계: CME 영업일 캘린더 ──────────────────────────────
    print(f"[1/8] CME 영업일 캘린더 생성 ({start_date} ~ {end_date}) ...")
    bdays = get_cme_business_days(start_ts, end_ts)
    print(f"      → {len(bdays):,}개 CME 영업일")

    # ── 2단계: 선물 결제가격 로딩 ────────────────────────────
    print("[2/8] 선물 결제가격 데이터 로딩 ...")
    prices_wide = load_futures_prices(futures_prices_source)
    print(f"      → {len(prices_wide):,}행, {len(prices_wide.columns)}개 계약 티커")

    # ── 3단계: T-Bill 금리 로딩 ──────────────────────────────
    print("[3/8] T-Bill 금리 데이터 로딩 및 영업일 정렬 ...")
    tbill_raw = load_tbill_rates(tbill_rates_source)
    tbill     = align_tbill(tbill_raw, bdays)
    print(f"      → {len(tbill):,}개 영업일 금리 적용")

    # expiration_map 구성 (DRA 스케줄 생성에 필요)
    if isinstance(futures_prices_source, str):
        raw_df = pd.read_csv(futures_prices_source)
    elif isinstance(futures_prices_source, list):
        raw_df = pd.concat([pd.read_csv(f) for f in futures_prices_source], ignore_index=True)
    else:
        raw_df = futures_prices_source.copy()

    raw_df["expiration"]   = pd.to_datetime(raw_df["expiration"], utc=True).dt.tz_localize(None)
    raw_df["_month_code"]  = raw_df["Symbol"].str.extract(r"^CL([A-Z])")[0]
    _exp_year              = raw_df["expiration"].dt.year
    _exp_month             = raw_df["expiration"].dt.month
    _deliv_month_num       = raw_df["_month_code"].map(FUTURES_CODE_TO_MONTH)
    _delivery_year         = _exp_year.where(_deliv_month_num > _exp_month, _exp_year + 1)
    raw_df["_ticker"]      = COMMODITY_PREFIX + raw_df["_month_code"] + _delivery_year.astype(str).str[-2:]
    expiration_map = (
        raw_df[["_ticker", "expiration"]]
        .drop_duplicates(subset=["_ticker"])
        .set_index("_ticker")["expiration"]
        .to_dict()
    )

    # ── 4단계: DRA 계약 스케줄 ───────────────────────────────
    print("[4/8] DRA(3) 계약 스케줄 생성 ...")
    dra_sched     = build_dra_contract_schedule(bdays, prices_wide, expiration_map, commodity_prefix)
    monthly_flags = dra_sched.groupby([dra_sched.index.year, dra_sched.index.month])["roll_required"].first()
    print(f"      → 롤 실행: {int(monthly_flags.sum())}개월  |  월물 유지: {int((~monthly_flags).sum())}개월")

    # ── 5단계: 롤 비중 계산 ──────────────────────────────────
    print("[5/8] DRA 롤 비중 계산 (MDE 처리 포함) ...")
    dra_rw = calculate_dra_roll_weights(dra_sched, prices_wide)
    n_mde  = int(dra_rw["is_mde"].sum())
    print(f"      → MDE 감지: {n_mde}개 영업일" if n_mde else "      → MDE 없음")

    # ── 6단계: WAV / PWAV → DER → ER ────────────────────────
    print("[6/8] WAV / PWAV → DER → ER 인덱스 계산 ...")
    wp = calculate_wav_pwav(bdays, dra_sched, dra_rw, prices_wide, lot_size)
    er = calculate_er_index(wp, base_level=base_er_level)

    # ── 7단계: T-Bill IR → TR ────────────────────────────────
    print("[7/8] T-Bill IR → TR 인덱스 계산 ...")
    ir = calculate_ir(bdays, tbill)
    tr = calculate_tr_index(er, ir, base_level=base_tr_level)

    # ── 8단계: 포지션 트래킹 ─────────────────────────────────
    print("[8/8] DRA 포지션 및 롤오버 이벤트 생성 ...")
    positions        = calculate_dra_positions(dra_sched, dra_rw, prices_wide,
                                               notional=notional, lot_size=lot_size)
    n_rolls          = int(positions["is_roll_day"].sum())
    n_ticker_changes = int(positions["lead_ticker_changed"].sum())
    print(f"      → 롤오버 발생일: {n_rolls}일  |  Lead 계약 교체: {n_ticker_changes}회")

    if positions_output_path:
        positions.to_csv(positions_output_path)
        print(f"      → 포지션 파일 저장: {positions_output_path}")

    # ── 결과 정리 ────────────────────────────────────────────
    result = dra_sched.copy()
    result["lead_weight"] = dra_rw["lead_weight"]
    result["next_weight"] = dra_rw["next_weight"]
    result["is_mde"]      = dra_rw["is_mde"]
    result["wav"]         = wp["wav"]
    result["pwav"]        = wp["pwav"]
    result["der"]         = (wp["wav"] / wp["pwav"]) - 1.0
    result["er_index"]    = er
    result["ir"]          = ir
    result["tr_index"]    = tr

    result = result[[
        "lead_ticker", "next_ticker", "bd_of_month",
        "lead_weight", "next_weight", "is_mde", "roll_required",
        "wav", "pwav", "der",
        "er_index", "ir", "tr_index",
    ]]

    if output_path:
        result.to_csv(output_path)
        print(f"\n결과 저장: {output_path}")

    print("\n" + "=" * 60)
    print("WTI DRA(3) Index — 계산 완료")
    print("=" * 60)
    print(f"기간      : {result.index[0].date()} ~ {result.index[-1].date()}")
    print(f"영업일 수 : {len(result):,}일")
    print(f"ER 인덱스 : {result['er_index'].iloc[0]:.8f} → {result['er_index'].iloc[-1]:.8f}")
    print(f"TR 인덱스 : {result['tr_index'].iloc[0]:.8f} → {result['tr_index'].iloc[-1]:.8f}")

    return result


# ============================================================
# 진입점 (Entry Point)
# ============================================================

if __name__ == "__main__":
    from pathlib import Path

    print("=" * 60)
    print("WTI Crude Oil Index with DRA(3) Dynamic Roll")
    print("=" * 60)

    _DATA_DIR      = Path(__file__).parent.parent / "data"
    FUTURES_SOURCE = str(_DATA_DIR / "cl_settlements_2016-2026.csv")
    TBILL_SOURCE   = str(_DATA_DIR / "13 Week Treasury Auction Rate High.csv")

    dra_result = run_dra_index_calculation(
        futures_prices_source = FUTURES_SOURCE,
        tbill_rates_source    = TBILL_SOURCE,
        start_date            = "2016-01-04",
        end_date              = "2026-03-18",
        base_er_level         = 100,
        base_tr_level         = 100,
        lot_size              = 1000.0,
        notional              = 10_000_000,
        output_path           = "ws_dra_index.csv",
        positions_output_path = "ws_dra_positions.csv",
    )
