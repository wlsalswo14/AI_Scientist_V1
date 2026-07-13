# Traceable AI Scientist

하나의 연구 목적과 핵심 질문을 받아 연구 계획, 실험, 증거 감사, 논문 심사까지 수행하는 추적 가능한 AI Scientist 파이프라인입니다. LLM이 의미 판단과 초안을 담당하고, Python 오케스트레이터가 schema, 근거 연결, hard gate, 재시도 범위와 최종 성공 조건을 검증합니다.

> 이 프로젝트는 연구 자동화 실험용 구현입니다. 생성 코드를 실행하는 `--execute-code`는 신뢰할 수 있는 전용 계정이나 컨테이너에서만 사용하십시오. 로컬 실행기는 완전한 보안 샌드박스가 아닙니다.

## 파이프라인

```text
CLI / .env
  ↓
Runtime Watchdog + checkpoint resume
  ↓
Research mode 분류 + ResearchBrief
  ↓
Anchor Director ─┐
Expansion Director ─┴→ 독립 Evaluator A/B → deterministic claim 승격
  ↓
Research Program Composer → ResearchContract → executable contract gate
  ↓
독립 Experimentor → 실행 검증 → Evidence Audit → Ex-Evaluator
  ↓
Claim Ledger → Provenance Graph
  ↓
Writer → Reviewer → 렌더링 → FinalManifest
```

단계가 실패하면 전체 실행을 처음부터 반복하지 않습니다. 실패한 target과 그 의존 범위만 무효화하고, 통과한 artifact는 잠근 채 관련 단계로 되돌아갑니다. 상세한 단계별 입력·출력과 코드 읽는 순서는 [docs/PIPELINE.md](docs/PIPELINE.md)에 정리되어 있습니다.

## 설치

요구 사항은 Python 3.11 이상과 다음 중 하나입니다.

- 로그인된 Codex CLI
- OpenAI API key

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m pytest
```

`.env`는 Git에서 제외됩니다. 공개 저장소에는 실제 키를 올리지 마십시오.

## 기본 실행

```powershell
ai-scientist run `
  --provider codex `
  --objective "연구 목적" `
  --question "검증할 핵심 연구 질문" `
  --research-depth thesis `
  --execute-code
```

주요 선택지는 다음과 같습니다.

- `--research-depth`: `quick`, `competition`, `thesis`, `publication`
- `--research-profile`: 범용 `general` 또는 provenance 연구용 `trace-audit`
- `--provider`: 로그인된 CLI를 쓰는 `codex` 또는 `OPENAI_API_KEY`를 쓰는 `openai`
- `--pipeline-smoke-test`: 실제 과학적 결과 대신 결정론적 CPU fixture로 연결만 검증
- `--run-id`: 중단된 동일 실행을 checkpoint에서 재개
- `--no-watchdog`: 디버깅할 때만 watchdog 없이 전경 실행

CLI 전체 옵션은 다음 명령으로 확인할 수 있습니다.

```powershell
ai-scientist run --help
```

코드 실행은 기본적으로 꺼져 있습니다. `--execute-code`를 지정한 경우에만 생성된 실험 코드를 실행합니다. `--pipeline-smoke-test`의 결과는 파이프라인 연결 확인용이며 원래 연구 질문의 증거로 사용할 수 없습니다.

## TRACE_AUDIT 프로파일

`trace-audit`는 paper-only, raw artifact, structured provenance, deterministic gate 조건의 false acceptance와 clean acceptance를 비교하는 전용 분기입니다. 실제 연구 실행은 외부 blinded reviewer 결정을 준비 단계와 분리합니다.

```powershell
$runId = "trace-study"

ai-scientist run `
  --provider codex `
  --research-profile trace-audit `
  --research-depth competition `
  --objective "연구 목적" `
  --question "핵심 연구 질문" `
  --prepare-trace-review `
  --run-id $runId `
  --execute-code

python tools/run_trace_external_review.py `
  --run-dir ".\runs\$runId" `
  --output ".\runs\$runId\external-review\reviewer-decisions.json" `
  --reviewer "gpt-5.6-sol/max" `
  --reviewer "gpt-5.4-mini/low"

# 첫 명령과 같은 목적·질문·깊이를 사용해 동일 run을 재개합니다.
ai-scientist run `
  --provider codex `
  --research-profile trace-audit `
  --research-depth competition `
  --objective "연구 목적" `
  --question "핵심 연구 질문" `
  --trace-review-decisions ".\runs\$runId\external-review\reviewer-decisions.json" `
  --run-id $runId `
  --execute-code
```

본 실행은 frozen contract fingerprint, C0–C3 coverage, reviewer family 수, hidden-gold consistency와 leakage 조건을 검증합니다. 준비 파일이나 결정 batch가 계약과 다르면 Experimentor 진입 전에 중단합니다.

## 결과 구조

모든 실행 산출물은 Git에서 제외된 `runs/<run-id>/`에 저장됩니다.

```text
runs/<run-id>/
├─ artifacts/            버전이 있는 구조화 artifact
├─ checkpoints/          재개 가능한 단계별 최신 상태
├─ experiments/          생성 코드, 실행 로그, 원시 결과
├─ paper/                논문, 심사 및 형식 감사 결과
├─ events.jsonl          상태 전환 감사 로그
├─ artifact_status.json  VALID/STALE 투영
├─ run_state.json        watchdog 런타임 상태
└─ manifest.json         최종 상태와 공개 산출물 경로
```

성공은 단순한 프로세스 종료가 아니라 준비된 `ResearchContract`, 통과한 실험·증거 gate, 검증된 claim/provenance 연결, Reviewer 승인이라는 불변조건을 모두 만족해야 합니다. 실패한 실행도 `paper/audit_report.md`와 `manifest.json`을 남깁니다.

## 저장소 구조

```text
src/ai_scientist/
├─ cli.py, config.py           입력과 설정
├─ orchestrator.py             전체 상태 머신과 backtracking
├─ agents/                     독립 structured LLM 호출
├─ workflows/                  단계별 반복·repair 로직
├─ schemas.py                  단계 간 데이터 계약
├─ validation.py               범용 deterministic gate
├─ program_validation.py       claim/program gate
├─ evidence_audit.py           증거 감사
├─ trace_audit.py              TRACE_AUDIT 계산·검증
├─ artifacts.py, runtime.py    artifact/checkpoint/runtime 상태
└─ rendering.py                논문과 감사 보고서 렌더링

tests/                         단위·gate·mock E2E 테스트
tools/                         범용 외부 reviewer 실행 도구
vendor/icml2026/               제출 렌더링에 필요한 스타일 파일
docs/PIPELINE.md               상세 파이프라인 설명
```

분석을 시작할 때는 `schemas.py`에서 데이터 계약을 확인한 뒤 `orchestrator.py`, `workflows/`, `validation.py` 순서로 읽는 것이 가장 빠릅니다.
