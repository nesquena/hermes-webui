# 프로젝트 세션 목표(`/goal`) 연동 설계 제안

## 한 줄 결론
전용 Project OS 세션의 장기 objective는 새 goal 엔진을 만들지 말고, 기존 WebUI `/goal` + `/api/goal` + `api/goals.py` 실행 루프를 그대로 재사용하되, Project OS의 `goalSummary`를 세션 goal의 상위 canonical source로 두는 thin sync layer로 연결하는 것이 가장 안전합니다.

## 왜 이 경로가 맞는가
- 기존 `/goal`은 이미 다음을 제공합니다.
  - 세션별 persisted goal state (`api/goals.py`, `goal:{session_id}` in `state.db`)
  - kickoff 시 normal chat stream 재사용 (`api/routes.py` `/api/goal` -> `_start_chat_stream_for_session(..., goal_related=True)`)
  - post-turn 평가와 자동 continuation (`api/streaming.py` + `PENDING_GOAL_CONTINUATION`)
- Project OS는 이미 별도 project truth를 일부 가지고 있습니다.
  - dedicated project session (`extensions/project-os/project-os-extension.js` `ensureProjectSession`, `sendProjectSessionPrompt`)
  - board-scoped project meta의 `goalSummary`
  - control-plane docs `docs/project-os/PROJECT.md`의 `## Goal` 섹션에서 이를 다시 읽어 동기화 (`syncProjectMetaFromDocs`)
- 따라서 필요한 것은 새 실행 레이어가 아니라, "project canonical goal ↔ dedicated session `/goal`" 사이의 ownership과 drift rule을 명확히 하는 것입니다.

## 현재 상태 요약

### 1) `/goal`의 실제 성격
현재 `/goal`은 "세션 하나에 묶인 standing goal executor"입니다.
- source of truth: `state.db` meta key `goal:{session_id}`
- scope: session-local
- actions: status/pause/resume/clear/set
- set 시 kickoff prompt를 반환하고 첫 turn을 일반 `/api/chat/start` 경로로 시작
- 후속 turn은 `goal_related=True` 일 때만 evaluate되어 unrelated turn budget burn을 막음

즉 이미 실행 primitive는 충분합니다. 부족한 것은 project-level canonical objective, multi-session continuity, drift handling뿐입니다.

### 2) Project OS의 실제 성격
Project OS extension은 이미 다음 철학으로 움직입니다.
- dedicated project session을 별도로 유지
- `goalSummary`, `nextStepSummary`, `blockerSummary`를 board/project meta로 유지
- `PROJECT.md`/`STATUS.md`와 extension 상태를 얇게 동기화
- 일반 prompt는 `buildContextualPrompt()`로 dedicated session에 보냄

즉 Project OS는 새로운 runtime이 아니라 control layer입니다. 이 철학과도 `/goal` 재사용이 가장 잘 맞습니다.

## 제안하는 ownership 모델

### canonical truth
우선순위는 아래처럼 고정합니다.
1. Project canonical goal: `board/continuity goal_summary` + `docs/project-os/PROJECT.md` `## Goal`
2. Dedicated project session의 active `/goal`: 위 canonical goal의 실행 상태 mirror
3. 일반 채팅 turn에 삽입되는 `Goal: ...` contextual prompt line: read-only hint

핵심 규칙:
- `goalSummary`가 프로젝트의 의미론적 truth입니다.
- session `/goal`은 execution truth입니다.
- 둘이 달라질 수는 있지만, 그 차이는 "의도된 drift"가 아니라 "sync pending / paused / stale session binding"으로만 설명되어야 합니다.

### 금지할 것
- Project OS 전용 별도 goal state store 신설
- board와 session에 서로 독립적인 장기 goal 텍스트 보관
- extension localStorage만 믿고 goal 상태를 장기 지속
- dedicated session 외 임의 session에서 project canonical goal을 자동 집행

## 얇은 연동 레이어 설계

### A. 최소 데이터 모델
새 canonical goal store는 만들지 않고, 아래 정도의 binding metadata만 추가합니다.

권장 위치: dedicated project session JSON extra field 또는 board meta

예시:
```json
{
  "project_goal_binding": {
    "board_slug": "pux-import-e2e-1780010472",
    "goal_source": "project_meta",
    "goal_text": "...canonical goal summary...",
    "goal_hash": "sha256:...",
    "last_synced_at": "2026-05-29T22:10:00Z",
    "last_session_goal": "...text currently pushed into /goal...",
    "sync_state": "in_sync"
  }
}
```

