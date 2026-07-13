# 파이프라인 구조

이 문서는 실행 이력이나 특정 실험 결과가 아니라 현재 코드의 제어 흐름만 설명합니다. 최상위 진입점은 `ai_scientist.cli:main`, 전체 상태 머신은 `ResearchOrchestrator.run`입니다.

## 설계 경계

파이프라인은 역할을 두 층으로 나눕니다.

- LLM 계층은 연구 모드, claim, 실험안, 증거의 의미, 논문과 심사 의견을 구조화된 schema로 생성합니다.
- deterministic 계층은 Pydantic schema, 점수와 hard gate, ID 연결, dependency, 수치 재계산, artifact 상태와 성공 불변조건을 Python으로 검사합니다.

LLM의 자기평가만으로 다음 단계에 진입할 수 없습니다. 각 workflow의 결과는 deterministic validation을 통과해야 checkpoint로 승격됩니다.

## 전체 제어 흐름

```text
Settings + ResearchBrief
        │
        ▼
ResearchModeWorkflow
        │
        ├─ 기본: DualDirectorResearchWorkflow
        │    Anchor + Expansion Directors
        │      → 독립 Program Evaluator A/B
        │      → claim promotion/dependency gate
        │      → Research Program Composer
        │
        └─ legacy: mode에 따라 HypothesisWorkflow 또는 DirectResearchWorkflow
                     │
                     ▼
              ResearchContract
                     │
          ExecutableSuccessContract gate
                     │
                     ▼
              ExperimentWorkflow
       design → independent execution → validation
                     │
              EvidenceAuditPipeline
                     │
                 Ex-Evaluator
                     │
                     ▼
           Claim Ledger + Provenance Graph
                     │
                     ▼
                PaperWorkflow
              Writer → Reviewer
                     │
                     ▼
          rendering + format audit + manifest
```

`RuntimeWatchdog`는 이 흐름의 바깥에서 자식 Python 프로세스를 감시합니다. 프로세스 종료나 heartbeat 정체가 감지되면 같은 `run_id`의 마지막 유효 checkpoint부터 재개합니다.

## 단계별 계약

| 단계 | 주요 구현 | 통과 조건과 산출물 |
|---|---|---|
| 입력·런타임 | `cli.py`, `config.py`, `watchdog.py`, `runtime.py` | 목적·질문·제약을 정규화하고 run 상태를 초기화합니다. |
| 모드 판정 | `agents/planning.py`, `workflows/planning.py` | 연구 유형과 근거가 schema 및 mode validation을 통과해야 합니다. |
| 연구 계획 | `workflows/research_program.py` | 기본 경로에서 Anchor/Expansion claim을 독립 생성하고 Evaluator A/B가 claim별 gate를 모두 통과시켜야 합니다. |
| 프로그램 조립 | `program_validation.py` | 승격 claim의 dependency, 깊이별 확장 수, stage 순서와 선택 target을 검사해 `ResearchContract`를 만듭니다. |
| 실행 가능성 | `success_contract.py` | 성공 기준이 실제 Python validator에 연결되고 traceability, 반증 가능성, 자원·예측 조건을 만족해야 합니다. |
| 실험 | `workflows/experiment.py`, `execution.py` | target별 코드와 결과를 격리된 작업 폴더에서 실행하고 entrypoint, 결과 schema, 근거 연결을 검증합니다. |
| 증거 감사 | `evidence_audit.py` | 독립 critic 질문, 질문별 resolver, 전역 concern audit 후 남은 MAJOR/FATAL 문제가 없어야 합니다. |
| 실험 평가 | `agents/experiment.py`, `validation.py` | Ex-Evaluator의 실행·통계·추적 gate가 통과해야 Writer로 이동합니다. 유효한 null/반증 결과도 계약을 지키면 통과할 수 있습니다. |
| 추적성 | `trace_audit.py`, `provenance.py` | claim–result–experiment–code 연결을 Claim Ledger와 Provenance Graph로 재구성하고 누락·불일치를 거부합니다. |
| 논문 | `workflows/paper.py`, `rendering.py` | Writer 초안의 근거·부호·인용을 검사하고 Reviewer가 모든 hard gate를 승인해야 최종 논문을 렌더링합니다. |
| 종료 | `orchestrator.py` | ready contract, passed experiment, accepted paper가 모두 참일 때만 `SUCCESS`를 기록합니다. |

