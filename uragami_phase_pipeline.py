#!/usr/bin/env python3
"""
우라가미 6국면 라이브 분류 파이프라인
ECOS(한국) + FRED(일본/글로벌) + EDINET(일본 펀더멘털 확증) → dRR/dE 계산 →
6상태 순서형 분류기(링 제약 + 히스테리시스) → 대시보드 HTML의 READS 객체 재생성.

설계 원칙
  - 분류기 입력은 가격 외생 매크로만 (주가는 검증·익스포저 맵으로만 사용, 순환참조 방지).
  - 월간 dE: 한국=수출금액지수, 일본=광공업생산(또는 CLI). EDINET은 분기·연차 영업이익
    YoY 확산을 만드는 느린 확증 신호로만 사용(시차 큼).
  - dRR/dE 는 3M 평활 후 3개월 변화량을 trailing window z-score 로 표준화.
  - 임계치는 피팅이 아니라 경제적 근거로 고정(STRONG=1.0, FLAT=0.3).

필요 환경변수(미설정 시 코드 내 폴백 키 사용)
  ECOS_API_KEY(한국), FRED_API_KEY(미/일 금리), ESTAT_APP_ID(일본 CPI·생산), EDINET_API_KEY(선택)
의존성: requests, pandas, numpy
사용
  python uragami_phase_pipeline.py \
      --template uragami_live_phase.html --out uragami_live_phase.generated.html [--edinet]
"""

from __future__ import annotations
import os, re, json, time, argparse, datetime as dt
from dataclasses import dataclass, asdict, field
from typing import Optional
import numpy as np
import pandas as pd
import requests

# ──────────────────────────────────────────────────────────────────────────
# 0. 설정 — 통계/시리즈 코드는 반드시 본인 환경에서 1회 검증할 것.
#    (ECOS 통계코드검색 / FRED series id). discover 헬퍼로 코드 확인 가능.
# ──────────────────────────────────────────────────────────────────────────
ECOS_KEY   = os.environ.get("ECOS_API_KEY",   "")
FRED_KEY   = os.environ.get("FRED_API_KEY",   "")
EDINET_KEY = os.environ.get("EDINET_API_KEY", "")
ESTAT_KEY  = os.environ.get("ESTAT_APP_ID",   "")

START = "200001"          # 학습 window 시작(월)
Z_WIN = 60                # z-score trailing window(개월)
STRONG, FLAT = 1.0, 0.3   # 강/완만/횡보 임계치 (z 기준)
PX_MOM    = 6             # 주가 모멘텀 측정 구간(개월) — 검증축
PX_THRESH = 5.0           # 주가 추세 판정 임계(%)
PX_EXPECT = {0:+1, 1:0, 2:+1, 3:-1, 4:0, 5:-1}  # 국면별 기대 주가방향(+상승/−하락/0횡보)
HIST_START = "2024-01"    # 월별 국면 이력 시작(월말)

# ECOS (월간) — 코드 검증 필요 항목은 # CHECK 표시
ECOS = {
    "kr_10y":   {"stat": "817Y002", "item": "010210000", "cycle": "D"},  # 국고채(10년), 817Y002는 일별만 제공
    "kr_cpi":   {"stat": "901Y009", "item": "0"},           # 소비자물가 총지수
    "kr_expamt":{"stat": "403Y001", "item": "*AA"},         # 수출금액지수  # CHECK stat/item
}
# FRED (월간/일간)
FRED = {
    "jp_10y": "IRLTLT01JPM156N",   # Japan 10Y (OECD/FRED) — 현재 갱신 확인됨
    "jp_exp": "JPNXTEXVA01CXMLM",  # Japan 수출액(엔, 현재가격) — 실적 프록시(한국 수출금액지수와 동일 개념)
    "us_real":"DFII10",            # US 10Y TIPS (글로벌 오버레이, 일간→월말)
    "kr_10y_bk":"IRLTLT01KRM156N", # 한국 10Y 백업
}
# e-Stat (일본 공식 통계, 월간 지수) — FRED OECD-MEI 일본 피드 중단으로 직접 연동.
#   각 항목: statsDataId + 카테고리 필터(cdTab/cdCat/cdArea). 검증 완료(2026-06 갱신).
ESTAT = {
    # 消費者物価指数(2020기준) 全国·総合, 指数(tab=1). 1970-01~ 최신
    "jp_cpi": {"id": "0003427113",
               "filt": {"cdTab": "1", "cdCat01": "0001", "cdArea": "00000"}},
    # 鉱工業生産指数(2020기준) 業種別 原指数 月次 生産, 鉱工業 총합(cat01=0001000). 2018-01~ 최신
    "jp_ip":  {"id": "0004052181",
               "filt": {"cdCat01": "0001000"}},
}

