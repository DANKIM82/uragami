# Uragami Phase Pipeline (우라가미 6국면 라이브 분류)

### 👉 [**라이브 대시보드 바로 보기 (https://dankim82.github.io/uragami/)**](https://dankim82.github.io/uragami/)

> 한국·일본 현재 국면을 데이터가 주입된 대시보드로 바로 확인 (정적 스냅샷).

---

우라가미 쿠니오(浦上邦雄)의 **주식시장 6국면(사계)** 모델을 라이브 매크로 데이터로
자동 분류하는 파이프라인입니다. 외생 매크로 지표(금리·실적 프록시)만으로 한국·일본
시장의 현재 국면을 판정하고, 대시보드 HTML(`uragami_live_phase.html`)에 결과를 주입해
`uragami_live_phase.generated.html`을 생성합니다.

> 핵심 설계 원칙: **분류기 입력은 주가 외생 매크로만 사용**합니다. 주가는 분류 입력이
> 아니라 *검증축*(오분류 게이트)과 *익스포저 맵*으로만 쓰여 순환참조를 방지합니다.

## 6국면 모델

| # | 국면 | 주도 변수 | 기대 주가 |
|---|------|-----------|-----------|
| 0 | 금융장세 | 금리 하락(dRR↓) | 상승 |
| 1 | 중간반락 | 데드존(금융계열) | 횡보 |
| 2 | 실적장세 | 실적 개선(dE↑) | 상승 |
| 3 | 역금융장세 | 금리 상승(dRR↑) | 하락 |
| 4 | 중간반등 | 데드존(역금융계열) | 횡보 |
| 5 | 역실적장세 | 실적 악화(dE↓) | 하락 |

사이클은 링(ring)으로 한 방향(0→1→…→5→0)으로만 진행하며, 히스테리시스로 노이즈
역행을 억제합니다.

## 동작 개요

1. **데이터 수집** — ECOS(한국), FRED(일본·글로벌 금리/수출), e-Stat(일본 CPI·광공업
   생산), EDINET(일본 영업이익 YoY 확산, 선택).
2. **피처 계산** — 실질금리 변화 `dRR`, 실적 프록시 변화 `dE`를 3M 평활 후 3개월 변화량을
   trailing 60개월 z-score로 표준화.
3. **분류** — 순서형 6상태 분류기(링 제약 + 히스테리시스). 임계치는 피팅이 아니라 경제적
   근거로 고정(`STRONG=1.0`, `FLAT=0.3`).
4. **주가 검증 게이트** — 하락 국면(역금융·역실적) 진입을 주가 강세가 거부(예: 일본
   리플레이션을 역금융으로 오분류하는 것을 차단).
5. **렌더링** — 분류 결과 + 섹터 익스포저 맵을 대시보드 HTML에 주입.

## 요구사항

- Python 3.10+
- 의존성: `requests`, `pandas`, `numpy`

```bash
pip install requests pandas numpy
```

## API 키 (환경변수)

소스에는 키가 포함되어 있지 않습니다. 아래 환경변수를 설정하세요(미설정 시 해당 소스는
실패합니다).

| 환경변수 | 용도 | 발급처 |
|----------|------|--------|
| `ECOS_API_KEY` | 한국 금리·수출금액지수·KOSPI | https://ecos.bok.or.kr/ |
| `FRED_API_KEY` | 일본·글로벌 금리, 일본 수출, Nikkei | https://fred.stlouisfed.org/docs/api/api_key.html |
| `ESTAT_APP_ID` | 일본 CPI·광공업생산 | https://www.e-stat.go.jp/api/ |
| `EDINET_API_KEY` | 일본 영업이익 YoY 확산(선택) | https://disclosure2.edinet-fsa.go.jp/ |

PowerShell 예시:

```powershell
$env:ECOS_API_KEY = "..."
$env:FRED_API_KEY = "..."
$env:ESTAT_APP_ID = "..."
$env:EDINET_API_KEY = "..."   # 선택
```

## 사용법

```bash
python uragami_phase_pipeline.py \
    --template uragami_live_phase.html \
    --out uragami_live_phase.generated.html \
    [--edinet]
```

- `--template` : 입력 대시보드 HTML (기본: `uragami_live_phase.html`)
- `--out`      : 생성 결과 HTML (기본: `uragami_live_phase.generated.html`)
- `--edinet`   : EDINET 영업이익 확산 신호 포함(느림, 선택)

## 구성

```
uragami_phase_pipeline.py        # 메인 파이프라인 (페처 → 분류 → 렌더)
uragami_live_phase.html          # 대시보드 템플릿
uragami_live_phase.generated.html# 생성 결과(파이프라인이 재생성)
Stocks/                          # 종목 유니버스(KOSPI/KOSDAQ) — 섹터 익스포저 맵
```

## 주의

- 통계/시리즈 코드(ECOS stat/item, FRED series id, e-Stat statsDataId)는 제공처에서
  변경될 수 있으므로 본인 환경에서 1회 검증하세요(소스 내 `# CHECK` 표시 참고).
- 분류 결과는 투자 자문이 아니며, 매크로 기반 국면 진단 도구입니다.
