# myflow

Babel의 개인 workflow 모음

## 0. 준비물

이 repo를 활용하기 위해 필요한 것들

| 준비물 | 내용 | 사용처 |
|---|---|---|
| python3, git | 스크립트 실행(표준 라이브러리만 사용) / 자동 업데이트 | 전체 |
| Claude Code | 설치 + 로그인 필요. <br> `claude` 명령이 $PATH 에 있어야 함. | flow 1, 2 (요약 생성) |
| `~/.claude/notion_token.txt` | Notion integration 액세스 토큰 한 줄 (`chmod 600`) | flow 1, 2 (Notion 기록) |
| Notion 데이터베이스 | `NOTION_DATABASE_ID` 가 가리키는 노션 로그 DB. <br> 조건: <br> ① 속성 `작업`(title) / `프로젝트`(select) / `날짜`(date) / `세션ID`(rich_text) <br> ② sub-item 활성화 (`Parent item` relation) <br> ③ integration 과 연결 필요 | flow 1, 2 |
| `~/.claude/settings.json` 의 Stop 훅 | `auto_update.sh` 가 리포의 `stop_hooks.json` 기준으로 자동 등록·동기화 (jq 필요) | flow 1 |
| jq | JSON을 안전하게 읽고 고치는 명령줄 도구 — Stop 훅 자동 동기화에 사용. <br> mac: `brew install jq` / linux: `apt install jq` | Stop 훅 동기화 |

> 다른 Notion 워크스페이스에서 쓰려면 위 조건대로 DB를 만든 뒤
> `notion_logger.py` / `daily_summary.py` 의 `NOTION_DATABASE_ID` 를 바꿔야 함.

(`~/.claude/hooks/` 심링크, `~/.claude/notion-log/` 폴더는 스크립트가 자동으로 생성)

## 1. Claude Code 작업 로그 Notion 자동 기록

Claude Code로 작업한 내용을 세션 단위로 요약해 Notion 데이터베이스에 자동으로 쌓는 flow.
"매일 무슨 작업을 했는지"를 따로 기록하지 않아도 일지가 만들어진다.

### 동작 방식

```
Claude Code 턴 종료 (Stop 훅)
  → 훅이 백그라운드 워커를 띄우고 즉시 리턴 (턴 블로킹 없음)
  → 워커가 transcript에서 이번 턴의 프롬프트 + 응답 추출
  → claude -p 로 "세션 제목 + 턴 요약" 생성 (한국어, JSON)
  → Notion DB에 기록
      - 세션 1개 = DB 행 1개 (제목은 턴마다 세션 전체를 아우르게 갱신)
      - 턴별 상세는 행 본문에 [HH:MM] bullet 타임라인으로 append
  → 실패 시 ~/.claude/notion-log/YYYY-MM-DD.md 에 로컬 fallback
```

- **재귀 가드**: 요약용 `claude -p` 호출도 Stop 훅을 타기 때문에 Naive 구현으로는 무한 재귀에 빠지게 된다.
  `CLAUDE_NOTION_LOGGER_SKIP` 환경변수로 무한 재귀를 차단한다.
- **세션 ↔ 행 매핑**: <br> 1. 로컬 캐시(`~/.claude/notion-log/session_map.json`)
  <br> 2. 없으면 DB의 `세션ID` 속성 조회 → 없으면 새 행 생성.
  <br> 3. 캐시가 유실돼도 중복 행이 생기지 않는다.

### 구성 요소

| 파일 | 역할 |
|---|---|
| `~/.claude/hooks/notion_logger.py` | Stop 훅 스크립트 — 이 리포 `notion_logger.py` 의 심링크 (표준 라이브러리만 사용, 맥/리눅스 공용) |
| `~/.claude/notion_token.txt` | Notion integration 액세스 토큰 (chmod 600) |
| `~/.claude/settings.json` | 전역 `Stop` 훅 등록 — `auto_update.sh` 가 `stop_hooks.json` 과 다르면 자동으로 맞춰줌 |
| `stop_hooks.json` (이 리포) | Stop 훅의 "최신 상태" 원본. `{{REPO_DIR}}` 는 적용 시 실제 repo 경로로 치환됨. 훅 구성을 바꾸려면 이 파일을 고쳐서 push |

Notion 세팅:

- 데이터베이스 스키마: `작업`(title) / `프로젝트`(select, cwd 폴더명) / `날짜`(date) / `세션ID`(rich_text)
- DB ID는 스크립트 상수 `NOTION_DATABASE_ID` 에 하드코딩
- 뷰는 API로 생성 불가 → Notion UI에서 수동 구성(혹은 claude cowork에게 요청)
  - **날짜별 뷰**: Group = 날짜(일), Sub-group = 프로젝트
  - **프로젝트별 뷰**: Group = 프로젝트, Sub-group = 날짜(일)
  - 두 뷰는 같은 DB를 바라보므로 데이터는 단일 소스

### 새 머신에 적용하기

1. Claude Code 설치 + 로그인 확인: `claude -p "ping"`
2. 리포 clone 후 `auto_update.sh` 실행 — `~/.claude/hooks/notion_logger.py`
   바로가기가 자동 생성된다. 토큰만 따로 복사한다:
   ```bash
   ssh $SERVER "git clone <repo-url> ~/prj/myflow && ~/prj/myflow/auto_update.sh"
   scp ~/.claude/notion_token.txt $SERVER:~/.claude/
   ssh $SERVER "chmod 600 ~/.claude/notion_token.txt"
   ```
3. Stop 훅은 2번에서 실행한 `auto_update.sh` 가 자동으로 등록한다
   (리포의 `stop_hooks.json` 을 원본으로 `~/.claude/settings.json` 의 `hooks.Stop` 만 교체,
   나머지 설정은 그대로 둠). 서버에 jq 가 없으면 먼저 설치할 것 (`apt install jq`).
   훅 구성을 바꾸고 싶으면 settings.json 을 직접 고치지 말고 `stop_hooks.json` 을
   수정해서 push — 각 머신의 `auto_update.sh` 가 다음 실행 때 알아서 반영한다.
4. 동작 테스트:
   ```bash
   tp=$(find ~/.claude/projects -name "*.jsonl" | head -1)
   echo "{\"transcript_path\":\"$tp\",\"cwd\":\"$HOME/test\",\"session_id\":\"server-test\"}" \
     | python3 ~/.claude/hooks/notion_logger.py --worker
   ```
   Notion DB에 `server-test` 행이 생기면 성공 (확인 후 삭제).
   행 대신 `~/.claude/notion-log/*.md` 에 기록이 생기면 claude 로그인
   또는 외부망 차단 문제.

### 트러블슈팅

- **기록이 안 쌓임**: `~/.claude/notion-log/` 에 fallback 파일이 있는지 확인.
  있다면 Notion API 호출이 실패한 것 (토큰/연결/네트워크).
- **404 object_not_found**: 대상 페이지·DB가 integration에 연결돼 있는지 확인
  (Notion 페이지 `•••` → 연결).
- **토큰 만료/교체**: `~/.claude/notion_token.txt` 내용만 갈아끼우면 됨.

> 훅 스크립트의 원본은 **이 리포의 `notion_logger.py`** 이고 (git 추적함),
> 실행되는 target python file은 `~/.claude/hooks/notion_logger.py` 는 그걸 가리키는 심볼릭 링크 바로가기
> 바로가기는 `auto_update.sh` 가 없으면 자동 생성
> 덕분에 `git pull` 로 리포가 갱신되면 훅도 즉시 새 코드를 쓴다.

## 2. 일일 회고 자동 생성

1번 flow가 쌓아둔 작업 로그 DB를 하루 단위로 읽어, 프로젝트별로 정리한 회고를
같은 Notion 페이지 본문(DB 아래)에 `yyyy.mm` 토글 → `yyyy.mm.dd` 토글 구조로 기록한다.

### 동작 방식

```
daily_summary.py 실행 (수동 또는 cron)
  → 작업 로그 DB에서 날짜 = 대상일 인 행 전체 조회
  → 각 행(세션)의 제목 + [HH:MM] 턴 bullet 수집, 프로젝트별 그룹핑
  → claude -p 로 프로젝트별 "한 줄 요약 + 세부 작업" JSON 생성
      (실패 시 세션 제목/턴 bullet 을 그대로 쓰는 raw fallback)
  → DB의 부모 페이지에서 "yyyy.mm" 토글 탐색 (없으면 생성)
  → 그 아래 "yyyy.mm.dd" 토글 생성 후 프로젝트별 bullet 기록
      - 같은 날짜 토글이 이미 있으면 지우고 다시 씀 (재실행 안전)
```

