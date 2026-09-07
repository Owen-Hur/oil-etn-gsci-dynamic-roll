"""
ws1_compare.py — WTI ETN 정적 vs DRA 성과 비교 분석

4개 지수: 정적_ER / 정적_TR / DRA_ER / DRA_TR
3가지 관점: 투자자 / 타임프레임·국면 / 연도별 롤일드
"""

from __future__ import annotations

import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── 경로 ──────────────────────────────────────────────────────────────────
THIS_DIR = Path(__file__).parent
DATA_DIR = THIS_DIR.parent / "data"
OUT_DIR  = THIS_DIR / "ws1_analysis"
OUT_DIR.mkdir(exist_ok=True)

# ── 한국어 폰트 ────────────────────────────────────────────────────────────
def _setup_font():
    for name in ["AppleGothic", "NanumGothic", "Malgun Gothic"]:
        if any(f.name == name for f in fm.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False

_setup_font()

# ── 상수 ──────────────────────────────────────────────────────────────────
LABELS = {"static_er": "정적 ER", "static_tr": "정적 TR",
          "dra_er":    "DRA ER",  "dra_tr":    "DRA TR"}
COLORS = {"static_er": "#1f77b4", "static_tr": "#aec7e8",
          "dra_er":    "#d62728", "dra_tr":    "#f7b6d2"}

# Fix 10: 슈퍼 백워데이션 종료일 2022-08-31로 수정
REGIMES = [
    ("강한 콘탱고",      "2016-01-04", "2016-03-31"),
    ("평범한/혼조세",    "2017-01-01", "2019-12-31"),
    ("슈퍼 콘탱고",      "2020-03-01", "2020-05-31"),
    ("슈퍼 백워데이션",  "2022-02-01", "2022-08-31"),
    ("강한 백워데이션",  "2025-07-01", "2026-03-18"),
]

# Fix 1: 달력 월 기준 근월물 티커 결정용 매핑
MONTH_TO_LEAD_CODE = {
    1: "G", 2: "H", 3: "J", 4: "K", 5: "M", 6: "N",
    7: "Q", 8: "U", 9: "V", 10: "X", 11: "Z", 12: "F",
}
FUTURES_CODE_TO_MONTH = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

# ============================================================
# PART 1: 데이터 로드
# ============================================================

def load_data():
    static_idx = pd.read_csv(THIS_DIR / "ws1_static_index.csv",
                             index_col="date", parse_dates=True)
    dra_idx    = pd.read_csv(THIS_DIR / "ws1_dra_index.csv",
                             index_col=0, parse_dates=True)
    dra_idx.index.name = "date"

    static_pos = pd.read_csv(THIS_DIR / "ws1_static_positions.csv",
                              index_col="date", parse_dates=True)
    dra_pos    = pd.read_csv(THIS_DIR / "ws1_dra_positions.csv",
                              index_col=0, parse_dates=True)
    dra_pos.index.name = "date"

    # Fix 7: 월별 체결금액 데이터 (chart2용)
    monthly_mc = pd.read_csv(THIS_DIR / "ws1_output" / "monthly_comparison.csv",
                              parse_dates=["롤결정일"])

    # 선물 결제 데이터 → 가격 피벗 (delivery_year 보정 적용)
    fut = pd.read_csv(DATA_DIR / "cl_settlements_2016-2026.csv",
                      dtype={"Symbol": str})
    fut.columns = [c.strip() for c in fut.columns]
    fut["date"]     = pd.to_datetime(fut["Trade date"])
    _exp_dt         = pd.to_datetime(fut["expiration"].str[:10])
    _exp_yr         = _exp_dt.dt.year
    _exp_mo         = _exp_dt.dt.month
    _month_code     = fut["Symbol"].str[2]
    _FCODE2MO       = {"F":1,"G":2,"H":3,"J":4,"K":5,"M":6,
                       "N":7,"Q":8,"U":9,"V":10,"X":11,"Z":12}
    _deliv_mo       = _month_code.map(_FCODE2MO)
    _deliv_yr       = _exp_yr.where(_deliv_mo > _exp_mo, _exp_yr + 1)
    fut["ticker"]   = "CL" + _month_code + (_deliv_yr % 100).apply(lambda y: f"{y:02d}")
    fut["price"]    = pd.to_numeric(fut["Settlement price"], errors="coerce")

    price_w = fut.pivot_table(index="date", columns="ticker",
                               values="price", aggfunc="last")

    return static_idx, dra_idx, static_pos, dra_pos, price_w, monthly_mc


# ============================================================
# PART 2: 근월물 연속 수익률 (Fix 1)
# ============================================================

def calc_fm_return(business_days, price_w, commodity_prefix="CL"):
    """
    달력 월 BD 1 기준 근월물 티커 전환.
    전환일은 새 티커로 t-1도 조회해 가격 단차 왜곡 제거.
    """
    def get_fm_ticker(date):
        m, y   = date.month, date.year
        code   = MONTH_TO_LEAD_CODE[m]
        cm     = FUTURES_CODE_TO_MONTH[code]
        cy     = y + 1 if cm <= m else y
        return f"{commodity_prefix}{code}{str(cy)[-2:]}"

    dates  = list(business_days)
    fm_ret = pd.Series(np.nan, index=business_days, name="fm_return")
    for i in range(1, len(dates)):
        d0, d1 = dates[i - 1], dates[i]
        tkr    = get_fm_ticker(d1)          # 오늘 달력 기준 티커 (t-1도 동일 티커)
        try:
            p0 = price_w.at[d0, tkr]
            p1 = price_w.at[d1, tkr]
            if pd.notna(p0) and pd.notna(p1) and p0 > 0:
                fm_ret.iloc[i] = p1 / p0 - 1
        except KeyError:
            pass
    return fm_ret


# ============================================================
# PART 3: 성과 지표
# ============================================================

def perf_metrics(series: pd.Series, label: str, rf_ann: float = 0.0) -> dict:
    ret     = series.pct_change().dropna()
    n       = len(ret)
    ann_ret = (series.iloc[-1] / series.iloc[0]) ** (252 / n) - 1
    ann_vol = ret.std() * np.sqrt(252)
    mdd     = ((series / series.cummax()) - 1).min()
    rf_d    = rf_ann / 252
    excess  = ret - rf_d
    sharpe  = excess.mean() * 252 / ann_vol if ann_vol > 0 else np.nan
    dn_vol  = ret[ret < rf_d].std() * np.sqrt(252)
    sortino = excess.mean() * 252 / dn_vol if (pd.notna(dn_vol) and dn_vol > 0) else np.nan
    calmar  = ann_ret / abs(mdd) if mdd != 0 else np.nan
    return dict(label=label, ann_ret=ann_ret, ann_vol=ann_vol, mdd=mdd,
                sharpe=sharpe, sortino=sortino, calmar=calmar,
                final=series.iloc[-1])


def calc_beta(etn_ret: pd.Series, fm_ret: pd.Series) -> float:
    df = pd.DataFrame({"e": etn_ret, "f": fm_ret}).dropna()
    if len(df) < 10:
        return np.nan
    c = np.cov(df["e"], df["f"])
    return c[0, 1] / c[1, 1] if c[1, 1] > 0 else np.nan


def calc_annual_beta(etn_ret: pd.Series, fm_ret: pd.Series) -> pd.Series:
    """연도별 베타."""
    df = pd.DataFrame({"e": etn_ret, "f": fm_ret}).dropna()
    annual = {}
    for yr, grp in df.groupby(df.index.year):
        if len(grp) < 10:
            continue
        c = np.cov(grp["e"], grp["f"])
        annual[yr] = c[0, 1] / c[1, 1] if c[1, 1] > 0 else np.nan
    return pd.Series(annual)


def calc_annual_roll_yield(ry_series: pd.Series) -> pd.Series:
    """연도별 롤일드 (%, 기하 연환산). 2026 부분 연도 제외."""
    annual = {}
    for yr, grp in ry_series.groupby(ry_series.index.year):
        if yr <= 2025:
            annual[yr] = ((1 + grp.dropna().mean()) ** 252 - 1) * 100
    return pd.Series(annual)


# ============================================================
# PART 4: 국면별 분석
# ============================================================

def regime_analysis(series_dict: dict) -> pd.DataFrame:
    rows = []
    for name, s0, s1 in REGIMES:
        row = {"국면": name, "시작": s0, "종료": s1}
        for k, ser in series_dict.items():
            sub = ser[s0:s1]
            row[k + "_ret"] = (sub.iloc[-1] / sub.iloc[0] - 1) if len(sub) >= 2 else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


# ============================================================
# PART 5: 롤링 1년 지표
# ============================================================

def rolling_metrics(series: pd.Series, roll_yield: pd.Series,
                    w: int = 252) -> pd.DataFrame:
    ret = series.pct_change()
    rr  = (1 + ret).rolling(w).apply(np.prod, raw=True) - 1
    rv  = ret.rolling(w).std() * np.sqrt(252)
    rs  = (rr / rv).where(rv > 0)
    # Fix 2/6: 기하 연환산
    rry = (1 + roll_yield.rolling(w).mean()) ** 252 - 1
    return pd.DataFrame({"ret": rr, "vol": rv, "sharpe": rs, "ry": rry})


# ============================================================
# PART 6: 차트
# ============================================================

def chart1_cumulative(sd: dict):
    fig, ax = plt.subplots(figsize=(14, 6))
    for k, s in sd.items():
        ax.plot(s.index, s, label=LABELS[k], color=COLORS[k], lw=1.5)
    ax.set_title("WTI ETN 누적 성과 비교 (2016–2026)", fontsize=13)
    ax.set_ylabel("지수 레벨 (기준=100)")
    ax.legend(); ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "chart1_cumulative.png", dpi=150)
    plt.close(fig)


