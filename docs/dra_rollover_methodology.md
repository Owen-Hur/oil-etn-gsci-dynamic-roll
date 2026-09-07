# WTI DRA(3) 동적 롤오버 방법론

> 기준 문서: *S&P GSCI Dynamic Roll Methodology* (S&P Dow Jones Indices, December 2024)
> 구현 파일: `ws_dra_index.py`

---

## 1. 개요

이 구현은 **S&P GSCI Dynamic Roll Algorithm(DRA)** 을 WTI 원유 단일 상품 선물 지수에 맞게 각색한 것이다. 정적 롤오버(매월 고정된 근월물→차근월물 교체)와 달리, DRA는 매월 선물 커브 전체를 분석하여 **롤 비용이 가장 낮은 월물** 을 동적으로 선택한다.

핵심 아이디어는 두 가지다.

- **Contango 구간**: 원월물일수록 가격이 높아 롤오버 시 비용이 발생한다. DRA는 커브에서 상대적으로 저렴한(=IRY가 높은) 월물을 찾아 롤 비용을 줄인다.
- **Backwardation 구간**: 근월물 가격이 높으므로 현 보유 월물을 최대한 유지하는 것이 유리하다. DRA의 Parity Principle이 불필요한 교체를 억제한다.

---

## 2. 원본 방법론: S&P GSCI Dynamic Roll

### 2-1. Implied Roll Yield (IRY)

선물 커브상 연속된 두 계약 C(i, j-1)과 C(i, j) 사이의 **연환산 내재 롤 수익률** 을 다음과 같이 정의한다.

```
IRY_C(i, j) = [ C(i, j-1) - C(i, j) ] / [ C(i, j) × Interval D ]
```

| 기호 | 의미 |
|---|---|
| C(i, j) | 월 i 기준, j번째 선물 계약의 가격 |
| C(i, j-1) | 그 직전 계약(더 근월물)의 가격 |
| Interval D | C(i, j-1)과 C(i, j)의 만기월 차이 (개월 수) |

- IRY > 0: Backwardation → 롤인 시 이득
- IRY < 0: Contango → 롤인 시 비용 발생
- IRY가 클수록 해당 월물로 롤인하는 것이 유리하다.

### 2-2. Rank Order와 Optimum Set

모든 연속 페어에 대해 IRY를 계산한 뒤 **내림차순** 으로 정렬한다. 상품별로 지정된 **Rank Order k** 에 따라 상위 k개 계약이 **Optimum Set** 을 구성한다.

```
Optimum Set = { Best(1), Best(2), ..., Best(k) }
```

S&P GSCI 원본 기준 WTI 원유(CL)의 Rank Order는 **3** 이다. 즉 매월 IRY 상위 3개 계약이 Optimum Set이 된다.

### 2-3. Dynamic Roll Parity Principle

롤 여부는 다음 규칙으로 결정된다.

> **현재 보유 중인 계약(rolled-out contract)이 Optimum Set 안에 있으면 → 롤 없이 유지**
> **Optimum Set 밖에 있으면 → Best(1)로 롤오버 실행**

이 원칙이 불필요한 교체를 억제하는 핵심 버퍼 역할을 한다. Backwardation 구간에서는 근월물이 이미 최선이므로 자연스럽게 유지 결정이 내려진다.

---

## 3. WTI 단일 상품 적용 — 원본 대비 각색 사항

S&P GSCI 원본은 다수 상품을 포함하는 지수 전용 설계이므로, WTI 단일 상품 Bloomberg 지수에 맞게 아래 항목을 조정했다.

### 3-1. Rank Order: 3 유지

원본에서 WTI(CL)의 Rank Order는 3으로 지정되어 있다. 이를 그대로 적용하여 **DRA(3)** 알고리즘을 사용한다.

### 3-2. 롤결정일: BD6 (원본 BD3 → 조정)

| 구분 | S&P GSCI 원본 | 이 구현 |
|---|---|---|
| 롤결정일 (Roll Determination Date) | 매월 3번째 영업일 (BD3) | 매월 **6번째 영업일 (BD6)** |
| 롤기간 (Roll Period) | BD5 ~ BD9 | **BD6 ~ BD10** |

