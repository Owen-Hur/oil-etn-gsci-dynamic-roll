# WTI S&P GSCI Dynamic Roll Algorithm (DRA) 방법론 및 백테스팅 구현 가이드

> 기준 문서: S&P Dow Jones Indices — S&P GSCI Dynamic Roll Methodology (December 2024)  
> 적용 대상: WTI Crude Oil (CL) 단일 종목  
> 작성 목적: ETN 발행사 헷지 포지션 운용 및 백테스팅 구현

---

## 목차

1. [Dynamic Roll 개요](#1-dynamic-roll-개요)
2. [Implied Roll Yield 계산](#2-implied-roll-yield-계산)
3. [Dynamic Roll Algorithm (DRA)](#3-dynamic-roll-algorithm-dra)
4. [월간 롤 실행 스케줄](#4-월간-롤-실행-스케줄)
5. [현실적 비용 반영](#5-현실적-비용-반영)
6. [결정-실행 시차 리스크](#6-결정-실행-시차-리스크)
7. [Look-Ahead Bias 방지](#7-look-ahead-bias-방지)
8. [백테스팅 구현](#8-백테스팅-구현)
9. [데이터 수집 가이드](#9-데이터-수집-가이드)
10. [민감도 분석 시나리오](#10-민감도-분석-시나리오)
11. [부록: 주요 용어 정리](#11-부록-주요-용어-정리)

---

## 1. Dynamic Roll 개요

### 핵심 목표

시장 상황(콘탱고/백워데이션)에 따라 **최적의 선물 계약월**을 자동으로 선택하여, 롤오버 과정에서 발생하는 비용을 최소화하고 수익을 최대화한다.

### 두 가지 시장 상황

```
[Contango 시장]
가격이 원월물로 갈수록 상승
→ 음의 롤 수익률 발생
→ 손실 최소화를 위해 기울기가 완만한 원월물로 이동

[Backwardation 시장]
가격이 원월물로 갈수록 하락
→ 양의 롤 수익률 발생
→ 수익 극대화를 위해 근월물 유지
```

### WTI(CL) 기본 파라미터

| 항목 | 값 |
|------|-----|
| 종목 코드 | CL |
| Rank Order | 3 |
| 알고리즘 | DRA(3) |
| Optimum Set 크기 | 상위 3개 월물 |
| Contract Size | 1,000 bbl |

---

## 2. Implied Roll Yield 계산

### 공식

```
Implied Roll Yield_C(i,j) = [C(i,j-1) - C(i,j)] / [C(i,j) × Interval_D]

C(i,j-1)   : (j-1)번째 계약의 가격 (앞 월물)
C(i,j)     : j번째 계약의 가격 (뒤 월물)
Interval_D : 두 계약 간 개월 수 = M[C(i,j)] - M[C(i,j-1)]
```

### 핵심: 연속 페어(Consecutive Pair)로만 계산

방법론은 항상 **인접한 두 계약** 사이의 수익률을 계산한다.

```
올바른 계산:
M1→M2: (C_M1 - C_M2) / (C_M2 × 1)
M2→M3: (C_M2 - C_M3) / (C_M3 × 1)
M3→M4: (C_M3 - C_M4) / (C_M4 × 1)

잘못된 계산 (Slope Score 방식, 방법론 아님):
M1→M3: (C_M1 - C_M3) / (C_M3 × 2)  ← 방법론에 없음
```

### 연속 페어 방식 vs Slope Score 비교

| 구분 | 연속 페어 (DRA 방식) | Slope Score |
|------|---------------------|-------------|
| 커브 분석 | 국소적 기울기 포착 | 전체 커브 기울기 |
| 특정 구간 왜곡 감지 | 가능 | 불가 |
| 실제 롤 비용 반영 | 간접적 | 직접적 |
| 근월물 편향 | 있음 | 없음 |
| 방법론 준수 | O | X |

### 정렬 기준

```
항상 Implied Roll Yield 값이 큰 순서로 정렬 (내림차순)

Backwardation: 양수값 중 가장 큰 것이 1위
Contango:      음수값 중 가장 덜 음수인 것(= 가장 큰 값)이 1위

→ 콘탱고에서도 절댓값 기준이 아닌 실제 값 기준으로 정렬
```

### 계산 예시

```
[Backwardation 시장]
M1=$80.50, M2=$80.20, M3=$79.90, M4=$79.60, M5=$79.40

M1→M2: (80.50-80.20)/(80.20×1) = +0.3739%
M2→M3: (80.20-79.90)/(79.90×1) = +0.3755%
M3→M4: (79.90-79.60)/(79.60×1) = +0.3769%  ← 1위
M4→M5: (79.60-79.40)/(79.40×1) = +0.2519%

[Contango 시장]
M1=$75.00, M2=$76.00, M3=$77.00, M4=$78.00

M1→M2: (75.00-76.00)/(76.00×1) = -1.3158%
M2→M3: (76.00-77.00)/(77.00×1) = -1.2987%
M3→M4: (77.00-78.00)/(78.00×1) = -1.2821%  ← 1위 (덜 음수)
```

---

## 3. Dynamic Roll Algorithm (DRA)

### WTI DRA(3) 적용 프로세스

```
Step 1: 자체 판정으로 확정된 Eligible Contracts 목록 확인

Step 2: 연속 페어별 Implied Roll Yield 계산

Step 3: Implied Roll Yield 내림차순 정렬

Step 4: 상위 3개 선택
        Optimum Set = {Best(1), Best(2), Best(3)}

Step 5: Dynamic Roll Parity Principle 적용
        → 현재 보유(Rolled-out) 계약이 Optimum Set에 포함?
           YES: 현재 계약 그대로 유지 (거래비용 절약)
           NO:  Best(1)으로 롤오버 실행
```

### Dynamic Roll Parity Principle 케이스별 예시

```
Optimum Set = {M3, M2, M1} 가정

Case 1: 현재 M4 보유
→ M4 ∉ {M3, M2, M1}
→ Best(1) = M3으로 롤오버

Case 2: 현재 M2 보유
→ M2 ∈ {M3, M2, M1}
→ 그대로 M2 유지 (롤오버 없음)

Case 3: 현재 M1 보유 (당월 만기 임박)
→ M1 ∈ {M3, M2, M1}이지만 만기 도래
→ 롤아웃 필요 → Best(1) = M3으로 롤오버
```

---

## 4. 월간 롤 실행 스케줄

### 월간 프로세스

```
매월 6번째 영업일 (Roll Determination Date)
→ 당일 Settlement Price 기준 Implied Roll Yield 계산
→ DRA(3) 실행 → 롤인 월물 결정 (이후 변경 없음)

매월 6~10번째 영업일 (Roll Execution Period)
→ 6영업일에 결정된 롤인 월물로 고정
→ 매일 총 포지션의 20%씩 롤오버 실행
→ 5일에 걸쳐 분산 실행
→ 각 실행일 Settlement Price를 체결가로 사용
→ 롤 기간 중 월물 변경 없음 (6영업일 결정 유지)
```

### WTI(CL) 만기일 계산 규칙

```
인도월 전전월 25번째 캘린더일 전 3영업일

예시: 2025년 3월물 만기
→ 전전월: 2025년 2월
→ 2025년 2월 25일 전 3영업일
→ 2025년 2월 20일 (만기일)
```

```python
from pandas.tseries.offsets import BDay
import pandas as pd

def get_cl_expiry(year, month):
    """
    WTI(CL) 만기일 계산
    규칙: 인도월 전전월 25번째 캘린더일 전 3영업일
    """
    if month == 1:
        ref_year, ref_month = year - 1, 12
    else:
        ref_year, ref_month = year, month - 1

    ref_date = pd.Timestamp(ref_year, ref_month, 25)

    while ref_date.weekday() >= 5:
        ref_date -= pd.Timedelta(days=1)

    expiry = ref_date - BDay(3)
    return expiry
```

---

## 5. 현실적 비용 반영

### 적용 비용 항목 (2가지)

ETN 발행사 헷지 포지션 기준으로 다음 2가지 비용을 반영한다.

1. 거래 수수료
2. 슬리피지 (시장 충격)

### 롤오버 실행 방식

```
총 포지션: N 계약
일별 실행량: N × 20% 계약
실행 기간: 5~9영업일 (5일간)
체결가: 각 실행일 Settlement Price
```

---

### (1) 거래 수수료

```
단가: $1.25~1.55/계약 (거래소 + 브로커 합산, 양방향)
  - CME 거래소 수수료: ~$0.85/계약
  - 브로커 수수료: ~$0.30~0.50/계약 (대규모 협상 기준)
  - 청산 수수료: ~$0.10~0.20/계약

일별 수수료 = 단가 × (N × 20%) × 2    [매도 + 매수]
5일 누적   = 단가 × N × 2
```

> 수수료 총액은 분산 실행 여부와 무관하게 동일하다.
> (일별 현금흐름 타이밍만 다름)

---

### (2) 슬리피지 (Square Root Market Impact Model)

```
일별 슬리피지 = σ × √(Q_daily / ADV) × Price × CS

σ        : 해당 월물 일간 변동성
Q_daily  : 하루 실행량 = N × 20%
ADV      : 해당 월물 일평균거래량 (Average Daily Volume)
Price    : 해당 월물 가격 (Settlement Price)
CS       : 계약 규모 (1,000 bbl)

5일 누적 슬리피지 = 일별 슬리피지 × 5
                 = σ × √(N×0.2 / ADV) × Price × CS × 5
```

### 20% 분산 실행의 슬리피지 효과

```
한 번에 실행:    σ × √(N / ADV) × Price × CS
20% 분산 실행:  σ × √(N×0.2 / ADV) × Price × CS × 5

비율: √0.2 × 5 ≈ 2.236

→ 분산 실행 시 누적 슬리피지는 약 2.2배 증가
→ 단, 단일 시점 최대 충격은 √5 ≈ 2.24배 감소
→ 대규모 포지션에서 단일 시점 충격 방지가 더 중요하므로 분산 실행 유지
```

---

### 통합 비용 구조

```
총 롤오버 비용 (1회 기준) =

  거래 수수료
  단가 × N × 2

+ 슬리피지
  σ × √(N×0.2 / ADV) × Price × CS × 5

= 총 비용 ($/롤오버)

순 IRY = IRY - (총 비용 / 포지션 평가액)
```

---

## 6. 결정-실행 시차 리스크

### 문제 구조

```
3영업일 (Roll Determination Date):
→ 종가 기준으로 Implied Roll Yield 계산 및 롤인 월물 결정

5~9영업일 (실제 실행):
→ Settlement Price로 체결
```

### 체결가 처리 방법

```
각 실행일 d (d = 5, 6, 7, 8, 9영업일):
→ 체결가 = d일 Settlement Price
→ 별도 VWAP 계산 없이 Settlement Price 직접 사용
```

---

## 7. Look-Ahead Bias 방지

### 핵심 원칙

```
백테스팅의 각 시점 t에서는
→ t 시점 이전에 실제로 알 수 있었던 데이터만 사용
→ t 시점 이후에 발생한 데이터는 절대 사용 불가
```

---

### Rule 1: IRY 계산 기준

**예시: 2025년 3월 Roll Determination Date = 3월 6일 (6번째 영업일)**

```
사용 가능한 데이터:
→ 2025년 2월까지의 MDVT/MDOI (이미 확정된 과거)

사용 불가한 데이터:
→ 2025년 3월 MDVT/MDOI (해당월 진행 중, 아직 미확정)

적용:
→ 3월 6일 Settlement Price 기준으로 IRY 계산
→ 해당일 이후 데이터 사용 금지
```

---

### Rule 2: 가격 데이터 사용 기준

**예시: 2025년 3월 Roll Determination Date = 3월 4일**

```
사용 가능:
→ 3월 4일 종가 (당일 장 마감 후 확정)
→ 3월 4일 이전 모든 가격 데이터

사용 불가:
→ 3월 5일 이후 가격 (아직 발생하지 않음)
→ 5~9일 체결가를 결정 로직에 역으로 반영하는 것 금지

적용:
→ Implied Roll Yield 계산 = 3월 4일 종가만 사용
→ 이후 가격 움직임과 무관하게 결정 고정
```

---

### Rule 3: 실행 가격 기준

**예시: 2025년 3월 5~9영업일 (롤 실행 구간)**

```
각 실행일 d에서:
→ d일 Settlement Price = 당일 장 마감 후 확정
→ d일 체결가로 사용 가능

사용 불가:
→ d+1일 이후 가격으로 d일 체결가를 소급 조정하는 것 금지
→ 예: 3월 7일 가격이 더 유리하다고 해서
       3월 5일 체결을 3월 7일 가격으로 바꾸는 것 금지
```

---

### Rule 4: 코드 구현 시 명시적 날짜 체크

**잘못된 구현 예시 (Look-Ahead Bias 있음):**

```python
# 위험: 전체 데이터프레임에서 한 번에 판정
# 미래 데이터가 섞여 들어올 수 있음
eligible = df[df['mdvt'] >= 1.2e9]  # 전체 기간 일괄 판정 → 위험
```

**올바른 구현 예시:**

```python
def get_eligible_contracts(df, determination_date):
    """
    determination_date 기준으로
    전월 말일까지의 데이터만 사용하여 Eligible 판정
    """
    # 전월 말일 계산
    cutoff = determination_date - pd.offsets.MonthBegin(1)

    # 명시적으로 과거 데이터만 필터링
    past_data = df[df.index < cutoff]

    if len(past_data) == 0:
        return []

    mdvt = calculate_mdvt(past_data)
    mdoi = calculate_mdoi(past_data)

    return check_eligibility(mdvt, mdoi)
```

---

### Rule 5: Look-Ahead Bias 체크리스트

매월 Roll Determination Date 기준으로 다음을 확인한다.

```
체크 1: MDVT/MDOI 계산에 사용된 데이터가
        전월 말일 이전 데이터인가?
        → YES: 통과 / NO: Look-Ahead Bias

체크 2: 롤인 월물 결정이 6영업일 이후 데이터 없이
        이루어졌는가?
        → YES: 통과 / NO: Look-Ahead Bias

체크 3: 롤인 월물 결정이
        Roll Determination Date 당일 이전 확정 정보만으로
        이루어졌는가?
        → YES: 통과 / NO: Look-Ahead Bias

체크 4: 실행 구간(5~9영업일) 체결가가
        각 해당일 Settlement Price인가?
        → YES: 통과 / NO: Look-Ahead Bias
```

---

### 전체 타임라인 요약

```
2025년 3월 기준:

2월 28일 (2월 마지막 영업일)
→ 2월 MDVT/MDOI 확정
→ 이 데이터까지만 Eligible 판정에 사용 가능

3월 6일 (6번째 영업일, Roll Determination Date)
→ 전월(2월) MDVT/MDOI로 Eligible 판정       ← Rule 1
→ 3월 6일 Settlement Price로 Implied Roll Yield 계산     ← Rule 2
→ DRA(3) 실행, 롤인 월물 확정 (이후 변경 불가)

3월 6~12일 (6~10번째 영업일, 실행 구간)
→ 각 해당일 Settlement Price로 체결가 적용   ← Rule 3
→ 매일 20%씩 롤오버 실행
→ 각 실행일 이후 데이터는 결정 로직에 절대 사용 불가
```

---

## 8. 백테스팅 구현

### 전체 백테스팅 흐름

```
for each month t:

  [1단계] Roll Determination Date (6영업일) 처리
    → 당일 Settlement Price 기준 Implied Roll Yield 계산
    → 연속 페어별 계산
    → DRA(3) 실행 → Optimum Set 결정
    → Dynamic Roll Parity Principle 적용
    → 롤인 월물 확정 (이후 변경 없음)

  [3단계] 순수익률 계산
    순 IRY = IRY - (수수료 + 슬리피지) / 포지션 평가액

  [4단계] 롤 실행 (6~10영업일, 매일 20%)
    for each day d in [6, 7, 8, 9, 10]:
        실행량 = N × 20%
        체결가 = Settlement Price_d    (Rule 2 준수)

        수수료  = 단가 × 실행량 × 2
        슬리피지 = σ × √(실행량 / ADV_d) × 체결가 × CS

        일별 비용 = 수수료 + 슬리피지
        누적 비용 += 일별 비용

    평균 체결가 = Σ(체결가_d × 20%)

  [5단계] 성과 기록
    → 월별 롤 수익률 (비용 차감 전/후)
    → 총 비용 내역 (수수료 / 슬리피지 분리)
    → 선택된 롤인 월물
    → 후보군 월물 수
```

### 핵심 검증 포인트

```
1. Look-Ahead Bias 검증
   → 체크리스트 4개 항목 전부 통과 여부 확인

2. 비용 반영 전후 비교
   → 순수 IRY vs 비용 차감 후 IRY 분리 기록

3. 연도 경계 처리 검증
   → 10~1월 구간 후보군 변화 모니터링
   → 자체 판정으로 원월물 편입 여부 확인

4. 시장 상황별 성과 분리
   → Contango 구간 vs Backwardation 구간 성과 비교
   → 선택 월물 분포 히스토그램
```

---

## 9. 데이터 수집 가이드

### 필요 데이터 목록

| 데이터 | 월물 범위 | 소스 |
|--------|---------|------|
| Settlement Price | M1~M12 | cl_settlements_2016-2026.csv |
| Contract Expiry Date | 전체 | expiration 컬럼 또는 Python 규칙 계산 |

### 데이터 구조

```
cl_settlements_2016-2026.csv:
컬럼: Trade date, Symbol, Settlement price, Cleared volume, Open interest, expiration
형식: Long format (하루에 여러 월물 데이터 동시 존재)
기간: 2015-12-31 ~ 2026-03-18

Wide format으로 변환하여 사용:
index = 거래일
columns = 월물 심볼 (CLG16, CLH16, ...)
values = Settlement price
```

### Contract Expiry Date 계산

```python
def get_cl_expiry_table(start_year=1985, end_year=2027):
    """
    WTI(CL) 전체 만기일 테이블 생성
    """
    expiry_dates = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            expiry = get_cl_expiry(year, month)
            expiry_dates.append({
                'contract_year': year,
                'contract_month': month,
                'expiry_date': expiry
            })
    return pd.DataFrame(expiry_dates)

expiry_df = get_cl_expiry_table()
expiry_df.to_csv("cl_expiry_dates.csv", index=False)
```

---

## 10. 민감도 분석 시나리오

### 왜 필요한가

비용 추정치에는 불확실성이 존재한다. 슬리피지는 ADV와 변동성 추정에 의존하며, 수수료는 브로커 협상 결과에 따라 달라진다. 단일 수치로 결과를 제시하면 신뢰도가 낮다. ETN 발행사 입장에서는 최악의 비용 시나리오에서도 전략이 수익성을 유지하는지 검증하는 것이 중요하다. 결과를 범위로 제시함으로써 실무적 의사결정의 신뢰도를 높인다.

### 시나리오 정의

| 시나리오 | 슬리피지 ADV 가정 | 수수료 단가 | 설명 |
|---------|----------------|-----------|------|
| Conservative | 실제 ADV × 0.5 | $1.55/계약 | 유동성 최악, 수수료 최대 |
| Base | 실제 ADV × 1.0 | $1.40/계약 | 평균적인 시장 환경 |
| Optimistic | 실제 ADV × 1.5 | $1.25/계약 | 유동성 양호, 수수료 최소 |

### 적용 방법

```
동일한 백테스팅 로직을 3가지 비용 가정으로 각각 실행

비교 지표:
→ 시나리오별 누적 롤 수익률 범위
→ 비용이 IRY를 초과하는 구간 식별 (전략 손익분기점)
→ Contango 구간에서의 시나리오별 성과 차이

결과 제시 방식:
→ Base 기준 수익률 + Conservative/Optimistic 범위
→ 예: "연간 롤 수익률 Base +1.2%, 범위 +0.8%~+1.5%"
```

---

## 11. 부록: 주요 용어 정리

| 용어 | 정의 |
|------|------|
| DRA(k) | k번째 Dynamic Roll Algorithm. k = Optimum Set 크기 |
| Dynamic Roll Parity Principle | Rolled-out Contract가 Optimum Set에 포함되면 유지, 미포함이면 Best(1)으로 변경 |
| Optimum Set | DRA(k)에 의해 선택된 상위 k개 월물 집합 |
| Rank Order | 상품별 DRA(k)의 k값. WTI = 3 |
| Roll Determination Date | 매월 6번째 영업일. 롤인 월물 결정 기준일 |
| Roll Execution Period | 매월 6~10번째 영업일. 실제 롤 실행 구간 |
| Implied Roll Yield | 연속된 두 계약 간 내재 롤 수익률 |
| Interval D | 두 계약 간 개월 수 |
| Look-Ahead Bias | 백테스팅 시 미래 정보를 현재 시점에 사용하는 오류 |
| Settlement Price | CME 공식 정산가. 체결가 기준으로 사용 |

---

*작성 기준: S&P GSCI Dynamic Roll Methodology (December 2024)*  
*적용 종목: WTI Crude Oil (CL), CME*