이 필드는 새로운 truth가 아닙니다. 아래 용도로만 씁니다.
- 현재 dedicated session이 어느 board goal에 binding되어 있는지 표시
- session goal drift 감지
- 세션 재생성/복원 시 재-sync 판단

`api/models.py`의 `Session.save()`는 metadata field 외 extra field도 JSON 끝에 저장하므로, 이런 소규모 metadata 추가와 궁합이 좋습니다.

### B. sync 방향
기본 방향은 단방향입니다.
- project canonical goal -> session `/goal`

역방향은 제한적으로만 허용합니다.
- 사용자가 dedicated project session 안에서 명시적으로 `/goal <text>`를 바꿨고,
- 그 세션이 현재 board의 bound session이며,
- Project OS UI에서 "프로젝트 목표로 승격"을 명시적으로 눌렀을 때만
- `goalSummary` / `PROJECT.md`를 갱신

즉 자동 양방향 sync는 하지 않습니다.

이유:
- session `/goal`은 실행 중 임시 조정이 잦음
- project canonical goal은 더 느리게 바뀌는 control-plane truth
- 양방향 자동 sync는 shadow truth 충돌을 만든다

## 추천 사용자 플로우

### 1) Project OS에서 goalSummary 편집/동기화
1. 사용자가 Project OS panel 또는 control-plane docs에서 goal을 바꿈
2. extension이 dedicated project session을 확인
3. bound session이 없으면 `ensureProjectSession()`로 생성
4. 현재 session goal status 조회
5. 아래 규칙에 따라 `/api/goal` 호출
   - goal 없음 -> `/goal <goalSummary>`
   - 같은 text의 active goal -> noop
   - 다른 text의 active goal -> 사용자에게 "Replace session goal" affordance 노출
   - paused same-text goal -> `/goal resume`
   - paused different-text goal -> replace affordance
6. 성공 시 binding metadata 갱신

### 2) dedicated project session에서 일반 작업 메시지 전송
- 지금처럼 `buildContextualPrompt()`는 `Goal: ${meta.goalSummary}`를 넣되 유지
- 하지만 이 문구는 실행 명령이 아니라 보조 문맥일 뿐입니다.
- 실제 장기 반복/continuation은 `/goal`이 담당

### 3) dedicated project session에서 `/goal status|pause|resume|clear`
- 이 명령은 허용
- 단, Project OS UI는 이것을 project canonical goal 변경으로 자동 승격하지 않음
- 반영 규칙:
  - `pause/resume`는 session execution status만 변경
  - `clear`는 session goal만 제거하고 Project OS에는 "project goal exists but no active session goal" 경고 상태 표시
  - project goal 자체를 없애려면 Project OS 쪽에서 `goalSummary`를 비워야 함

## UI 제안

### 1) Project OS summary/status 영역에 goal binding chip 추가
표시 상태 예시:
- `Goal executor: active`
- `Goal executor: paused`
- `Goal executor: not bound`
- `Goal executor: drifted`

### 2) drift affordance
아래 상황에만 작은 액션 버튼을 둡니다.
- canonical goal exists, session goal missing -> `Start /goal`
- canonical goal != session goal -> `Sync session goal`
- session goal exists, canonical goal missing -> `Adopt to project` 또는 `Clear session goal`

### 3) transcript/composer 쪽 변경은 최소화
- `/goal` 자체 UX는 existing WebUI 사용
- Project OS는 새 goal composer를 만들지 않음
- 필요하면 control strip 수준 버튼만 추가
  - `Start goal`
  - `Pause goal`
  - `Resume goal`
  - `Open session`

## API 제안

### Option A: 최소 변경 권장
기존 `/api/goal` 재사용 + dedicated session에 대해 status polling만 수행
- 이미 `/api/goal`은 status/pause/resume/clear/set을 지원
- 부족한 것은 dedicated session goal state를 쉽게 읽는 read surface

권장 추가:
- `GET /api/project-session-goal-status?session_id=...`
  - 내부적으로 `goal_state_snapshot()` 또는 `goal_command_payload(..., 'status')` 재사용
  - response는 기존 `/api/goal status` payload shape와 최대한 동일

장점:
- command endpoint의 side-effect semantics를 건드리지 않음
- Project OS extension이 polling/read를 더 명확히 할 수 있음