## 독립성, repair, backtracking

Evaluator A/B, 가설별 Experimentor, Evidence Critic과 Resolver는 서로 상태를 공유하지 않는 별도 structured 호출입니다. 이 격리는 다수결처럼 보이는 단일 세션 자기확신을 줄이고, 어느 산출물이 어느 판정에 사용됐는지 artifact dependency로 남기기 위한 것입니다.

오류 처리 범위는 다음과 같습니다.

1. schema 또는 한 component의 필드가 잘못되면 해당 component만 제한된 횟수로 repair합니다.
2. claim gate가 실패하면 실패 claim의 오답노트만 원래 Director에 돌려보냅니다.
3. 실행/증거 문제가 특정 target에 한정되면 통과한 target은 잠그고 영향 target만 재계획 또는 재실험합니다.
4. Reviewer는 문제 유형에 따라 논문 수정, 분석, 실험, 연구 계획 중 가장 가까운 단계로 돌려보냅니다.
5. 수정된 상위 artifact의 하위 dependency는 `STALE`로 표시하며 기존 파일을 덮어쓰지 않습니다.

반복 횟수, 정체 판정, 전체 backtrack 수와 deadline은 `Settings`와 `.env`의 `AISCI_*` 값으로 제한됩니다.

## TRACE_AUDIT 분기

`research_profile=trace_audit`는 범용 골격에 다음 계약을 추가합니다.

```text
Dual-Director tension cards
  → C0 paper-only / C1 raw / C2 provenance / C3 deterministic gate 계획
  → frozen benchmark + corruption manifest
  → 외부 blinded reviewer decision batch
  → decision batch integrity 검사
  → false/clean acceptance와 비용 재계산
  → trace 전용 Claim Ledger, Provenance Graph, 제출 형식 감사
```

실제 reviewer inference는 `--prepare-trace-review`에서 생성한 고정 계약을 `tools/run_trace_external_review.py`가 읽어 수행합니다. 이후 동일 `run_id`를 `--trace-review-decisions`와 함께 재개합니다. `--pipeline-smoke-test`만 내부 deterministic fixture를 허용하며, 이 결과의 `scientific_claim_valid`는 거짓으로 고정됩니다.

## 상태와 산출물

`ArtifactStore`는 각 구조화 출력에 ID, 종류, 생성 시각, dependency와 metadata를 붙여 `artifacts/`에 저장합니다. `checkpoints/`는 재개용 최신 상태이고, `artifact_status.json`은 immutable artifact를 `VALID` 또는 `STALE`로 투영합니다.

`manifest.json`은 외부 소비자가 먼저 읽어야 하는 파일입니다. 최종 상태, 마지막 단계, 선택 target, 논문 경로와 미해결 문제를 한곳에 모읍니다. 상세 감사에는 `events.jsonl`, 해당 artifact envelope, 실패 시 `paper/audit_report.md`를 사용합니다.

## 코드 읽는 순서

1. `schemas.py`: 단계 사이에 전달되는 데이터 계약과 enum
2. `orchestrator.py`: 단계 전환, 성공 불변조건, 전역 backtracking
3. `workflows/research_program.py`: 기본 Dual-Director 계획 경로
4. `program_validation.py`와 `success_contract.py`: 연구 계획의 deterministic gate
5. `workflows/experiment.py`와 `evidence_audit.py`: 실행 및 증거 감사
6. `workflows/paper.py`와 `rendering.py`: 논문 심사와 최종 파일
7. `artifacts.py`, `runtime.py`, `watchdog.py`: 추적성과 복구

회귀 동작은 `tests/test_orchestrator_e2e_mock.py`, hard gate는 `tests/test_pipeline_hard_gates.py`, Dual-Director는 `tests/test_research_program.py`, TRACE_AUDIT는 `tests/test_trace_audit.py`, 런타임 복구는 `tests/test_operational_controls.py`에서 확인할 수 있습니다.
