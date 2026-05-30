# New Hermes Session Bootstrap

## 목적
기존 긴 Hermes 세션을 끊고, 새 WebUI 세션에서 Project OS 총괄(governor)만 맡기기 위한 시작점이다.

## canonical truth
- Repo: `/Users/parantoux/Andy/workspace/hermes-webui`
- Canonical board: `pux-import-e2e-1780010472`
- Main blocker: `t_8dcfd135`
- Acceptance target board: `pux-rootseed-proof-1780110594`
- Canonical continuity: `.ax/handoff/current.json`, `.ax/handoff/current.md`, `docs/project-os/STATUS.md`

## 새 세션 시작 방법
1. WebUI에서 **새 세션**을 연다.
2. workspace를 `/Users/parantoux/Andy/workspace` 로 둔다.
3. 첫 메시지로 아래 프롬프트를 그대로 보낸다.

## first prompt
```text
Project OS 총괄 세션을 시작한다.

작업 원칙:
- canonical truth는 repo-local continuity와 live kanban 기준으로 본다.
- 현재 canonical repo는 /Users/parantoux/Andy/workspace/hermes-webui 이다.
- 먼저 .ax/handoff/current.json, .ax/handoff/current.md, docs/project-os/STATUS.md, live kanban board `pux-import-e2e-1780010472`, active cron jobs를 읽어 현재 상태를 복구하라.
- mainline은 `t_8dcfd135` 고정이다. queue-freeze를 유지하라.
- 지금은 broad replan이나 새 epic 생성보다, blocker-first governor 모드로 운영하라.
- Codex CLI, kanban, cron을 활용해 병렬 진행하되, 사람 개입이 꼭 필요할 때만 Discord로 알리고 나머지는 조용히 계속 진행하라.
- 브라우저 자동화는 **headless/background 우선**으로 운영하라. visible popup/browser focus steal이 없어야 한다.
- acceptance에 실제 browser proof가 필요하면 먼저 headless/background로 가능한지 점검하라.
- visible popup이 **정말 필수**인 경우에만 그 단계 직전에 Discord로 operator action-needed 알림을 보내고, 가능한 나머지 병렬 작업은 계속 진행하라.
- 진행 상태는 code/product progress 와 continuity/verification progress 를 구분해서 보고하라.

즉시 할 일:
1. 현재 canonical truth 복구
2. 현재 cron/kanban/dev-review lane 상태 점검
3. 기존 Hermes 세션이 남긴 kanban 태스크와 cron 작업을 전체 audit해서, keep / pause / resume / remove 후보를 정리
4. main blocker `t_8dcfd135` 기준으로 다음 bounded slice 1개 선택
5. 안전한 자동운영 구조(조용한 dev/review + Discord escalation + headless browser 우선)가 유지되는지 확인
6. 바로 실행 시작
```

## 현재 자동운영 정책
- dev lane: `project-os-nextstep-dev-codex-5m` 계속 활성
- review lane: 활성, quiet/local 유지
- blocker resolver: 활성
- auto resume: 활성
- stall watchdog: Discord `#hermes-report` 로 알림
- browser-proof lane: 현재는 일시 정지. 새 총괄 세션이 **headless/background 안전성**을 먼저 점검한 뒤, 안전하면 resume / 아니면 paused 유지 + 필요 시 Discord escalation
- backlog reconcile / gap curator / replanner: 현재 governor-first phase 동안 paused 유지

## 운영 메모
- 새 세션은 기존 긴 세션을 이어받되, 채팅 transcript 자체보다 repo-local continuity를 우선한다.
- 기존 긴 세션은 별도 추가 작업 없이 종료해도 되지만, 사람이 보기 좋은 종료 요약이 필요하면 이 파일과 `.ax/handoff/current.md` 기준으로 wrap만 남기면 된다.
