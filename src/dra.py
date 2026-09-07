"""
dra.py — DRA(3) 알고리즘
Dynamic Roll Parity Principle 적용
"""
from typing import List, Tuple, Dict


def run_dra(ranked_contracts: List[Tuple], current_contract: str) -> Dict:
    """
    DRA(3) 실행.

    Parameters
    ----------
    ranked_contracts : [(심볼, IRY), ...] — IRY 내림차순
    current_contract : 현재 보유 월물 심볼

    Returns
    -------
    dict:
        roll_in         : 롤인 월물 심볼
        roll_required   : bool
        optimum_set     : [심볼1, 심볼2, 심볼3]
        selected_iry    : 선택된 IRY 값
    """
    rank_order = 3
    top_n = min(rank_order, len(ranked_contracts))
    optimum_set = [sym for sym, _ in ranked_contracts[:top_n]]
    optimum_iry = {sym: iry for sym, iry in ranked_contracts[:top_n]}

    if not optimum_set:
        # 후보 없음 — 현재 보유 유지
        return {
            "roll_in": current_contract,
            "roll_required": False,
            "optimum_set": [],
            "selected_iry": float('nan'),
        }

    best_sym = optimum_set[0]
    best_iry = ranked_contracts[0][1]

    if current_contract in optimum_set:
        # Dynamic Roll Parity: 현재 보유가 Optimum Set 내 → 유지
        return {
            "roll_in": current_contract,
            "roll_required": False,
            "optimum_set": optimum_set,
            "selected_iry": optimum_iry[current_contract],
        }
    else:
        # 미포함 → Best(1)으로 롤오버
        return {
            "roll_in": best_sym,
            "roll_required": True,
            "optimum_set": optimum_set,
            "selected_iry": best_iry,
        }