def chart2_execution(monthly_mc: pd.DataFrame):
    """
    Fix 7: 월별 체결금액 — 정적 연속선 + DRA 롤 실행 월만 막대.
    """
    mc       = monthly_mc.sort_values("롤결정일")
    dates    = mc["롤결정일"].values
    s_amt    = mc["정적_체결금액(달러)"].values / 1e6
    d_amt    = mc["동적_체결금액(달러)"].values / 1e6
    d_roll   = mc["롤실행여부"].astype(bool).values

    fig, ax  = plt.subplots(figsize=(16, 6))
    ax.plot(dates, s_amt, color=COLORS["static_er"], lw=1.5, label="정적 ER (전체)")
    ax.bar(dates[d_roll], d_amt[d_roll], width=20,
           color=COLORS["dra_er"], alpha=0.75, label="DRA ER (롤 실행 월)")
    ax.axhline(0, color="black", lw=0.8, ls="--")
    ax.set_title("월별 체결 금액 비교 (USD 백만)", fontsize=13)
    ax.set_ylabel("USD 백만")
    ax.text(0.5, 0.97, "(DRA: 롤 실행 월만 표시)",
            transform=ax.transAxes, ha="center", va="top", fontsize=9, color="gray")
    ax.legend(); ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(OUT_DIR / "chart2_execution_amount.png", dpi=150)
    plt.close(fig)


