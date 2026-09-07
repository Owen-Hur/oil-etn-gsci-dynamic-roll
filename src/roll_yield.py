"""
roll_yield.py — Implied Roll Yield 계산
연속 페어(Consecutive Pair)로만 계산. IRY 내림차순 정렬.
"""
import pandas as pd
from typing import List, Tuple


def calculate_iry(contracts: List[Tuple]) -> List[Tuple]:
    """
    연속 페어별 IRY 계산 후 내림차순 정렬.

    Parameters
    ----------
    contracts : [(심볼, 가격, 만기일), ...] — 만기 오름차순 정렬된 상태

    Returns
    -------
    [(심볼_롤인, IRY값), ...] — IRY 내림차순
    IRY의 '심볼'은 롤인(뒤) 월물 기준
    """
    if len(contracts) < 2:
        return []

    iry_list = []
    for i in range(len(contracts) - 1):
        sym_prev, price_prev, exp_prev = contracts[i]
        sym_curr, price_curr, exp_curr = contracts[i + 1]

        if price_curr == 0:
            continue

        # Interval_D: 두 계약 간 개월 수
        interval_d = (
            (exp_curr.year - exp_prev.year) * 12
            + (exp_curr.month - exp_prev.month)
        )
        if interval_d <= 0:
            interval_d = 1

        iry = (price_prev - price_curr) / (price_curr * interval_d)
        # 롤인 월물(뒤 월물)이 선택 대상
        iry_list.append((sym_curr, iry))

    # IRY 내림차순 (절댓값 아님, 실제 값 기준)
    iry_list.sort(key=lambda x: x[1], reverse=True)
    return iry_list


def get_static_iry(contracts: List[Tuple], determination_date: pd.Timestamp) -> float:
    """
    정적 롤오버(Lead→Next) IRY 반환.
    index_calculator.py의 MONTH_TO_LEAD_CODE 기준 Lead와 Next 간 IRY.

    Returns
    -------
    float — Lead→Next IRY, 계산 불가 시 float('nan')
    """
    from index_calculator import (
        MONTH_TO_LEAD_CODE,
        FUTURES_CODE_TO_MONTH,
        ALL_MONTH_CODES,
        COMMODITY_PREFIX,
    )

    month = determination_date.month
    year = determination_date.year
    lead_code = MONTH_TO_LEAD_CODE[month]

    # Next 코드
    lead_idx = ALL_MONTH_CODES.index(lead_code)
    next_code = ALL_MONTH_CODES[(lead_idx + 1) % 12]

    # 계약 연도 계산 (index_calculator._contract_year 로직 동일)
    def contract_year(code):
        c_month = FUTURES_CODE_TO_MONTH[code]
        return year + 1 if c_month <= month else year

    lead_year = contract_year(lead_code)
    next_year = contract_year(next_code)

    lead_ticker = f"{COMMODITY_PREFIX}{lead_code}{str(lead_year)[-2:]}"
    next_ticker = f"{COMMODITY_PREFIX}{next_code}{str(next_year)[-2:]}"

    # contracts에서 해당 심볼 검색
    sym_to_info = {sym: (price, exp) for sym, price, exp in contracts}

    if lead_ticker not in sym_to_info or next_ticker not in sym_to_info:
        return float('nan')

    lead_price, lead_exp = sym_to_info[lead_ticker]
    next_price, next_exp = sym_to_info[next_ticker]

    if next_price == 0:
        return float('nan')

    interval_d = (
        (next_exp.year - lead_exp.year) * 12
        + (next_exp.month - lead_exp.month)
    )
    if interval_d <= 0:
        interval_d = 1

    return (lead_price - next_price) / (next_price * interval_d)