### Option B: 더 얇게 가기
read endpoint도 추가하지 않고, extension이 `/api/goal`에 `args: 'status'` POST
- 구현은 더 적음
- 다만 read를 POST side-effect command처럼 다루게 되어 계약이 덜 명확

권장안은 Option A입니다.

## 충돌 규칙

### 케이스 1) `goalSummary`와 session goal text가 다름
판정:
- canonical mismatch

처리:
- Project OS는 `drifted`
- 자동 overwrite 금지
- 사용자가 `Sync session goal` 누르면
  1. `/goal clear` 또는 새 `/goal <text>`
  2. binding metadata 갱신

### 케이스 2) session goal이 paused, canonical goal은 동일
판정:
- healthy but paused

처리:
- `Resume goal` affordance
- project goal 자체는 drift 아님

### 케이스 3) session goal cleared, canonical goal은 존재
판정:
- executor missing

처리:
- Project OS 상태는 `not bound`
- `Start goal` affordance
- 일반 contextual prompt는 계속 넣을 수 있지만 execution loop는 꺼진 상태

### 케이스 4) board/continuity `goal_summary`와 `PROJECT.md Goal`이 다름
판정:
- project control-plane drift

처리:
- session goal sync보다 먼저 docs/board canonicalization을 수행
- session `/goal`은 그 다음 단계

즉 충돌 우선순위는 항상:
1. board/docs canonical drift 해결
2. canonical -> dedicated session executor drift 해결

## 구현 slice 제안

### Slice 1: read-only binding visibility
범위:
- dedicated project session goal status 조회
- Project OS panel에 binding chip 표시
- no auto sync

변경 후보:
- `extensions/project-os/project-os-extension.js`
- 필요 시 small read route (`api/routes.py`, `api/goals.py` helper 재사용)
- regression tests for state mapping

완료 기준:
- Project OS가 `goalSummary` 존재 여부와 dedicated session `/goal` 상태를 동시에 보여줌
- active/paused/missing/drifted 구분 가능

### Slice 2: one-way sync controls
범위:
- `Start goal`, `Resume goal`, `Sync session goal` 버튼
- canonical -> session sync only
- binding metadata 저장

완료 기준:
- panel에서 dedicated session `/goal`을 시작/재개/재동기화 가능
- 기존 `/goal` transcript/continuation 동작 회귀 없음

### Slice 3: explicit promote/demote semantics
범위:
- session `/goal`을 project canonical goal로 승격하는 명시적 UX
- `clear executor only` vs `clear project goal` 구분

완료 기준:
- session-level command와 project-level meaning이 혼동되지 않음

## 테스트 포인트

### 서버/계약
- same-text sync는 noop
- paused same-text goal은 resume path
- different-text active goal은 자동 overwrite하지 않음
- kickoff 실패 시 기존 goal rollback 유지 (`restore_goal_state`)
- goal-related continuation only rule 불변

### extension/UI
- dedicated session 없는 상태에서 Start goal이 session 생성 후 동작
- project session 교체 후 binding metadata가 새 session으로 이동
- `goalSummary` 없음 + session goal 있음 상태에서 drift badge 노출
- docs sync 후 canonical goal이 바뀌면 session goal drift로 표시

### regression 위험
- 기존 chat `/goal` 명령 UX 깨짐
- Project OS control-plane docs sync가 session goal을 몰래 overwrite
- localStorage-only 상태가 브라우저 간 불일치 유발

## 왜 이 설계가 thin-layer 원칙에 맞는가
- 새 엔진 없음: existing `/goal` executor reuse
- no shadow truth: canonical goal은 기존 board/docs, execution goal은 기존 session state
- Hermes-native fallback 유지: extension이 깨져도 dedicated session 열고 `/goal status` 직접 사용 가능
- Project OS는 control strip만 추가하고 transcript/runtime semantics는 기존 WebUI 유지

## 최종 권고
1. Project canonical goal은 계속 `goalSummary`/`PROJECT.md Goal`에 둡니다.
2. dedicated project session의 `/goal`은 그 canonical goal의 executor로만 씁니다.
3. 양방향 자동 sync는 하지 말고, 기본은 canonical -> session 단방향 sync로 제한합니다.
4. 첫 구현은 read-only binding visibility + one-way sync control까지만 자릅니다.
5. 이 작업은 새 goal subsystem이 아니라 Project OS extension의 thin integration slice로 다뤄야 합니다.
