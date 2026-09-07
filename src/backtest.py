"""
backtest.py — 백테스팅 메인 루프
DRA 동적 롤오버 vs 정적 롤오버(ETN 벤치마크) 비교
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from typing import List, Dict

from index_calculator import (
    get_cme_business_days,
    MONTH_TO_LEAD_CODE,
    FUTURES_CODE_TO_MONTH,
    ALL_MONTH_CODES,
    COMMODITY_PREFIX,
    ROLL_START_BD,
    ROLL_END_BD,
)
from src.data_loader import get_contracts_for_date
from src.roll_yield import calculate_iry, get_static_iry
from src.dra import run_dra
from src.execution import calculate_roll_execution


def run_backtest(
    prices_wide: pd.DataFrame,
    expiration_map: dict,
    start_date: str = "2016-01-04",
    end_date: str = "2026-03-18",
    n_contracts: int = 1000,
    contract_size: int = 1000,
) -> List[Dict]:
    """
    월별 DRA vs 정적 롤오버 백테스팅 실행.

    Returns
    -------
    List of monthly result dicts
    """
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)

    # CME 영업일 전체 목록
    all_bdays = get_cme_business_days(start_ts, end_ts)

    # 월별로 그룹화
    months = sorted(set((d.year, d.month) for d in all_bdays))

    # 동적 전략: 현재 보유 월물 추적
    # 초기값: 첫 달의 Lead Contract
    first_month = months[0]
    first_lead_code = MONTH_TO_LEAD_CODE[first_month[1]]

    def get_lead_ticker(year, month):
        lead_code = MONTH_TO_LEAD_CODE[month]
        c_month = FUTURES_CODE_TO_MONTH[lead_code]
        c_year = year + 1 if c_month <= month else year
        return f"{COMMODITY_PREFIX}{lead_code}{str(c_year)[-2:]}"

    current_dynamic_contract = get_lead_ticker(first_month[0], first_month[1])

    results = []

    for year, month in months:
        # 해당 월 영업일
        month_bdays = [d for d in all_bdays if d.year == year and d.month == month]
        if len(month_bdays) < ROLL_START_BD:
            continue

        # Step 1: 6영업일 = Roll Determination Date (0-indexed: [5])
        determination_date = month_bdays[ROLL_START_BD - 1]

        # Step 2: M1~M12 월물 수집 (Look-Ahead Bias 방지: determination_date 당일 가격만)
        contracts = get_contracts_for_date(prices_wide, expiration_map, determination_date, n=12)
        if len(contracts) < 2:
            continue

        # Step 3: 시장 상황 판단
        m1_price = contracts[0][1]
        m2_price = contracts[1][1] if len(contracts) > 1 else m1_price
        market_condition = "Backwardation" if m1_price > m2_price else "Contango"

        # ── 동적 롤오버 (DRA) ──

        # Step 4: IRY 계산
        ranked = calculate_iry(contracts)

        # Step 5: DRA(3) 실행
        dra_result = run_dra(ranked, current_dynamic_contract)
        dynamic_roll_in = dra_result["roll_in"]
        dynamic_iry = dra_result["selected_iry"]
        optimum_set = dra_result["optimum_set"]

        # 롤아웃: 현재 보유 월물
        dynamic_roll_out = current_dynamic_contract
        roll_required = dra_result["roll_required"]

        # Step 6: 동적 체결 금액 계산
        # roll_required=False → 현재 월물 유지, 거래 없음 (0)
        if roll_required:
            dyn_exec = calculate_roll_execution(
                prices_wide, dynamic_roll_in, dynamic_roll_out,
                determination_date, month_bdays, n_contracts, contract_size
            )
        else:
            dyn_exec = {
                "avg_price_in": float('nan'),
                "avg_price_out": float('nan'),
                "total_execution_amount": 0.0,
                "daily_details": [],
            }

        # 다음 달 현재 보유 월물 업데이트
        current_dynamic_contract = dynamic_roll_in

        # ── 정적 롤오버 (ETN 벤치마크) ──

        # Step 7: 정적 Lead→Next IRY 계산
        static_iry = get_static_iry(contracts, determination_date)

        # 정적 롤 월물 결정
        lead_code = MONTH_TO_LEAD_CODE[month]
        lead_idx = ALL_MONTH_CODES.index(lead_code)
        next_code = ALL_MONTH_CODES[(lead_idx + 1) % 12]

        def c_year(code):
            cm = FUTURES_CODE_TO_MONTH[code]
            return year + 1 if cm <= month else year

        static_roll_out = f"{COMMODITY_PREFIX}{lead_code}{str(c_year(lead_code))[-2:]}"
        static_roll_in = f"{COMMODITY_PREFIX}{next_code}{str(c_year(next_code))[-2:]}"

        # Step 8: 정적 체결 금액 계산
        sta_exec = calculate_roll_execution(
            prices_wide, static_roll_in, static_roll_out,
            determination_date, month_bdays, n_contracts, contract_size
        )

        # Step 9: 결과 저장
        iry_saving = (dynamic_iry - static_iry) if not (np.isnan(dynamic_iry) or np.isnan(static_iry)) else float('nan')
        exec_diff = dyn_exec["total_execution_amount"] - sta_exec["total_execution_amount"]

        results.append({
            "date": determination_date,
            "year": year,
            "month": month,
            "market_condition": market_condition,
            "static_roll_out_contract": static_roll_out,
            "static_roll_contract": static_roll_in,
            "dynamic_roll_out_contract": dynamic_roll_out,
            "dynamic_roll_contract": dynamic_roll_in,
            "roll_required": roll_required,
            "optimum_set": str(optimum_set),
            "static_iry": static_iry,
            "dynamic_iry": dynamic_iry,
            "iry_saving": iry_saving,
            "static_execution_amount": sta_exec["total_execution_amount"],
            "dynamic_execution_amount": dyn_exec["total_execution_amount"],
            "execution_amount_diff": exec_diff,
        })

    return results
