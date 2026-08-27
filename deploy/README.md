# deploy/ — 서버 유닛 정의 (참고용 사본)

Vultr 서버 `/etc/systemd/system/` 의 실물을 그대로 복사해 둔 것이다. **저장소에서 서버로 자동 배포되지 않는다** —
서버가 죽으면 이 파일로 복원할 수 있게 두는 것이 목적이다. 바꿀 때는 서버에서 바꾸고 여기로 다시 복사한다.

| 유닛 | 역할 |
| --- | --- |
| `engra.service` | 앱 서버. `127.0.0.1:4320`, Caddy 가 HTTPS 로 프록시. `EnvironmentFile=/opt/engra/.env` (AI 토큰·모드) |
| `engra-shift.timer` → `.service` | **교대 시각 06:00·18:00 KST 에 `cli.py run --latest`**. 막 끝난 근무의 초안을 AI 로 만든다. `AccuracySec=1s` 필수 — 30초 밀리면 다음 근무를 고른다 |
| `engra-reset.timer` → `.service` | 매시 정각 DB 를 **빈 상태**로 (`tools/reset_if_idle.sh`, 최근 2시간 QA 활동 있으면 건너뜀). 화면의 「완전 빈 상태로」와 같은 경로 |

`.env` 는 여기 없다 (토큰). 키 이름만: `ENGRA_LLM=cli|api|off`, `CLAUDE_CODE_OAUTH_TOKEN=` (cli), `ANTHROPIC_API_KEY=` (api).