원본의 BD3 결정 + BD5 롤 시작 구조는 S&P GSCI 멀티 상품 스케줄을 전제로 한다. Bloomberg 단일 WTI 선물 지수는 롤기간을 BD6~BD10으로 정의하므로, 결정일도 롤기간 첫날인 BD6로 맞췄다. 롤결정일에 그날의 결제가격을 기준으로 IRY를 계산한다.

### 3-3. 유동성 필터 미적용

원본은 월간 달러 거래대금(MDVT) 및 월간 달러 미결제약정(MDOI) 기준으로 적격 계약을 연간 심사한다. 이 구현은 단일 상품(WTI)을 대상으로 하며 Databento에서 수집한 실제 결제가격이 존재하는 계약만 자동으로 후보군에 포함되므로 별도의 유동성 필터를 적용하지 않는다.

### 3-4. 후보 계약 범위: 만기 오름차순 최대 12개

결정일 기준으로 아직 만기가 도래하지 않은 계약 중 결제가격이 존재하는 것을 만기 오름차순으로 최대 12개까지 후보군으로 삼는다. 원본의 Dynamic Roll Matrix 개념에 해당한다.

---

## 4. 롤오버 파이프라인

롤오버 결정은 **월 단위**로 이루어진다. 아래는 매월 실행되는 전체 흐름이다.

```
[매월 BD6 — 롤결정일]
        │
        ▼
① Forward Curve 구성
   결정일 기준 만기 미도래 + 가격 존재 계약 수집
   만기 오름차순 정렬 → 최대 12개
        │
        ▼
② IRY 계산 (연속 페어)
   모든 인접 계약 쌍 (j-1, j)에 대해
   IRY = (P_{j-1} - P_j) / (P_j × 월수 차이)
        │
        ▼
③ IRY 내림차순 정렬 → 순위 결정
   Best(1) = 가장 높은 IRY를 가진 계약
   Best(2), Best(3) ...
        │
        ▼
④ DRA(3) Parity Principle 적용
   Optimum Set = { Best(1), Best(2), Best(3) }
   ┌─ 현 보유 월물 ∈ Optimum Set? ─┐
   │ YES → 롤 없이 유지             │ NO → Best(1)로 롤 실행
   └───────────────────────────────┘
        │
        ▼
⑤ 롤 비중 적용 (BD6~BD10, 하루 20%씩)
   roll_required = True  → 5일간 Lead↓ Next↑ (각 20%p)
   roll_required = False → lead_weight = 1.0 고정
        │
        ▼
⑥ MDE(Missing Data Event) 처리
   롤기간 중 가격 누락 시 해당 일 롤 유예
   다음 거래일에 누적 스텝 일괄 반영
```

### 각 단계별 구현 함수

| 단계 | 함수 |
|---|---|
| ① Forward Curve 구성 | `_get_contracts_for_date()` |
| ② IRY 계산 | `_calculate_iry()` |
| ③④ DRA(3) Parity Principle | `_run_dra()` |
| 월별 스케줄 생성 | `build_dra_contract_schedule()` |
| ⑤⑥ 롤 비중 + MDE | `calculate_dra_roll_weights()` |

---

## 5. 단계별 상세 설명

### ① Forward Curve 구성 — `_get_contracts_for_date()`

```python
# 결정일(det_date) 기준
# - 만기(expiry) > 결정일 인 계약만 포함
# - 결제가격이 존재하는 계약만 포함
# - 만기 오름차순 정렬, 최대 12개
contracts = [(symbol, price, expiry), ...]
```

### ② IRY 계산 — `_calculate_iry()`

만기 순서로 나열된 계약 리스트에서 인접한 모든 페어에 대해 IRY를 계산한다.

```python
interval_d = (exp_curr.month - exp_prev.month) + (exp_curr.year - exp_prev.year) * 12
iry = (price_prev - price_curr) / (price_curr * interval_d)
```

결과는 IRY 내림차순으로 정렬된 `[(symbol, iry), ...]` 리스트다. **주의**: IRY의 주체는 *롤인 대상 계약*(더 원월물인 C(i,j))이다. 즉 "이 계약으로 롤인했을 때의 수익률"을 의미한다.