# 원본 데이터 출처 페이지 — 카드 메트릭 클릭 시 사람이 보는 포털로 연결(데이터 진의 확인).
#   FRED·e-Stat: 통계코드 딥링크 가능. ECOS: SPA라 표 딥링크 불가 → 포털+코드 툴팁.
FRED_PAGE  = "https://fred.stlouisfed.org/series/{}"
ESTAT_PAGE = "https://www.e-stat.go.jp/dbview?sid={}"
ECOS_HOME  = "https://ecos.bok.or.kr/"          # SPA — ecos.bok.or.kr에서 코드/키워드 검색
def src(url, tip): return {"url": url, "tip": tip}

PHASES = ["금융장세","중간반락","실적장세","역금융장세","중간반등","역실적장세"]
# 곡선 세그먼트 중점 → 핀 좌표(대시보드 SVG 좌표계와 동일)
PIN = {0:[90,145],1:[185,95],2:[280,70],3:[385,95],4:[490,145],5:[590,170]}

# ──────────────────────────────────────────────────────────────────────────
# 1. 데이터 페처
# ──────────────────────────────────────────────────────────────────────────
def ecos_series(stat: str, item: str, cycle="M",
                start=START, end="209912") -> pd.Series:
    """ECOS StatisticSearch → 월간 시계열(Series, index=PeriodM).
    cycle='D'(일별)면 일별로 받아 월말 last 값으로 리샘플."""
    if not ECOS_KEY:
        raise RuntimeError("ECOS_API_KEY 미설정")
    # start/end 포맷을 주기에 맞춤 (월간 YYYYMM, 일별 YYYYMMDD)
    if cycle == "D":
        s_param = start + "01" if len(start) == 6 else start
        e_param = end + "31"   if len(end)   == 6 else end
    else:
        s_param, e_param = start, end
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{ECOS_KEY}/json/kr/"
           f"1/100000/{stat}/{cycle}/{s_param}/{e_param}/{item}")
    r = requests.get(url, timeout=30); r.raise_for_status()
    rows = r.json().get("StatisticSearch", {}).get("row", [])
    if not rows:
        raise RuntimeError(f"ECOS 빈 응답 stat={stat} item={item}")
    s = pd.Series({row["TIME"]: float(row["DATA_VALUE"]) for row in rows
                   if row.get("DATA_VALUE") not in (None, "")})
    if cycle == "D":
        s.index = pd.to_datetime(s.index, format="%Y%m%d")
        s = s.resample("ME").last().to_period("M")
    else:
        s.index = pd.PeriodIndex(s.index, freq="M")
    return s.sort_index()

def fred_series(series_id: str, start=f"{START[:4]}-{START[4:]}-01") -> pd.Series:
    """FRED observations → 월말 리샘플 Series(index=PeriodM)."""
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY 미설정")
    url = ("https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={series_id}&api_key={FRED_KEY}&file_type=json"
           f"&observation_start={start}")
    r = requests.get(url, timeout=30); r.raise_for_status()
    obs = r.json().get("observations", [])
    rec = {o["date"]: float(o["value"]) for o in obs if o["value"] not in (".", "")}
    s = pd.Series(rec); s.index = pd.to_datetime(s.index)
    return s.resample("ME").last().to_period("M").sort_index()

ESTAT_BASE = "https://api.e-stat.go.jp/rest/3.0/app/json"

def _estat_time_map(stats_data_id: str) -> dict:
    """e-Stat 시간축 코드 → 표시명 매핑(내부코드를 날짜로 풀기 위함)."""
    r = requests.get(f"{ESTAT_BASE}/getMetaInfo",
                     params={"appId": ESTAT_KEY, "statsDataId": stats_data_id}, timeout=60)
    r.raise_for_status()
    co = r.json()["GET_META_INFO"]["METADATA_INF"]["CLASS_INF"]["CLASS_OBJ"]
    tmap = {}
    for c in co:
        if c.get("@id") != "time":
            continue
        cls = c["CLASS"]
        if isinstance(cls, dict):
            cls = [cls]
        for x in cls:
            tmap[x["@code"]] = x["@name"]
    return tmap

def _estat_period(name: str) -> Optional[pd.Period]:
    """e-Stat 시간 표시명 → PeriodM. 'YYYY年M月'·'YYYYMM' 두 형식 지원.
    연차·가중치 등 월이 아닌 행은 None(→ 호출부에서 스킵)."""
    m = re.match(r"(\d{4})年(\d{1,2})月", name)
    if not m:
        m = re.match(r"^(\d{4})(\d{2})$", name)
    if not m:
        return None
    return pd.Period(f"{m.group(1)}-{int(m.group(2)):02d}", freq="M")