- 1번 flow와 같은 토큰(`~/.claude/notion_token.txt`)·DB ID를 사용한다.
- `claude -p` 호출에 `CLAUDE_NOTION_LOGGER_SKIP=1` 을 걸어
  요약 호출이 1번 flow의 Stop 훅에 다시 기록되는 것을 막는다.

### 사용법

```bash
python3 daily_summary.py                  # 오늘 회고 기록
python3 daily_summary.py --date 2026-06-10
python3 daily_summary.py --dry-run        # Notion에 쓰지 않고 결과만 출력
```

매일 자동 실행하려면 cron 등록:

```bash
# 매일 21:45 (그날 로그가 없으면 아무것도 기록하지 않음)
45 21 * * * /usr/bin/python3 ~/prj/myflow/daily_summary.py >> ~/.claude/notion-log/daily_summary.log 2>&1
```

### 주의

- 월 토글 탐색은 페이지 본문의 **토글 블록**(`yyyy.mm` 텍스트)만 대상으로 한다.
  토글 헤딩(heading + 접기)으로 수동 생성한 블록은 인식하지 못한다.
- 새 월 토글은 페이지 맨 아래에 append 된다. 위치를 옮겨도 탐색에는 영향 없다.

## 3. Claude Code 작업을 Linear 이슈로 등록 (skill)

flow 1·2(Notion 자동 기록)와 **완전히 별개**인, 사용자가 직접 호출하는 Claude Code skill.
이번 세션 또는 오늘 한 작업을 요약해 **Linear에 새 이슈 한 개를 생성**한다.
자동 트리거(Stop 훅)가 없고, 사용자가 "지금 작업 linear 이슈로 만들어" 처럼 부를 때만 동작한다.

- **신규 이슈 생성 전용** (기존 이슈에 코멘트를 다는 용도가 아님)
- **Notion DB를 거치지 않음** — 세션 모드는 현재 대화가, 오늘 모드만 Notion 로그가 소스
- 전제: 해당 머신에 Linear MCP(`claude.ai Linear` 커넥터)가 연결되어 있어야 함
  (`claude` 안에서 `/mcp` → `claude.ai Linear` 인증)

### 동작 방식

```
사용자가 skill 호출
  → 모드 판별
      - 세션 모드: 현재 대화 내용을 요약
      - 오늘 모드: skills/issue-generator/fetch_today.py 로
                   현재 프로젝트(cwd 폴더명)의 오늘 Notion 로그를 가져와 요약
  → 팀 결정: ~/.claude/issue-generator/linear_map.json 캐시 조회
      - 있으면 그 팀(확인만), 없으면 추천값과 함께 질문 후 캐시에 저장
  → title/description 자동 생성, point·due·priority 등은 추천값과 함께 질문
  → 미리보기 + 승인
  → Linear MCP save_issue 로 신규 이슈 생성 → 이슈 identifier/URL 출력
```

### 구성 요소

| 파일 | 역할 |
|---|---|
| `skills/issue-generator/SKILL.md` (이 리포) | skill 본체 — 위 절차를 담은 지침. git 원본 |
| `~/.claude/skills/issue-generator` | 위 디렉토리를 가리키는 심링크 — `auto_update.sh` 가 자동 생성 |
| `skills/issue-generator/fetch_today.py` | "오늘 모드" 헬퍼. `daily_summary.py` 의 조회 함수를 재사용해 특정 프로젝트의 그날 로그를 JSON 으로 출력 |
| `~/.claude/issue-generator/linear_map.json` | repo↔Linear 팀 매핑 로컬 캐시. **repo 밖에 두며 git에 올리지 않음** (서버마다 다를 수 있는 로컬 상태) |

> skill 도 훅 스크립트와 같은 방식으로 배포된다 — 원본은 이 리포의 `skills/` 이고,
> `~/.claude/skills/` 에는 심링크만 둔다. `auto_update.sh` 가 pull 다음에 `skills/` 의 각
> 디렉토리를 `~/.claude/skills/` 로 연결하므로, 다른 서버는 clone + `auto_update.sh` 한 번이면
> skill 까지 따라온다.