def chart3_roll_yield(ry_static: pd.Series, ry_dra: pd.Series):
    """Fix 2: 기하 연환산 / Fix 4: Y축 ±150% 클리핑 + 주석."""
    fig, ax = plt.subplots(figsize=(14, 5))

    all_vals = []
    for ry, k, lbl in [(ry_static, "static_er", "정적 ER 롤일드"),
                        (ry_dra,    "dra_er",    "DRA ER 롤일드")]:
        sm = ((1 + ry.rolling(60).mean()) ** 252 - 1) * 100
        ax.plot(sm.index, sm, label=f"{lbl} (60일MA, 연환산%)",
                color=COLORS[k], lw=1.5)
        all_vals.extend(sm.dropna().tolist())

    ax.axhline(0, color="black", lw=0.8, ls="--")
    ax.set_ylim(-150, 150)
    ax.set_title("롤 일드 시계열 (연환산 %, 60일 이동평균)", fontsize=13)
    ax.set_ylabel("롤일드 (%, 연환산, 60일 MA)")

    if all_vals:
        vmax, vmin = max(all_vals), min(all_vals)
        if vmax > 150 or vmin < -150:
            ax.text(0.99, 0.97,
                    f"※ 최대 {vmax:.0f}% / 최소 {vmin:.0f}% (클리핑됨)",
                    transform=ax.transAxes, ha="right", va="top",
                    fontsize=8, color="gray")

    ax.legend(); ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "chart3_roll_yield.png", dpi=150)
    plt.close(fig)


