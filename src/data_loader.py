"""
data_loader.py — 데이터 로딩 및 전처리
index_calculator.py의 load_futures_prices 재활용
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from index_calculator import load_futures_prices


def load_data(filepath: str):
    """
    CSV 로딩 후 Wide format DataFrame과 expiration_map 반환.

    Returns
    -------
    prices_wide : DataFrame  (index=거래일, columns=월물 심볼 CLG16 등)
    expiration_map : dict    (key=심볼, value=만기일 pd.Timestamp)
    """
    # load_futures_prices는 Wide format DataFrame 반환
    prices_wide = load_futures_prices(filepath)

    # expiration_map 구성: CSV에서 직접 읽어 심볼→만기일 매핑
    raw = pd.read_csv(filepath)
    raw["expiration"] = pd.to_datetime(raw["expiration"], utc=True).dt.tz_localize(None)

    # 내부 티커 생성 (load_futures_prices와 동일 로직)
    from index_calculator import FUTURES_CODE_TO_MONTH, COMMODITY_PREFIX
    month_num_to_code = {v: k for k, v in FUTURES_CODE_TO_MONTH.items()}
    exp_year = raw["expiration"].dt.year
    exp_month_code = raw["expiration"].dt.month.map(month_num_to_code)
    raw["_internal_ticker"] = (
        COMMODITY_PREFIX + exp_month_code + exp_year.astype(str).str[-2:]
    )

    # 심볼별 최초 만기일 추출
    ticker_exp = (
        raw[["_internal_ticker", "expiration"]]
        .drop_duplicates(subset=["_internal_ticker"])
        .set_index("_internal_ticker")["expiration"]
    )
    expiration_map = ticker_exp.to_dict()

    return prices_wide, expiration_map


def get_contracts_for_date(prices_wide: pd.DataFrame, expiration_map: dict,
                            date: pd.Timestamp, n: int = 12):
    """
    특정 날짜 기준 M1~Mn 월물 반환.
    - date 이후 만기인 월물만 포함
    - 해당 날짜 Settlement Price 있는 월물만 포함
    - 만기 가까운 순서 정렬

    Returns
    -------
    list of (심볼, 가격, 만기일) — 만기 오름차순
    """
    if date not in prices_wide.index:
        return []

    row = prices_wide.loc[date]
    result = []

    for symbol, expiry in expiration_map.items():
        if pd.isna(expiry):
            continue
        # 만기가 date 이후인 월물만
        if expiry <= date:
            continue
        # 해당 날짜 가격 존재 여부
        if symbol not in row.index:
            continue
        price = row[symbol]
        if pd.isna(price):
            continue
        result.append((symbol, price, expiry))

    # 만기 오름차순 정렬 후 상위 n개
    result.sort(key=lambda x: x[2])
    return result[:n]