### ③④ DRA(3) Parity Principle — `_run_dra()`

```python
optimum_set = [Best(1), Best(2), Best(3)]  # IRY 상위 3개 계약

if current_holding in optimum_set:
    # 유지: 현 보유 월물이 충분히 좋은 위치
    roll_required = False
    roll_in = current_holding
else:
    # 교체: Best(1)로 롤오버
    roll_required = True
    roll_in = Best(1)
```

### 월별 스케줄 생성 — `build_dra_contract_schedule()`

전체 분석 기간을 월 단위로 순회하며 각 월의 결정을 누적한다. 한 달의 결정이 다음 달의 `current_holding` 초기값에 영향을 준다. 즉 **보유 상태가 월간 연속성을 가진다.**

```
2016-01: CLG16 보유 → Optimum Set 확인 → 유지/교체 결정 → current_holding 갱신
2016-02: (갱신된 current_holding) → Optimum Set 확인 → ...
```

### ⑤ 롤 비중 계산 — `calculate_dra_roll_weights()`

`roll_required = True` 인 달에만 BD6~BD10에 걸쳐 롤 비중이 변화한다.

| 영업일 | lead_weight | next_weight |
|---|---|---|
| BD1~BD5 | 1.00 | 0.00 |
| BD6 | 0.80 | 0.20 |
| BD7 | 0.60 | 0.40 |
| BD8 | 0.40 | 0.60 |
| BD9 | 0.20 | 0.80 |
| BD10~ | 0.00 | 1.00 |

`roll_required = False` 인 달은 **BD1부터 말일까지 lead_weight = 1.0** 이 유지된다. 이 달은 롤 비중 변화가 전혀 없으므로 사실상 WAV/PWAV 계산에서 단일 계약 보유와 동일하게 작동한다.

### ⑥ MDE(Missing Data Event) 처리

롤기간(BD6~BD10) 중 Lead 또는 Next 계약의 결제가격이 누락된 날은 롤을 유예하고 `pending` 카운터를 증가시킨다. 가격이 회복된 첫날에 누적된 스텝을 한꺼번에 반영한다.

```python
# 예) BD7에 가격 누락 → BD8에 한꺼번에 2스텝 반영
rolls = 1 + pending   # 당일(1) + 유예된 날수
cur_next_w = min(1.0, cur_next_w + 0.20 * rolls)
```

롤기간(BD10)이 지난 이후에도 `pending > 0` 이 남아있으면 가격 회복 시 동일하게 일괄 반영한다.

---

## 6. 정적 롤오버와의 차이

| 항목 | 정적 롤오버 (Bloomberg ETN BM) | DRA(3) 동적 롤오버 |
|---|---|---|
| 롤인 계약 결정 | 항상 차근월물 (Lead+1) | IRY 상위 3위 이내 최적 월물 |
| 월물 유지 가능 여부 | 없음 (매월 교체) | 있음 (Parity Principle) |
| 시장 상황 반응 | 없음 | Contango→원월물 / Backwardation→근월물 유지 |
| 후보 계약 범위 | 1개 (차근월물 고정) | 최대 12개 중 상위 3개 |
| 롤 실행 빈도 | 매월 100% | 약 48% (백테스트 기준, 123개월 중 60개월) |

---

## 7. 참고 사항

- **IRY 계산의 주체**: `_calculate_iry()`는 연속 페어의 *더 원월물* 계약에 IRY를 귀속시킨다. 이는 "해당 계약으로 롤인할 경우의 수익률"을 의미하며, S&P GSCI 원본 공식과 동일한 방향이다.
- **만기 정보 보정**: WTI 선물은 인도월(delivery month) 전달에 만기가 발생한다. 예를 들어 CLF16(1월 인도)의 만기는 2015년 12월이다. 코드 내 `delivery_year` 보정 로직이 이 1개월 차이를 교정하여 올바른 내부 티커를 생성한다.
- **롤기간 조정**: Bloomberg 단일 WTI 지수의 롤기간(BD6~BD10)을 따른다. S&P GSCI 원본(BD5~BD9)과 1영업일 차이가 있으나 알고리즘 구조는 동일하다.
