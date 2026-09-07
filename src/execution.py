"""
execution.py — 롤오버 실행 및 체결 금액 계산
추가 비용 없음. 순수 체결 금액만 계산.
"""
import pandas as pd
from typing import List, Dict


def calculate_roll_execution(
    prices_wide: pd.DataFrame,
    roll_in: str,
    roll_out: str,
    determination_date: pd.Timestamp,
    business_days: List[pd.Timestamp],
    n_contracts: int,
    contract_size: int = 1000,
) -> Dict:
    """
    롤오버 체결 금액 계산.

    Parameters
    ----------
    prices_wide      : Wide format 가격 DataFrame
    roll_in          : 롤인 월물 심볼
    roll_out         : 롤아웃 월물 심볼
    determination_date : Roll Determination Date (6영업일)
    business_days    : 해당 월 전체 영업일 목록
    n_contracts      : 총 보유 계약 수
    contract_size    : 계약 단위 (기본 1000 bbl)

    Returns
    -------
    dict:
        avg_price_in           : 롤인 월물 평균 체결가
        avg_price_out          : 롤아웃 월물 평균 체결가
        total_execution_amount : 총 체결 금액 ($)
        daily_details          : list of daily dict
    """
    from index_calculator import ROLL_START_BD, ROLL_END_BD, WEIGHT_STEP

    # 해당 월 6~10영업일 추출
    roll_days = []
    for i, d in enumerate(business_days):
        bd_num = i + 1  # 1-based
        if ROLL_START_BD <= bd_num <= ROLL_END_BD:
            roll_days.append(d)

    if not roll_days:
        return {
            "avg_price_in": float('nan'),
            "avg_price_out": float('nan'),
            "total_execution_amount": 0.0,
            "daily_details": [],
        }

    daily_qty = n_contracts * WEIGHT_STEP  # 매일 실행량
    daily_details = []
    total_amount = 0.0
    price_in_list = []
    price_out_list = []

    # 롤 기간 중 가용 일수로 균등 분배 처리
    available_days = []
    for d in roll_days:
        in_ok = (roll_in in prices_wide.columns and
                 d in prices_wide.index and
                 not pd.isna(prices_wide.at[d, roll_in]))
        out_ok = (roll_out in prices_wide.columns and
                  d in prices_wide.index and
                  not pd.isna(prices_wide.at[d, roll_out]))
        if in_ok and out_ok:
            available_days.append(d)

    n_avail = len(available_days)
    if n_avail == 0:
        return {
            "avg_price_in": float('nan'),
            "avg_price_out": float('nan'),
            "total_execution_amount": 0.0,
            "daily_details": [],
        }

    # 가용일 수에 따라 weight 재분배 (원래 5일 기준 20%씩, 누락 시 균등)
    weight_per_day = 1.0 / n_avail

    for d in available_days:
        p_in = prices_wide.at[d, roll_in]
        p_out = prices_wide.at[d, roll_out]

        qty = n_contracts * weight_per_day
        roll_in_amount = p_in * qty * contract_size
        roll_out_amount = p_out * qty * contract_size
        total_amount += roll_in_amount  # 롤인 기준 체결 금액

        price_in_list.append((p_in, weight_per_day))
        price_out_list.append((p_out, weight_per_day))

        daily_details.append({
            "date": d,
            "roll_out_price": p_out,
            "roll_in_price": p_in,
            "roll_out_amount": roll_out_amount,
            "roll_in_amount": roll_in_amount,
            "roll_out_weight": weight_per_day,
            "roll_in_weight": weight_per_day,
        })

    avg_in = sum(p * w for p, w in price_in_list)
    avg_out = sum(p * w for p, w in price_out_list)

    return {
        "avg_price_in": avg_in,
        "avg_price_out": avg_out,
        "total_execution_amount": total_amount,
        "daily_details": daily_details,
    }
