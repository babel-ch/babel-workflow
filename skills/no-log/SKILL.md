---
name: no-log
description: 지금 턴 또는 현재 세션을 Notion 작업 기록에서 제외한다. "/no-log", "이건 notion 기록하지 마", "이번 건 로그 남기지 마", "노션에 안 남겨도 돼", "기록 끄자" 처럼 Notion 기록을 끄라고 할 때 사용한다. "이 세션 끝까지 끄자" 처럼 범위를 넓히거나, "다시 기록해", "로그 다시 켜" 처럼 되돌릴 때도 이 skill 로 처리한다.
---

# no-log

Stop 훅이 매 턴 실행하는 `notion_logger.py` 가 기록을 건너뛰도록 마커 파일을 만든다.
훅 설정(settings.json)이나 다른 세션에는 전혀 영향을 주지 않는다.

## 마커 두 종류

| 범위 | 경로 | 동작 |
|---|---|---|
| 이 턴만 | `~/.claude/notion-log/skip-once/<session_id>` | 훅이 읽는 순간 지워진다. 다음 턴부터 다시 기록 |
| 세션 전체 | `~/.claude/notion-log/skip/<session_id>` | 지울 때까지 계속 꺼져 있다 |

`skip-once` 는 만든 지 1시간이 지나면 소비되지 않고 버려진다. 훅이 실행되지 않은 채
남은 마커가 나중에 엉뚱한 턴을 잡아먹지 않게 하기 위한 것이다. 두 마커 모두 14일이
지나면 로거가 알아서 지운다.

## 어느 쪽을 쓸지 고르기

**기본은 `skip-once` (이 턴만).** 아래에 해당할 때만 `skip` 을 쓴다.

- 세션의 **첫 턴**에서 트리거됐다 → `skip` (세션 전체)
- 사용자가 범위를 명시했다 → 그대로 따른다
  - "이 세션 전체", "지금부터 끝까지", "오늘 이 작업은 계속" → `skip`
  - "이번 것만", "이 턴만" → `skip-once`

첫 턴인지는 지금까지의 대화로 판단한다. 애매하면 transcript 의 user 메시지 수로 확인한다.

```bash
grep -c '"type":"user"' ~/.claude/projects/<프로젝트슬러그>/<session_id>.jsonl
```

## session_id 구하기

시스템 프롬프트의 scratchpad 경로에 세션 UUID 가 들어 있다.

```
/private/tmp/claude-501/<프로젝트슬러그>/<session_id>/scratchpad
                                         ^^^^^^^^^^^^ 이 부분
```

즉 scratchpad 경로에서 `/scratchpad` 를 뗀 마지막 디렉토리 이름이 `session_id` 다.
추측하지 말고 경로에서 그대로 읽을 것.

scratchpad 경로를 알 수 없을 때만 fallback 으로 가장 최근에 수정된 transcript 를 쓴다.

```bash
ls -t ~/.claude/projects/<프로젝트슬러그>/*.jsonl | head -1 | xargs basename | sed 's/\.jsonl$//'
```

## 실행

이 턴만 끄기:

```bash
mkdir -p ~/.claude/notion-log/skip-once && touch ~/.claude/notion-log/skip-once/<session_id>
```

세션 전체 끄기:

```bash
mkdir -p ~/.claude/notion-log/skip && touch ~/.claude/notion-log/skip/<session_id>
```

되돌리기 (세션 전체를 껐던 것을 다시 켜기):

```bash
rm -f ~/.claude/notion-log/skip/<session_id>
```

마커를 만든 **그 턴부터** 적용된다. Stop 훅은 턴이 끝난 뒤 실행되므로, 훅이 돌 때는
마커가 이미 있다.

## 규칙

- **이미 만들어진 Notion 행은 절대 건드리지 않는다.** 마커를 세우기 전 턴들이 남긴
  부모 행·자식 행은 그대로 둔다. 지울지 물어보지도 않는다. 의도된 동작이다.
- 응답은 한 줄로 짧게. **어느 범위를 껐는지**(이 턴만인지 세션 전체인지)는 반드시
  밝힌다. 경로나 명령은 늘어놓지 않는다.
- 사용자가 명시적으로 요청했을 때만 실행한다. "기록할 만한 내용이 아닌 것 같다"는
  식의 자체 판단으로 먼저 끄지 않는다.