def chart4_rolling(rolling_dict_tr: dict, rolling_dict_er: dict):
    """
    Fix 6: 4패널 — ret/vol/sharpe는 TR, ry는 ER 기준.
    """
    specs = [
        ("ret",    "롤링 1년 수익률 (%)",  100),
        ("vol",    "롤링 1년 변동성 (%)",  100),
        ("sharpe", "롤링 1년 샤프 지수",     1),
        ("ry",     "롤링 1년 롤일드 (%)",  100),
    ]
    fig, axes = plt.subplots(4, 1, figsize=(14, 14), sharex=True)
    for ax, (col, title, scale) in zip(axes, specs):
        src = rolling_dict_er if col == "ry" else rolling_dict_tr
        for k, rm in src.items():
            ax.plot(rm.index, rm[col] * scale, label=LABELS[k],
                    color=COLORS[k], lw=1.1, alpha=0.85)
        ax.axhline(0, color="black", lw=0.8, ls="--")
        ax.set_title(title, fontsize=11)
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)
    fig.suptitle("롤링 1년 성과 지표 추이 (252영업일)", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "chart4_rolling_metrics.png", dpi=150)
    plt.close(fig)


def chart5_regime(regime_df: pd.DataFrame):
    regimes = regime_df["국면"].tolist()
    keys    = list(LABELS.keys())
    x = np.arange(len(regimes)); w = 0.2
    fig, ax = plt.subplots(figsize=(15, 6))
    for i, k in enumerate(keys):
        col  = k + "_ret"
        if col not in regime_df.columns:
            continue
        vals = [v * 100 if pd.notna(v) else 0 for v in regime_df[col]]
        ax.bar(x + i * w, vals, w, label=LABELS[k], color=COLORS[k], alpha=0.85)
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xticks(x + w * 1.5)
    ax.set_xticklabels(regimes, rotation=15, ha="right")
    ax.set_title("시장 국면별 누적 수익률 비교 (%)", fontsize=13)
    ax.set_ylabel("누적 수익률 (%)")
    ax.legend(); ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(OUT_DIR / "chart5_regime.png", dpi=150)
    plt.close(fig)