def estat_series(stats_data_id: str, filt: dict) -> pd.Series:
    """e-Stat getStatsData → 월간 지수 Series(index=PeriodM).
    filt: cdTab/cdCat01/cdArea 등 카테고리 필터(검증된 코드)."""
    if not ESTAT_KEY:
        raise RuntimeError("ESTAT_APP_ID 미설정")
    tmap = _estat_time_map(stats_data_id)
    params = {"appId": ESTAT_KEY, "statsDataId": stats_data_id, "limit": "100000"}
    params.update(filt)
    r = requests.get(f"{ESTAT_BASE}/getStatsData", params=params, timeout=60)
    r.raise_for_status()
    g = r.json()["GET_STATS_DATA"]
    if str(g["RESULT"]["STATUS"]) != "0":
        raise RuntimeError(f"e-Stat 오류: {g['RESULT'].get('ERROR_MSG')}")
    vals = g["STATISTICAL_DATA"]["DATA_INF"]["VALUE"]
    if isinstance(vals, dict):
        vals = [vals]
    rec = {}
    for v in vals:
        per = _estat_period(tmap.get(v.get("@time", ""), ""))
        if per is None:
            continue
        try:
            rec[per] = float(v.get("$"))
        except (TypeError, ValueError):
            continue
    if not rec:
        raise RuntimeError(f"e-Stat 빈 응답 id={stats_data_id} filt={filt}")
    s = pd.Series(rec)
    s.index = pd.PeriodIndex(s.index, freq="M")
    return s.sort_index()

def yoy(level: pd.Series) -> pd.Series:
    """지수 → 전년동월비(%)."""
    return level.pct_change(12) * 100.0

# ──────────────────────────────────────────────────────────────────────────
# 2. EDINET — 일본 영업이익 YoY 확산(느린 확증). docTypeCode 120=유가증권보고서.
#    실시간 아님(법정공시, 시차 큼). 호출 간 3~5초 슬립 필수.
# ──────────────────────────────────────────────────────────────────────────
EDINET_DOCS = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
EDINET_DOC  = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc}"
OPINC_TAGS  = (  # 표준별 영업이익 element id 후보
    "jpcrp_cor:OperatingIncome", "OperatingIncome",
    "ifrs-full:OperatingProfitLoss", "OperatingProfitLoss",
)

def edinet_doclist(day: dt.date) -> list[dict]:
    p = {"date": day.isoformat(), "type": 2, "Subscription-Key": EDINET_KEY}
    r = requests.get(EDINET_DOCS, params=p, timeout=30); r.raise_for_status()
    return r.json().get("results", []) or []

def _opinc_yoy_from_csv_zip(content: bytes) -> Optional[float]:
    import io, zipfile, csv
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except Exception:
        return None
    cur = prior = None
    for name in zf.namelist():
        if not name.lower().endswith(".csv"):
            continue
        txt = zf.read(name).decode("utf-16", errors="ignore")
        for row in csv.reader(txt.splitlines(), delimiter="\t"):
            if len(row) < 3:
                continue
            elem, ctx, val = row[0], row[1], row[2]
            if elem not in OPINC_TAGS:
                continue
            try: v = float(val)
            except ValueError: continue
            if "CurrentYear" in ctx and "Duration" in ctx:  cur = v
            elif "Prior1Year" in ctx and "Duration" in ctx: prior = v
    if cur is None or prior in (None, 0):
        return None
    return (cur - prior) / abs(prior) * 100.0

def edinet_opinc_breadth(days=45, cap=60, sleep=3.5) -> Optional[float]:
    """최근 days일 유가증권보고서 표본의 영업이익 YoY 양(+) 비율(%)."""
    if not EDINET_KEY:
        return None
    today = dt.date.today(); seen = 0; pos = 0
    for d in range(days):
        day = today - dt.timedelta(days=d)
        try:
            docs = edinet_doclist(day)
        except Exception:
            time.sleep(sleep); continue
        for doc in docs:
            if doc.get("docTypeCode") != "120" or doc.get("secCode") in (None, ""):
                continue
            if seen >= cap:
                break
            try:
                time.sleep(sleep)
                rr = requests.get(EDINET_DOC.format(doc=doc["docID"]),
                                  params={"type": 5, "Subscription-Key": EDINET_KEY},
                                  timeout=60)
                rr.raise_for_status()
                g = _opinc_yoy_from_csv_zip(rr.content)
            except Exception:
                g = None
            if g is not None:
                seen += 1; pos += 1 if g > 0 else 0
        if seen >= cap:
            break
        time.sleep(sleep)
    return (pos / seen * 100.0) if seen else None

