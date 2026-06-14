# hermes-webui macOS launchd 자동 시작

macOS 로그인 시 hermes-webui 서버를 자동으로 시작하려면 `launchd`를 사용합니다.

## launchd vs ctl.sh

`launchd`와 `ctl.sh`는 **둘 중 하나만 활성화**해야 합니다. 같은 포트에서 두 프로세스가 충돌하는 것을 방지하기 위해 `ctl.sh`는 launchd job을 감지하면 시작을 거부합니다.

| 방식 | 시작 방법 | 관리 |
|------|-----------|------|
| launchd | 로그인 시 자동 (`RunAtLoad`) + `KeepAlive`로 크래시 복구 | `launchctl` 명령어 |
| ctl.sh | 수동 `./ctl.sh start` | `./ctl.sh stop/restart/status` |

## 설치

```bash
# plist를 ~/Library/LaunchAgents/에 설치 (load는 하지 않음)
./scripts/launchd/install.sh

# 설치 후 수동으로 load (macOS Ventura+):
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.parantoux.hermes-webui.plist

# 또는 구형 macOS:
launchctl load ~/Library/LaunchAgents/com.parantoux.hermes-webui.plist
```

## 상태 확인

```bash
# launchd job 상태 + 서버 health check (한국어 요약)
./scripts/launchd/status.sh

# ctl.sh로도 확인 가능 (launchd가 관리 중일 때는 PID/cgroup 정보가 다를 수 있음)
./ctl.sh status
```

## launchd 사용 중 수동 재시작

```bash
# launchd job을 강제로 재시작 (프로세스를 kill하고 다시 spawn)
launchctl kickstart -k gui/$(id -u)/com.parantoux.hermes-webui
```

`./ctl.sh restart`는 **사용하지 마세요** — launchd가 관리 중일 때 ctl.sh는 충돌을 감지하고 거부합니다.

## launchd에서 ctl.sh로 전환

```bash
# 1. launchd job 중지 및 등록 해제 (macOS Ventura+):
launchctl bootout gui/$(id -u)/com.parantoux.hermes-webui

# 또는 구형 macOS:
launchctl unload ~/Library/LaunchAgents/com.parantoux.hermes-webui.plist

# 2. ctl.sh로 시작:
./ctl.sh start
```

## ctl.sh에서 launchd로 전환

```bash
# 1. ctl.sh로 중지:
./ctl.sh stop

# 2. launchd job 등록 및 시작:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.parantoux.hermes-webui.plist
```

## 제거

```bash
# plist 파일 제거 + 수동 unload 명령어 안내 (실제 unload는 실행하지 않음)
./scripts/launchd/uninstall.sh
```

## 충돌 감지 동작 방식

`ctl.sh`의 `start_cmd()`는 시작 전에 `launchctl print gui/$(id -u)/com.parantoux.hermes-webui`를 조회합니다. 동일 포트(8788)에서 실행 중인 launchd job이 있으면:

```
[ctl] Refusing to start a second Hermes WebUI while launchd job com.parantoux.hermes-webui is running (PID 12345).
[ctl] Use launchctl kickstart -k gui/501/com.parantoux.hermes-webui or disable the launchd job before using ctl.sh start.
```

와 같이 거부 메시지를 출력하고 종료 코드 2로 빠져나갑니다. `HERMES_WEBUI_CTL_ALLOW_LAUNCHD_CONFLICT=1`이 설정된 환경(launchd plist에서 설정됨)에서는 launchd 자신이 이 검사를 건너뛰므로 launchd job 자체는 정상 동작합니다.

## 문제 해결

- **로그 확인**: `tail -f ~/.hermes/webui.log`
- **launchd job이 안 뜰 때**: `launchctl print gui/$(id -u)/com.parantoux.hermes-webui`로 상태/종료 코드 확인
- **포트 충돌**: `lsof -iTCP:8788 -sTCP:LISTEN`으로 어떤 프로세스가 포트를 점유 중인지 확인