def chart8_beta_scatter(sd: dict, fm_ret: pd.Series):
    """Fix 9: ER 기준 베타 산점도."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    for ax, k, title in zip(
        axes,
        ["static_er", "dra_er"],
        ["정적 ETN ER vs 최근월물", "DRA ETN ER vs 최근월물"],
    ):
        etn_ret = sd[k].pct_change().dropna()
        df      = pd.DataFrame({"fm": fm_ret, "etn": etn_ret}).dropna()
        ax.scatter(df["fm"], df["etn"], alpha=0.3, s=5)
        m, b    = np.polyfit(df["fm"], df["etn"], 1)
        x_line  = np.linspace(df["fm"].min(), df["fm"].max(), 100)
        ax.plot(x_line, m * x_line + b, color="red", lw=1.5,
                label=f"β = {m:.4f}")
        ax.set_title(title)
        ax.set_xlabel("최근월물 일간 수익률")
        ax.set_ylabel("ETN ER 일간 수익률")
        ax.legend(); ax.grid(True, alpha=0.3)
    fig.suptitle("최근월물 베타 산점도 (ER 기준)", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "chart8_beta_scatter.png", dpi=150)
    plt.close(fig)


# ============================================================
# PART 7: 요약 CSV
# ============================================================

def save_summary(perf_list: list, regime_df: pd.DataFrame,
                 ry_ann: dict, beta_dict: dict,
                 ry_annual_static: pd.Series, ry_annual_dra: pd.Series,
                 beta_annual_static: pd.Series, beta_annual_dra: pd.Series):
    lines = []

    # 투자자 지표
    lines.append("=== [투자자 관점] 성과 지표 ===")
    hdr = ["지수", "최종값", "누적수익(%)", "연환산수익(%)", "연환산변동성(%)",
           "최대낙폭(%)", "샤프", "소르티노", "칼마", "연환산롤일드(%)", "근월물베타"]
    lines.append(",".join(hdr))
    for p in perf_list:
        k  = p["label"]
        ry = ry_ann.get(k, np.nan)
        bt = beta_dict.get(k, np.nan)
        lines.append(",".join([
            LABELS.get(k, k),
            f"{p['final']:.4f}",
            f"{(p['final'] / 100 - 1) * 100:.2f}",
            f"{p['ann_ret'] * 100:.2f}",
            f"{p['ann_vol'] * 100:.2f}",
            f"{p['mdd'] * 100:.2f}",
            f"{p['sharpe']:.3f}",
            f"{p['sortino']:.3f}",
            f"{p['calmar']:.3f}",
            f"{ry * 100:.3f}" if pd.notna(ry) else "N/A",
            f"{bt:.4f}"       if pd.notna(bt) else "N/A",
        ]))

    lines.append("")
    lines.append("=== [시장 국면별] 누적 수익률 ===")
    cols = ["국면", "시작", "종료"] + [k + "_ret" for k in LABELS]
    lines.append(",".join(cols))
    for _, row in regime_df.iterrows():
        vals = [str(row.get("국면", "")), str(row.get("시작", "")), str(row.get("종료", ""))]
        for k in LABELS:
            v = row.get(k + "_ret", np.nan)
            vals.append(f"{v * 100:.2f}" if pd.notna(v) else "N/A")
        lines.append(",".join(vals))

    # Fix 8: 연도별 롤일드 섹션
    lines.append("")
    lines.append("=== [연도별 롤일드 (%, 연환산)] ===")
    lines.append("year,static_ry_pct,dra_ry_pct")
    all_years = sorted(set(ry_annual_static.index) | set(ry_annual_dra.index))
    for yr in all_years:
        s = ry_annual_static.get(yr, np.nan)
        d = ry_annual_dra.get(yr, np.nan)
        lines.append(
            f"{yr},"
            f"{'N/A' if pd.isna(s) else f'{s:.3f}'},"
            f"{'N/A' if pd.isna(d) else f'{d:.3f}'}"
        )

    # 연도별 베타 섹션
    lines.append("")
    lines.append("=== [연도별 근월물 베타] ===")
    lines.append("year,static_er_beta,dra_er_beta")
    all_beta_years = sorted(set(beta_annual_static.index) | set(beta_annual_dra.index))
    for yr in all_beta_years:
        s = beta_annual_static.get(yr, np.nan)
        d = beta_annual_dra.get(yr, np.nan)
        lines.append(
            f"{yr},"
            f"{'N/A' if pd.isna(s) else f'{s:.4f}'},"
            f"{'N/A' if pd.isna(d) else f'{d:.4f}'}"
        )

    with open(OUT_DIR / "summary_stats.csv", "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("WTI ETN 정적 vs DRA 종합 성과 비교")
    print("=" * 60)

    # ── 1. 데이터 로드 ──
    print("\n[1] 데이터 로드 중...")
    static_idx, dra_idx, static_pos, dra_pos, price_w, monthly_mc = load_data()
    print(f"  정적 기간: {static_idx.index[0].date()} ~ {static_idx.index[-1].date()} "
          f"({len(static_idx)}일)")
    print(f"  DRA  기간: {dra_idx.index[0].date()} ~ {dra_idx.index[-1].date()} "
          f"({len(dra_idx)}일)")

    sd = {"static_er": static_idx["er_index"],
          "static_tr": static_idx["tr_index"],
          "dra_er":    dra_idx["er_index"],
          "dra_tr":    dra_idx["tr_index"]}

    # ── 2. 근월물 수익률 & 롤일드 ──
    print("[2] 근월물 연속 수익률 & 롤 일드 계산 중...")
    # Fix 1: 달력 월 BD 1 기준 티커 전환
    fm_ret = calc_fm_return(static_idx.index, price_w)

    ry_static = (static_idx["der"].fillna(0)
                 - fm_ret.reindex(static_idx.index).fillna(0))
    ry_dra    = (dra_idx["der"].fillna(0)
                 - fm_ret.reindex(dra_idx.index).fillna(0))

    # Fix 2: 기하 연환산 (arithmetic mean * 252 → (1+mean)^252 - 1)
    ry_ann = {
        "static_er": (1 + ry_static.dropna().mean()) ** 252 - 1,
        "static_tr": (1 + ry_static.dropna().mean()) ** 252 - 1,
        "dra_er":    (1 + ry_dra.dropna().mean())    ** 252 - 1,
        "dra_tr":    (1 + ry_dra.dropna().mean())    ** 252 - 1,
    }
    print(f"  정적 연환산 롤일드: {ry_ann['static_er'] * 100:+.3f}%")
    print(f"  DRA   연환산 롤일드: {ry_ann['dra_er'] * 100:+.3f}%")

    # ── 3. 성과 지표 ──
    print("[3] 성과 지표 계산 중...")
    perf_list = []
    for k, s in sd.items():
        p = perf_metrics(s, k)
        perf_list.append(p)
        print(f"  {LABELS[k]:8s} | 연환산: {p['ann_ret']*100:+.2f}% | "
              f"변동성: {p['ann_vol']*100:.2f}% | MDD: {p['mdd']*100:.2f}% | "
              f"샤프: {p['sharpe']:.3f} | 소르티노: {p['sortino']:.3f} | "
              f"칼마: {p['calmar']:.3f}")

    # Fix 3: ER만 베타 계산
    beta_dict = {}
    for k in ["static_er", "dra_er"]:
        beta_dict[k] = calc_beta(
            sd[k].pct_change().dropna(),
            fm_ret.reindex(sd[k].index)
        )
    print(f"\n  정적 ER 근월물 베타: {beta_dict['static_er']:.4f}")
    print(f"  DRA  ER 근월물 베타: {beta_dict['dra_er']:.4f}")

    # 연도별 베타
    beta_annual_static = calc_annual_beta(
        sd["static_er"].pct_change().dropna(),
        fm_ret.reindex(sd["static_er"].index)
    )
    beta_annual_dra = calc_annual_beta(
        sd["dra_er"].pct_change().dropna(),
        fm_ret.reindex(sd["dra_er"].index)
    )
    print(f"\n  {'연도':>6s} | {'정적 ER β':>10s} | {'DRA ER β':>10s}")
    print("  " + "─" * 32)
    all_beta_years = sorted(set(beta_annual_static.index) | set(beta_annual_dra.index))
    for yr in all_beta_years:
        s = beta_annual_static.get(yr, np.nan)
        d = beta_annual_dra.get(yr, np.nan)
        print(f"  {yr:>6d} | "
              f"{'N/A' if pd.isna(s) else f'{s:+.4f}':>10s} | "
              f"{'N/A' if pd.isna(d) else f'{d:+.4f}':>10s}")
    print(f"  {'전체':>6s} | {beta_dict['static_er']:>+10.4f} | {beta_dict['dra_er']:>+10.4f}")

    # Fix 8: 연도별 롤일드
    ry_annual_static = calc_annual_roll_yield(ry_static)
    ry_annual_dra    = calc_annual_roll_yield(ry_dra)

    # ── 4. 국면별 분석 ──
    print("[4] 시장 국면별 분석 중...")
    regime_df = regime_analysis(sd)
    print(f"\n  {'국면':20s} | {'정적ER':>8s} | {'DRA ER':>8s} | {'차이':>8s}")
    print("  " + "─" * 52)
    for _, row in regime_df.iterrows():
        s    = row.get("static_er_ret", np.nan)
        d    = row.get("dra_er_ret",    np.nan)
        diff = (d - s) if pd.notna(d) and pd.notna(s) else np.nan
        print(f"  {row['국면']:20s} | "
              f"{'N/A' if pd.isna(s) else f'{s*100:+.2f}%':>8s} | "
              f"{'N/A' if pd.isna(d) else f'{d*100:+.2f}%':>8s} | "
              f"{'N/A' if pd.isna(diff) else f'{diff*100:+.2f}%':>8s}")

    # ── 5. 롤링 지표 ──
    print("\n[5] 롤링 1년 지표 계산 중...")
    ry_map = {"static_er": ry_static, "static_tr": ry_static,
              "dra_er":    ry_dra,    "dra_tr":    ry_dra}
    # Fix 6: TR → 패널 1-3, ER → 패널 4(롤일드)
    rolling_dict_tr = {k: rolling_metrics(sd[k], ry_map[k].reindex(sd[k].index).fillna(0))
                       for k in ["static_tr", "dra_tr"]}
    rolling_dict_er = {k: rolling_metrics(sd[k], ry_map[k].reindex(sd[k].index).fillna(0))
                       for k in ["static_er", "dra_er"]}

    # ── 6. 차트 생성 ──
    print("[6] 차트 생성 중...")
    chart1_cumulative(sd)
    chart2_execution(monthly_mc)
    chart3_roll_yield(ry_static, ry_dra)
    chart4_rolling(rolling_dict_tr, rolling_dict_er)
    chart5_regime(regime_df)
    chart8_beta_scatter(sd, fm_ret)
    print(f"  PNG 6개 저장 완료 → {OUT_DIR}")

    # ── 7. 요약 CSV ──
    print("[7] summary_stats.csv 저장 중...")
    save_summary(perf_list, regime_df, ry_ann, beta_dict,
                 ry_annual_static, ry_annual_dra,
                 beta_annual_static, beta_annual_dra)
    print(f"  저장 완료 → {OUT_DIR / 'summary_stats.csv'}")

    print("\n" + "=" * 60)
    print("분석 완료!")
    print("=" * 60)


if __name__ == "__main__":
    main()
