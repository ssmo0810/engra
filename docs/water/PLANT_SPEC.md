# ENGRA — 수처리 가상 Plant 시뮬레이터

## 프로젝트 목적
SK AI 해커톤 결선 데모용. AI 교대 인수인계 도구 **engra**가 ASU 외 공정에도
적용된다는 것을 보이기 위한 **가상 수처리 Plant**를 만든다.
실제 설비(Y1_GP1 Water Treatment System)의 공정 구성과 태그 체계를 그대로 쓰되,
데이터는 시뮬레이션으로 생성한다.

산출물: 태그별 시계열 CSV(정상 운전 + 이상 시나리오) 및 생성 스크립트.
엔지니어링 계산 정확도보다 **운전원이 보기에 그럴듯한 거동**이 우선이다.

## 설비 개요
- 침지식 한외여과(UF). SUEZ ZeeWeed ZW500D, PVDF 중공사, 공칭 공경 약 0.04 µm
- 카세트: 68 modules/cassette × 3 cassette/train
- 막여과 계열: A·B·C 3 train 독립 운전·세정
- 처리 흐름: 전처리 → UF 막여과 → 여과수 저장·공급
  부속 계통: CIP 세정, 폐수 회수

## 공정 단계
1. **전처리** — 원수는 Auto Strainer(500 µm)로 협잡물 제거, 응축수 회수수는
   Cation Resin으로 경도 제거 후 합류. Coagulant·NaOCl 주입 → Flocculation Tank
   에서 플록 형성 → Membrane Distribution Channel에서 3계열 균등 분배
2. **UF 막여과** — 막조 침지 막을 Process Pump로 진공 흡인하여 투과수 생산.
   막 하부 Blower 공기로 Air Scrubbing
3. **여과수 저장·공급** — Filtered Water Tank(120 m³, A/B 2실) 저장 후
   Transfer Pump로 Fab·냉각탑 공급
4. **CIP 세정** — TMP 상승 시 또는 정기. MC(약식, NaOCl) / RC(집중, NaOCl 알칼리
   → Citric Acid 산 → 헹굼). 세정폐수는 NaOH/SBS 중화
5. **폐수 회수** — 역세수·세정폐수·냉각탑 B/D 집수 → 응집 → 중화 → 응집 →
   Clarifier 침전. 상등수 재이용, 슬러지는 탈수 후 반출

## 태그 명명 규칙
형식: `10-<기기코드><계통번호>-<일련><계열>`

| 코드 | 기기 | 예시 |
|---|---|---|
| D | Tank / Vessel | 10-D8499-02A |
| P | Pump | 10-P8469-01A/B/C |
| C | Blower | 10-C8461-01A/B |
| S | Strainer / Resin / Dehydrator | 10-S8445-01 |
| AG | Agitator | 10-AG8496-01 |
| CD | Chemical Dosing Tank | 10-CD8496-02 |
| CP | Chemical Dosing Pump | 10-CP8496-02A/B |
| E | Heater | 10-E8496-01 |

계통번호 구분: `84xx` 막여과 본계통 · `82xx` 폐수 회수 계통 · `8496` 약품 주입
계열 접미사: A/B = 1운전·1예비, A/B/C = 막 3계열

## 주요 기기 (시뮬레이션 대상)
| 태그 | 기기 | 정격 |
|---|---|---|
| 10-S8445-01 | Auto Self-Cleaning Strainer | 200 m³/hr, 500 µm |
| 10-S8446-01A/B | Cation Resin | 13 m³/hr, 450 L |
| 10-D8499-01A | Flocculation Tank | 13 m³ |
| 10-D8499-01B | Membrane Distribution Channel | 9 m³ |
| 10-D8499-02A/B/C | Membrane & Cassette Tank | 8.5 m³/train |
| 10-P8469-01A/B/C | Process Pump (인버터) | 112 m³/hr × 20 m, 11 kW |
| 10-P8469-02A/B/C | Drain Pump | 12 m³/hr × 30 m |
| 10-C8461-01A/B | Blower for Membrane | 833 Nm³/hr × 35 kPa, 18.5 kW |
| 10-D8499-03A/B | Filtered Water Tank | 120 m³ |
| 10-P8469-03A/B | Filtered Water Transfer Pump | 170 m³/hr × 90 m, 75 kW |
| 10-D8499-04 | RC WW Tank (CIP Tank) | 14 m³, 히터 35 kW, 약 100 ℃ |
| 10-D8299-06 | Clarifier | 60 m³ |

