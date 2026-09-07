"""
run_backtest.py — src/ 모듈 파이프라인 실행 엔트리포인트

DRA(3) 동적 롤오버와 정적(Lead→Next) 롤오버를 월 단위로 비교하고,
월별 결과를 CSV로 저장한다.

사용법:
    python run_backtest.py [--futures data/cl_settlements_2016-2026.csv]
                           [--start 2016-01-04] [--end 2026-03-18]
                           [--out results/monthly_backtest.csv]

입력 데이터 준비 방법은 data/README.md 참고.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from src.data_loader import load_data
from src.backtest import run_backtest

ROOT = Path(__file__).parent
DEFAULT_FUTURES = ROOT / "data" / "cl_settlements_2016-2026.csv"


def main() -> None:
    p = argparse.ArgumentParser(description="WTI DRA(3) vs 정적 롤오버 백테스트")
    p.add_argument("--futures", default=str(DEFAULT_FUTURES),
                   help="WTI 선물 결제가 CSV 경로")
    p.add_argument("--start", default="2016-01-04", help="백테스트 시작일")
    p.add_argument("--end", default="2026-03-18", help="백테스트 종료일")
    p.add_argument("--contracts", type=int, default=1000, help="보유 계약 수")
    p.add_argument("--out", default=str(ROOT / "results" / "monthly_backtest.csv"),
                   help="월별 결과 CSV 저장 경로")
    args = p.parse_args()

    futures_path = Path(args.futures)
    if not futures_path.exists():
        raise SystemExit(
            f"선물 결제가 파일을 찾을 수 없습니다: {futures_path}\n"
            f"data/README.md 의 데이터 명세를 참고해 파일을 준비하세요."
        )

    print(f"[1/3] 데이터 로딩 — {futures_path}")
    prices_wide, expiration_map = load_data(str(futures_path))
    print(f"      거래일 {len(prices_wide):,}일 / 월물 {len(expiration_map):,}개")

    print(f"[2/3] 백테스트 실행 — {args.start} ~ {args.end}")
    results = run_backtest(
        prices_wide, expiration_map,
        start_date=args.start, end_date=args.end,
        n_contracts=args.contracts,
    )
    df = pd.DataFrame(results)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"[3/3] 저장 완료 — {out_path} ({len(df)}개월)")

    rolled = int(df["roll_required"].sum())
    print()
    print(f"  총 분석 월수      : {len(df)}")
    print(f"  DRA 실제 롤 실행  : {rolled} ({rolled / len(df):.0%})")
    print(f"  월물 유지(거래 X) : {len(df) - rolled}")
    print(f"  IRY 절감액 합계   : {df['iry_saving'].sum():.4f}")
    print(f"  정적 총 체결금액  : ${df['static_execution_amount'].sum():,.0f}")
    print(f"  동적 총 체결금액  : ${df['dynamic_execution_amount'].sum():,.0f}")


if __name__ == "__main__":
    main()