# ──────────────────────────────────────────────────────────────────────────
# 3. 피처 엔지니어링 + 6상태 분류기(링 제약 + 히스테리시스)
# ──────────────────────────────────────────────────────────────────────────
def zscore(s: pd.Series, win=Z_WIN) -> pd.Series:
    mu = s.rolling(win, min_periods=max(12, win // 3)).mean()
    sd = s.rolling(win, min_periods=max(12, win // 3)).std()
    return (s - mu) / sd

def features(real_rate: pd.Series, earn_proxy_level: pd.Series) -> pd.DataFrame:
    rr = real_rate.rolling(3, min_periods=1).mean()
    rr_chg = rr.diff(3)
    e_yoy  = yoy(earn_proxy_level).rolling(3, min_periods=1).mean()
    e_chg  = e_yoy.diff(3)                       # 실적 모멘텀(YoY 가속/둔화)
    df = pd.DataFrame({
        "real_rate": real_rate, "rr": rr, "e_yoy": e_yoy,
        "dRR": zscore(rr_chg), "dE": zscore(e_chg),
    }).dropna(subset=["dRR", "dE"])
    return df

def _raw_phase(dRR: float, dE: float, prev: int) -> int:
    if abs(dRR) < FLAT and abs(dE) < FLAT:          # 데드존 → 전이국면
        if prev in (0, 1): return 1                 # 직전 금융계열 → 중간반락
        if prev in (3, 4): return 4                 # 직전 역금융계열 → 중간반등
        return prev
    rate_led = abs(dRR) > abs(dE)
    if rate_led and dRR < 0: return 0               # 금융장세
    if rate_led and dRR > 0: return 3               # 역금융장세
    if not rate_led and dE > 0: return 2            # 실적장세
    if not rate_led and dE < 0: return 5            # 역실적장세
    return prev

def classify(df: pd.DataFrame, gate_mom: Optional[pd.Series] = None,
             thr: float = PX_THRESH) -> pd.Series:
    """링 제약: 사이클은 한 방향(0→1→…→5→0)으로만 진행.
      - 전방 1~2칸: 신호로 즉시 이동(snap).
      - 전방 3칸(반대편): 한 번에 비약 금지 → 매월 1칸씩 전진(walk)해 따라붙음.
      - 후방(전방 4~5칸): 역행=노이즈로 보고 유지(히스테리시스).
    구버전은 전방 3칸을 '유지'해, 신호가 한 국면을 건너뛰면(예: 금융→역금융)
    분류가 영구히 정지하는 버그가 있었음.

    주가 검증 게이트(gate_mom 주어질 때): 하락 국면(역금융=3, 역실적=5)으로의 진입을
    주가 강세(모멘텀 > thr)가 거부함. "주가가 강하게 오르는데 가격 하락 국면일 수 없다"
    — 순환참조가 아니라 검증축의 거부권. 거부 시 3→실적(2), 5→금융(0)으로 대체.
    (예: 일본 리플레이션 — 디플레 탈출형 금리 정상화를 역금융으로 오분류하는 것을 차단.)"""
    out, prev = [], 0
    for idx, dRR, dE in zip(df.index, df["dRR"], df["dE"]):
        cand = _raw_phase(float(dRR), float(dE), prev)
        d = (cand - prev) % 6
        if d in (1, 2):
            nv = cand
        elif d == 3:
            nv = (prev + 1) % 6        # 멀리 앞선 신호 → 한 칸씩 전진
        else:
            nv = prev                  # d==0 또는 d in (4,5): 유지
        if gate_mom is not None and nv in (3, 5):
            m = gate_mom.get(idx)
            if m is not None and not pd.isna(m) and m > thr:
                nv = 2 if nv == 3 else 0   # 주가 강세 → 하락 국면 거부
        prev = nv
        out.append(prev)
    return pd.Series(out, index=df.index, name="phase")

# ──────────────────────────────────────────────────────────────────────────
# 4. 시장별 현재 국면 빌드
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Read:
    cc: str
    mkt: str
    phase: int
    watch: Optional[str]
    metrics: list
    why: str
    pin: list = field(default_factory=list)
    pxchk: Optional[dict] = None      # 주가 검증(분류와 괴리 점검). 분류 입력 아님.
    gated: Optional[str] = None       # 주가 게이트로 하락국면이 거부됐을 때 원래(매크로) 국면명
    hist: list = field(default_factory=list)  # 월별 국면 이력 [{m,phase,gated,dRR,dE}]

def _watch(dRR: float, dE: float, phase: int) -> Optional[str]:
    """주도 마진이 얇으면 다음 링 국면 근접 경보."""
    margin = abs(abs(dRR) - abs(dE))
    if margin >= 0.35:
        return None
    nxt = PHASES[(phase + 1) % 6]
    return f"{nxt} 근접"

# 검증축: 시장 주가지수. 분류 입력에는 절대 넣지 않음(순환참조 방지). 분류 결과와
#   주가 추세가 어긋날 때 경고만 띄움 — "역금융인데 지수 급등" 같은 오분류를 잡아냄.
PRICE = {
    "한국": {"src": "ecos", "stat": "802Y001", "item": "0001000", "cycle": "D", "name": "KOSPI"},
    "일본": {"src": "fred", "id": "NIKKEI225", "name": "Nikkei 225"},
}
def _price_series(cc: str) -> Optional[pd.Series]:
    """시장 주가지수(월간) 가져오기. 실패 시 None."""
    cfg = PRICE.get(cc)
    if not cfg:
        return None
    try:
        if cfg["src"] == "ecos":
            s = ecos_series(cfg["stat"], cfg["item"], cycle=cfg.get("cycle", "M"))
        else:
            s = fred_series(cfg["id"])
        return s.dropna()
    except Exception:
        return None

def price_check(cc: str, phase: int, s: Optional[pd.Series] = None) -> Optional[dict]:
    """분류 국면이 기대하는 주가 방향 vs 실제 지수 추세. 어긋나면 diverge=True.
    s를 주면 재호출 없이 재사용."""
    cfg = PRICE.get(cc)
    if not cfg:
        return None
    if s is None:
        s = _price_series(cc)
    if s is None or len(s) < PX_MOM + 1:
        return None
    mom  = float(s.iloc[-1] / s.iloc[-1 - PX_MOM] - 1) * 100
    yoy_ = float(s.iloc[-1] / s.iloc[-13] - 1) * 100 if len(s) >= 13 else None
    exp = PX_EXPECT.get(phase, 0)
    actual = 1 if mom > PX_THRESH else (-1 if mom < -PX_THRESH else 0)
    diverge = (exp > 0 and actual < 0) or (exp < 0 and actual > 0)
    return {"name": cfg["name"], "mom": round(mom, 1),
            "yoy": round(yoy_, 1) if yoy_ is not None else None,
            "expect": exp, "actual": actual, "diverge": diverge}

def fmt(s: pd.Series, suffix="%", nd=2):
    try: return f"{s.dropna().iloc[-1]:.{nd}f}{suffix}"
    except Exception: return "n/a"

def fmt_at(s: pd.Series, idx, suffix="%", nd=2):
    """idx(해당 월) 시점의 값 — 정확히 그 달이 없으면 직전 최신값(as-of)."""
    try:
        return f"{s.loc[:idx].dropna().iloc[-1]:.{nd}f}{suffix}"
    except Exception:
        return "n/a"

def price_check_at(s: Optional[pd.Series], idx, phase: int, name: str) -> Optional[dict]:
    """idx 시점 기준 주가 검증(국면 기대방향 vs 실제 추세)."""
    if s is None:
        return None
    sub = s.loc[:idx].dropna()
    if len(sub) < PX_MOM + 1:
        return None
    mom  = float(sub.iloc[-1] / sub.iloc[-1 - PX_MOM] - 1) * 100
    yoy_ = float(sub.iloc[-1] / sub.iloc[-13] - 1) * 100 if len(sub) >= 13 else None
    exp = PX_EXPECT.get(phase, 0)
    actual = 1 if mom > PX_THRESH else (-1 if mom < -PX_THRESH else 0)
    diverge = (exp > 0 and actual < 0) or (exp < 0 and actual > 0)
    return {"name": name, "mom": round(mom, 1),
            "yoy": round(yoy_, 1) if yoy_ is not None else None,
            "expect": exp, "actual": actual, "diverge": diverge}

def build_read(cc, mkt, real_rate, earn_level, specs, why_tmpl) -> Read:
    """specs: [(label, series, suffix, note, src), ...] — 월별 값 계산용.
    src: {url,tip} 또는 None(계산값). metrics에 [라벨,값,note,url,tip]로 실림.
    현재(최신월) Read + HIST_START~ 월별 전체 스냅샷(hist) 생성 → 과거 시점 재현 가능."""
    df = features(real_rate, earn_level)
    px_s  = _price_series(cc)
    pname = PRICE.get(cc, {}).get("name", "")
    gate = ((px_s / px_s.shift(PX_MOM) - 1) * 100
            if px_s is not None and len(px_s) > PX_MOM else None)
    ph     = classify(df, gate_mom=gate)        # 주가 검증 게이트 적용
    ph_raw = classify(df)                        # 게이트 없는 원(매크로) 분류

    def make_why(p, pr, dRR, dE):
        if pr != p:
            return (f"매크로 신호는 {PHASES[pr]}이나, 주가 강세(검증축)가 하락 국면을 부정 "
                    f"→ {PHASES[p]}로 판정. dRR={dRR:+.2f}, dE={dE:+.2f}.")
        return why_tmpl.format(lead=("금리" if abs(dRR) > abs(dE) else "실적"), dRR=dRR, dE=dE)

    def snap(idx) -> dict:
        p, pr = int(ph[idx]), int(ph_raw[idx])
        dRR, dE = float(df.loc[idx, "dRR"]), float(df.loc[idx, "dE"])
        return {"m": str(idx), "phase": p,
                "gated": PHASES[pr] if pr != p else None,
                "dRR": round(dRR, 2), "dE": round(dE, 2),
                "watch": _watch(dRR, dE, p),
                "metrics": [[lab, fmt_at(s, idx, suf), note,
                             (sr or {}).get("url"), (sr or {}).get("tip")]
                            for (lab, s, suf, note, sr) in specs],
                "why": make_why(p, pr, dRR, dE),
                "pin": PIN[p],
                "pxchk": price_check_at(px_s, idx, p, pname)}

    cur  = snap(df.index[-1])
    hist = [snap(idx) for idx in df.index if str(idx) >= HIST_START]
    return Read(cc, mkt, cur["phase"], cur["watch"], cur["metrics"], cur["why"],
                cur["pin"], cur["pxchk"], cur["gated"], hist)

def korea_read() -> Read:
    try:    y10 = ecos_series(**ECOS["kr_10y"])
    except Exception: y10 = fred_series(FRED["kr_10y_bk"])
    cpi = yoy(ecos_series(**ECOS["kr_cpi"]))
    exp_idx = ecos_series(**ECOS["kr_expamt"])
    real = (y10 - cpi).dropna()
    specs = [("국채 10년", y10, "%", "ECOS 국고채10년",
              src(ECOS_HOME, "ECOS 817Y002 국고채(10년) · ecos.bok.or.kr에서 검색")),
             ("CPI (YoY)", cpi, "%", "ECOS 총지수",
              src(ECOS_HOME, "ECOS 901Y009 소비자물가지수 총지수")),
             ("실질금리", real, "%", "10Y − CPI", None),   # 계산값(국채10년 − CPI YoY)
             ("수출 (YoY)", yoy(exp_idx), "%", "ECOS 수출금액지수",
              src(ECOS_HOME, "ECOS 403Y001 수출금액지수"))]
    return build_read("한국", "KOSPI", real, exp_idx, specs,
                      "수출 모멘텀이 실적축을 끌고 금리·물가가 반대로 작용. "
                      "현재 주도={lead} (dRR={dRR:+.2f}, dE={dE:+.2f}).")

def japan_read(edinet=False) -> Read:
    y10 = fred_series(FRED["jp_10y"])
    cpi = yoy(estat_series(ESTAT["jp_cpi"]["id"], ESTAT["jp_cpi"]["filt"]))  # 총합 지수 → YoY
    exp = fred_series(FRED["jp_exp"])                                        # 수출액(매출) = 실적 프록시
    ip  = estat_series(ESTAT["jp_ip"]["id"], ESTAT["jp_ip"]["filt"])         # 鉱工業 생산(참고용)
    real = (y10 - cpi).dropna()
    breadth = edinet_opinc_breadth() if edinet else None
    note_exp = "FRED 일본 수출액" + (f" · EDINET 영익+ {breadth:.0f}%" if breadth is not None else "")
    specs = [("국채 10년", y10, "%", "FRED JGB10Y",
              src(FRED_PAGE.format(FRED["jp_10y"]), f"FRED {FRED['jp_10y']} · Japan 10Y")),
             ("CPI (YoY)", cpi, "%", "e-Stat 総合",
              src(ESTAT_PAGE.format(ESTAT["jp_cpi"]["id"]), "e-Stat 0003427113 · 消費者物価指数 全国総合")),
             ("실질금리", real, "%", "10Y − CPI", None),   # 계산값(JGB10Y − CPI YoY)
             ("수출 (YoY)", yoy(exp), "%", note_exp,
              src(FRED_PAGE.format(FRED["jp_exp"]), f"FRED {FRED['jp_exp']} · 일본 수출액")),
             ("생산 (YoY)", yoy(ip), "%", "e-Stat 鉱工業 (참고)",
              src(ESTAT_PAGE.format(ESTAT["jp_ip"]["id"]), "e-Stat 0004052181 · 鉱工業生産指数"))]
    return build_read("일본", "TOPIX · Nikkei", real, exp, specs,
                      "수출(실적)과 금리 정상화가 동시 진행. "
                      "현재 주도={lead} (dRR={dRR:+.2f}, dE={dE:+.2f}).")

# ──────────────────────────────────────────────────────────────────────────
# 4.5 종목 유니버스(KOSPI200 · KOSDAQ150) + KRX업종 × 6국면 스탠스
#   - 분류기와 무관(주가 외생 원칙 유지). 국면 결과를 종목 익스포저로 '번역'만 함.
#   - 스탠스: 'L'(선호/롱) · 'S'(회피/숏) · 미기재=N(중립). 1·4(전이)는 전 업종 N.
#   - 우라가미 섹터 로테이션을 KRX 업종구분 그룹에 매핑(매니저 승인 매트릭스).
# ──────────────────────────────────────────────────────────────────────────
GROUP_STANCE = {
    # group        :  0금융 2실적 3역금융 5역실적
    "securities":   {0: "L",        3: "S", 5: "S"},   # 증권: 유동성장세 주도, 긴축 직격
    "banks":        {       2: "L", 3: "S", 5: "S"},   # 은행: 실적장세 NIM 수혜
    "insurance":    {       2: "L", 3: "S", 5: "S"},
    "finance":      {0: "L",        3: "S", 5: "S"},   # 기타금융·지주
    "construction": {0: "L",        3: "S", 5: "S"},   # 금리민감 자산
    "realestate":   {0: "L",        3: "S", 5: "S"},
    "it":           {0: "L",        3: "S"},           # IT서비스: 장기듀레이션 성장
    "pharma":       {0: "L",        3: "S", 5: "L"},   # 제약: 봄 성장 + 겨울 방어
    "medical":      {0: "L",        3: "S"},
    "entertain":    {0: "L",                5: "L"},   # 오락·문화(게임 방어 가정)
    "utility":      {0: "L", 2: "S", 3: "S", 5: "L"},  # 전기·가스 유틸 방어
    "electronics":  {       2: "L",         5: "S"},   # 전기·전자(반도체) 시클리컬
    "chemicals":    {       2: "L", 3: "S", 5: "S"},
    "metals":       {       2: "L", 3: "S", 5: "S"},   # 금속·비금속
    "machinery":    {       2: "L",         5: "S"},
    "autoparts":    {       2: "L",         5: "S"},   # 운송장비·부품(차·조선)
    "logistics":    {       2: "L",         5: "S"},   # 운송·창고
    "telecom":      {       2: "S", 3: "L", 5: "L"},   # 통신: 채권성격 방어
    "food":         {0: "S", 2: "S", 3: "L", 5: "L"},  # 음식료·담배 필수재
    "retail":       {                       5: "L"},   # 유통(필수소비)
    "textile":      {                       5: "S"},
    "general":      {},                                # 일반서비스 등 혼합 → 중립
}

def sector_group(sec: str) -> str:
    """KRX 업종구분 문자열 → 스탠스 그룹. 키워드 매칭(중점문자 회피).
    순서 주의: '운송장비·부품'은 '장비'를 포함하므로 기계보다 먼저 판정."""
    s = str(sec)
    if "증권" in s: return "securities"
    if "은행" in s: return "banks"
    if "보험" in s: return "insurance"
    if "금융" in s: return "finance"          # 기타금융 · 금융
    if "건설" in s: return "construction"
    if "부동산" in s: return "realestate"
    if "IT" in s: return "it"
    if "제약" in s: return "pharma"
    if "의료" in s or "정밀" in s: return "medical"
    if "오락" in s or "문화" in s: return "entertain"
    if "가스" in s: return "utility"          # 전기·가스 (≠ 전기·전자)
    if "전자" in s: return "electronics"
    if "화학" in s: return "chemicals"
    if "금속" in s: return "metals"           # 금속 · 비금속
    if "부품" in s or "운송장비" in s: return "autoparts"
    if "기계" in s or "장비" in s: return "machinery"
    if "창고" in s: return "logistics"
    if "통신" in s: return "telecom"
    if "음식료" in s or "담배" in s: return "food"
    if "유통" in s: return "retail"
    if "섬유" in s or "의류" in s: return "textile"
    return "general"

UNIVERSE_SPEC = [("KOSPI", "KOSPI_*.xlsx", 200), ("KOSDAQ", "KOSDAQ_*.xlsx", 150)]

def load_universe(here: str) -> list[dict]:
    """Stocks/ 폴더의 최신 KOSPI·KOSDAQ 엑셀 → 시총 상위 N 종목 리스트.
    컬럼: 종목코드·종목명·시장구분·업종구분·현재가·대비·등락률·시가총액."""
    import glob
    rows: list[dict] = []
    for mkt, pat, topn in UNIVERSE_SPEC:
        files = sorted(glob.glob(os.path.join(here, "Stocks", pat)))
        if not files:
            continue
        df = pd.read_excel(files[-1])
        df.columns = (["code", "name", "market", "sector",
                       "price", "chg", "chgpct", "mcap"][:df.shape[1]])
        df = (df.dropna(subset=["mcap"])
                .sort_values("mcap", ascending=False).head(topn))
        for _, r in df.iterrows():
            rows.append({"code": str(r["code"]).split(".")[0].zfill(6),
                         "name": str(r["name"]).strip(), "mkt": mkt,
                         "sector": str(r["sector"]).strip(), "mcap": int(r["mcap"])})
    return rows

def stance_by_sector(universe: list[dict]) -> dict:
    """유니버스에 실제 등장하는 업종 → 그룹 스탠스(JSON용). phase 키는 문자열화됨."""
    return {sec: GROUP_STANCE[sector_group(sec)]
            for sec in sorted({u["sector"] for u in universe})}

def inject_universe(html: str, universe: list[dict]) -> str:
    """HTML의 const STANCE / const STOCKS 두 블록을 실데이터로 교체."""
    stance = json.dumps(stance_by_sector(universe), ensure_ascii=False)
    stocks = json.dumps(universe, ensure_ascii=False)
    html, n1 = re.subn(r"const STANCE=\{[\s\S]*?\};",
                       "const STANCE=" + stance + ";", html, count=1)
    html, n2 = re.subn(r"const STOCKS=\[[\s\S]*?\];",
                       "const STOCKS=" + stocks + ";", html, count=1)
    if not n1 or not n2:
        raise RuntimeError("템플릿에서 STANCE/STOCKS 블록을 찾지 못함")
    return html

# ──────────────────────────────────────────────────────────────────────────
# 5. 대시보드 HTML 재생성 (READS 블록만 교체)
# ──────────────────────────────────────────────────────────────────────────
def render(template_path: str, out_path: str, reads: list[Read],
           universe: Optional[list[dict]] = None):
    html = open(template_path, encoding="utf-8").read()
    payload = json.dumps([asdict(r) for r in reads], ensure_ascii=False, indent=2)
    new_block = "const READS=" + payload + ";"
    html2, n = re.subn(r"const READS=\[[\s\S]*?\];", new_block, html, count=1)
    if n == 0:
        raise RuntimeError("템플릿에서 READS 블록을 찾지 못함")
    if universe:
        html2 = inject_universe(html2, universe)
    asof = dt.date.today().isoformat()
    html2 = re.sub(r"as of \d{4}-\d{2}-\d{2}", f"as of {asof}", html2)
    open(out_path, "w", encoding="utf-8").write(html2)
    return out_path

def summary(reads: list[Read]):
    print(f"\n우라가미 6국면 · {dt.date.today().isoformat()}")
    print("-" * 56)
    for r in reads:
        w = f"  ⚠ {r.watch}" if r.watch else ""
        print(f"  {r.cc:<4} {PHASES[r.phase]:<8}{w}")
        for m in r.metrics:
            print(f"       {m[0]:<10} {m[1]:>9}   {m[2]}")
        if r.gated:
            yo = ""
            if r.pxchk:
                yo = (f" ({r.pxchk['yoy']:+.0f}% YoY)" if r.pxchk.get("yoy") is not None
                      else f" ({r.pxchk['mom']:+.0f}% 6M)")
            print(f"       ⓘ 주가 게이트: 매크로는 {r.gated}이나 주가 강세{yo} → {PHASES[r.phase]}로 보정")
        px = r.pxchk
        if px and px.get("diverge"):
            yo = f"{px['yoy']:+.0f}% YoY" if px.get("yoy") is not None else f"{px['mom']:+.0f}% 6M"
            exp = {1: "상승", -1: "하락", 0: "횡보"}[px["expect"]]
            print(f"       ⚠ 주가 괴리: {px['name']} {yo} (분류는 {exp} 예상) — 분류 재검토 필요")
    print("-" * 56)

# ──────────────────────────────────────────────────────────────────────────
def main():
    # Windows 콘솔(cp949)에서도 한글·기호(−, ·)를 깨짐 없이 출력.
    import sys
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass
    # 기본 경로는 실행 위치(cwd)가 아니라 이 스크립트 파일 기준으로 해석
    # (VS Code 등 다른 폴더에서 실행해도 템플릿을 찾도록).
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default=os.path.join(here, "uragami_live_phase.html"))
    ap.add_argument("--out",      default=os.path.join(here, "uragami_live_phase.generated.html"))
    ap.add_argument("--edinet",   action="store_true",
                    help="일본 EDINET 영업이익 확산 확증(느림, 3~5초/호출)")
    args = ap.parse_args()

    universe = load_universe(here)
    reads = [korea_read(), japan_read(edinet=args.edinet)]
    summary(reads)
    print(f"  유니버스 {len(universe)}종목 로드 (KOSPI200 · KOSDAQ150)")
    path = render(args.template, args.out, reads, universe)
    print(f"대시보드 재생성 완료 → {path}")

if __name__ == "__main__":
    main()