## 핵심 계측 태그
| 태그 | 측정 | 정상 기준 | 이상 판정 |
|---|---|---|---|
| VPT-01 | 흡인압 / TMP | 약 104 kPa | 상승 → 막 오염, CIP 검토 |
| TUR-01 | 투과수 탁도 | < 0.1 NTU (목표) | 급등 → 막 파손 의심 |
| AT-01 | 여과수 전도도 | — | 상승 → 수질 이상 |
| PSH-01 | 흡인 고압 스위치 | — | 작동 → 흡인 이상 |
| LT-01 | Distribution Channel 수위 | — | |
| LT-02 | 막조 수위 | — | |
| LT-03A/B | 여과수 탱크 수위 | — | |
| LT-06 | WW Transfer Tank 수위 | — | |
| LT-08 | Clarifier 상등수 | — | |
| PH-01 | 전처리 pH | — | |
| AT-02 / AT-03 | CIP 폐수 pH / ORP | — | 중화 판정 |
| AT-06 | 중화조 pH | — | |
| TT-01 | CIP 세정온도 | 약 100 ℃ | |

## 약품 (9종)
H2SO4 · NaOCl(12.6%) · NaOH(5%) · Coagulant · SBS(38%) · Citric Acid(50%) ·
Antiscalant · 동부식방지제 · Polymer
모든 주입 펌프는 A/B 이중(1운전·1예비).

## DCS 화면 구성 (9종)
OVERVIEW · PRE TREATMENT · MEMBRANE A&B · MEMBRANE C / FILTERED TK ·
RECOVERY & MAINTENANCE · WASTE WATER TRANSFER · POST TREATMENT ·
CHEMICAL#1 · CHEMICAL#2
보조 탭: UF SYSTEM · SETTING · TREND · SYSTEM

## 이상 시나리오 (데모용 우선순위)
1. **막 오염 누적** — TMP가 수 시간에 걸쳐 서서히 상승. 알람 임계 전 드리프트
   구간이 engra의 핵심 소구점
2. **막 파손** — TUR-01 급등, 해당 계열만 발생 (A·B·C 독립이므로 계열 구분 가능)
3. **전처리 부하 급증** — 원수 탁도 상승 → 응집 부하 증가 → 후단 TMP 영향
4. **Blower 절체** — A/B 전환 시 스크러빙 공기량 일시 변동

## 데이터 생성 규칙
- 샘플링 2초 주기 기준 (engra의 기존 ASU 데이터와 동일하게 맞춘다)
- 정상 구간은 태그별 중심값 + 산포로 생성하고, 관리한계 대비 이탈로 이상 판정
  (규격 판정이 아닌 통계적 공정관리 방식)
- 이상은 계단 변화가 아니라 드리프트로 넣는다 — 알람 전 구간이 보여야 한다
- A·B·C 계열은 독립 생성하되 공통 원인(전처리 부하)은 3계열에 함께 반영

## 작업 규칙
- 파일·폴더·변수명은 영문. 한글은 주석과 출력 라벨에만
- 태그명은 실제 표기 그대로 유지 (`10-P8469-01A` 형식)
- 미확정 사양은 `(VP 확인 후 기입 예정)`으로 남아 있음 — 임의로 채우지 말고
  시뮬레이션용 가정값임을 코드 주석에 명시할 것
