# `data/` — 입력 데이터 준비 가이드

이 디렉토리는 **비어 있는 상태로 배포**됩니다.
백테스트 입력으로 쓰이는 원시 시장 데이터(선물 결제가, T-Bill 금리)는 상용 벤더의 재배포
제한이 걸려 있어 공개 레포에 포함하지 않았습니다. 아래 명세대로 직접 내려받아 이 폴더에
넣으면 `src/`·`reference/`의 코드가 그대로 동작합니다.

---

## 1. WTI 원유 선물 일별 결제가 — `cl_settlements_2016-2026.csv`

| 항목 | 값 |
|---|---|
| 상품 | CME/NYMEX WTI Crude Oil Futures (`CL`) |
| 기간 | 2016-01-04 ~ 2026-03-18 (프로젝트 백테스트 구간) |
| 범위 | 매 거래일마다 **미도래 월물 12개 이상**의 결제가가 있어야 함 (DRA는 M1~M12를 후보로 씀) |
| 원 출처 | Databento (`GLBX.MDP3` 데이터셋, `CL.FUT`, schema `statistics` + `definition`) |
| 대안 | CME Group 공식 Settlement 파일, Bloomberg `CL{월코드}{연도} Comdty`, Refinitiv 등 |

**필수 컬럼 (Databento long-format 기준)**

| 컬럼 | 내용 | 예시 |
|---|---|---|
| `Trade date` | 거래일 | `2016-01-04` |
| `Symbol` | 월물 심볼 | `CLG6`, `CLG16` |
| `Settlement price` | 일별 정산가 (USD/배럴) | `36.76` |
| `expiration` | 해당 월물의 만기 시각 (UTC 타임스탬프 허용) | `2016-01-20T...` |
| `Cleared volume` | 청산 거래량 (유동성/슬리피지 분석에만 사용, 선택) | `123456` |
| `Open interest` | 미결제약정 (선택) | `234567` |

`index_calculator.load_futures_prices()` 가 `Symbol`의 월 코드와 `expiration`의 연도를 조합해
`CLG16` 형태의 내부 티커를 만든 뒤 wide 포맷(행=거래일, 열=월물)으로 피벗합니다.
인도월과 만기월이 어긋나는 케이스(예: 2월 인도물 `CLG16`의 만기는 1월)를 보정하는 로직이
포함되어 있으므로, 원본 컬럼명만 위와 맞추면 됩니다.

> 프로젝트 발표자료 Appendix에 Databento API 다운로더 스크립트 전문이 실려 있습니다.
> `db.Historical().timeseries.get_range(dataset="GLBX.MDP3", symbols="CL.FUT",
> stype_in="parent", schema="statistics"/"definition")` 를 연 단위로 나눠 호출하는 방식입니다.

---

## 2. 13주 미국 T-Bill 고할인율 — `13 Week Treasury Auction Rate High.csv`

TR(Total Return) 지수의 현금 담보 이자수익 계산에 쓰입니다.

| 항목 | 값 |
|---|---|
| 지표 | 13-Week (91일) US T-Bill Auction High Discount Rate (Bloomberg 티커 `USB3MTA`) |
| 주기 | 월간(또는 주간) — 영업일 기준으로 forward-fill 됨 |
| 원 출처 | U.S. Treasury (TreasuryDirect Auction Results), Moody's Analytics CSV export, FRED `DTB3` |

**형식** — `index_calculator.load_tbill_rates()` 는 Moody's Analytics CSV export 형식을 가정합니다.

- 상단 5행은 메타데이터 헤더로 건너뜀 (`skiprows=5`, 컬럼명 없음)
- 1열: 날짜 문자열 `YY-Mon` 또는 `Mon-YY` (예: `10-Jan`, `Jan-10`)
- 2열: 할인율 값 (% 단위, 코드가 100으로 나눠 소수로 변환)
- 결측은 `na` 로 표기 → NaN 처리 후 제거

다른 소스(FRED 등)를 쓸 경우, 위 형식으로 변환하거나 `load_tbill_rates()` 에
`pd.Series(index=월초 날짜, values=소수 금리)` 를 직접 넘기면 됩니다.

---

## 3. 커밋 금지

`.gitignore` 가 `data/*.csv`, `cl_settlements*.csv`, `*_positions.csv` 등을 차단합니다.
벤더 데이터와 그 파생 스냅샷(월물별 결제가가 그대로 들어 있는 `*_positions.csv`)은
공개 레포에 올리지 마십시오. 반면 지수 레벨·요약 통계처럼 원가격을 복원할 수 없는
집계 산출물은 `results/` 에 포함되어 있습니다.
