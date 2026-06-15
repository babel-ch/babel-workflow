#!/bin/sh
# myflow 자동 업데이트: GitHub(remote)의 최신 상태와 local을 비교해서
# remote에 새 커밋이 있으면 pull 해서 반영한다.
#
# 동작 개요
#   0. ~/.claude/hooks/notion_logger.py 바로가기(심링크)가 없으면 만들고,
#      다른 곳을 가리키면 이 repo의 notion_logger.py 를 가리키도록 고친다.
#   1. `git fetch` 로 remote의 최신 정보만 받아온다 (이 단계에서 코드는 안 바뀜).
#   2. local 커밋과 remote 커밋이 같으면 아무것도 하지 않는다.
#   3. remote에만 새 커밋이 있으면 `git pull --ff-only` 로 반영한다.
#   4. 아래 경우에는 덮어쓰지 않고 경고만 남긴다 (사람이 직접 정리해야 함):
#      - local에 커밋하지 않은 수정 파일이 있을 때
#      - local에만 있는 커밋이 있을 때 (push 필요)
#      - 양쪽이 서로 다른 커밋을 갖고 있을 때 (충돌 가능성)
#   5. ~/.claude/settings.json 의 Stop 훅을 이 repo의 stop_hooks.json 과 비교해서
#      다르면 맞춰준다. 즉 stop_hooks.json 이 "Stop 훅의 최신 상태" 원본이다.
#      (pull 다음에 실행되므로, repo가 갱신되면 같은 실행에서 훅 설정도 따라온다)
#
# 사용법
#   ./auto_update.sh            # 1회 확인 후 필요하면 pull + Stop 훅 동기화
#   ./auto_update.sh --check    # pull/동기화 하지 않고 비교 결과만 출력
#
# 첫 줄의 #!/bin/sh 덕분에 사용자가 bash를 쓰든 zsh를 쓰든 상관없이
# 항상 같은 방식으로 실행된다.
#
# 주기 실행 등록 예 (cron, 10분마다):
#   */10 * * * * /path/to/myflow/auto_update.sh >> ~/.myflow_update.log 2>&1

# 이 파일이 있는 폴더 = 저장소 위치. 어느 서버에 어떤 경로로 clone 해도 그대로 동작한다.
REPO_DIR=$(cd "$(dirname "$0")" && pwd)
MODE="$1"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# 저장소에서 실행하는 git 명령을 짧게 쓰기 위한 함수
g() {
    git -C "$REPO_DIR" "$@"
}

# 0) ~/.claude/hooks/notion_logger.py 바로가기(심링크) 확인.
#    없으면 만들고, 다른 곳을 가리키면 이 repo를 가리키도록 고친다.
#    바로가기는 한 번 만들어두면 repo 파일이 pull로 바뀔 때 자동으로 새 내용을 보게 되므로,
#    이 단계만 통과하면 훅 스크립트 업데이트는 따로 할 일이 없다.
HOOK_LINK="$HOME/.claude/hooks/notion_logger.py"
HOOK_TARGET="$REPO_DIR/notion_logger.py"

if [ -L "$HOOK_LINK" ]; then
    # 이미 바로가기가 있음 — 가리키는 곳이 이 repo가 맞는지만 확인
    CURRENT=$(readlink "$HOOK_LINK")
    if [ "$CURRENT" != "$HOOK_TARGET" ]; then
        ln -sf "$HOOK_TARGET" "$HOOK_LINK"
        log "바로가기가 다른 곳($CURRENT)을 가리키고 있어 다시 연결했습니다: $HOOK_LINK → $HOOK_TARGET"
    fi
elif [ -e "$HOOK_LINK" ]; then
    # 바로가기가 아니라 진짜 파일이 그 자리에 있음 — 지우면 그 안의 수정 내용이 사라질 수
    # 있으므로 자동으로 덮어쓰지 않고 알리기만 한다 (사람이 직접 확인해야 함)
    log "경고: $HOOK_LINK 가 바로가기가 아니라 일반 파일입니다. 덮어쓰지 않았으니 직접 확인하세요."
else
    mkdir -p "$HOME/.claude/hooks"
    ln -s "$HOOK_TARGET" "$HOOK_LINK"
    log "바로가기 생성: $HOOK_LINK → $HOOK_TARGET"
fi

# 1~4) git 갱신 로직.
#      어떤 경우로 끝나든 마지막의 Stop 훅 동기화(5)는 항상 실행돼야 하므로,
#      중간에 exit 하지 않도록 함수로 묶고 return 으로 빠져나온다.
update_repo() {
    # 1) remote의 최신 정보 받아오기 (코드는 아직 안 바뀜)
    if ! g fetch 2>&1; then
        log "fetch 실패 (네트워크/인증 문제일 수 있음)"
        return 1
    fi

    # 2) local 커밋, remote 커밋, 공통 조상 커밋의 ID를 구해서 비교
    if ! LOCAL=$(g rev-parse HEAD 2>/dev/null); then
        log "이 저장소에는 아직 커밋이 없습니다. 먼저 commit & push 하세요."
        return 1
    fi
    # @{u} = 현재 브랜치가 따라가는 remote 브랜치
    if ! REMOTE=$(g rev-parse '@{u}' 2>/dev/null); then
        log "이 브랜치가 따라갈 remote 브랜치가 설정돼 있지 않습니다. 예: git branch --set-upstream-to=origin/main"
        return 1
    fi
    BASE=$(g merge-base HEAD '@{u}')

    if [ "$LOCAL" = "$REMOTE" ]; then
        log "이미 최신 상태입니다."
        return 0
    fi

    if [ "$BASE" = "$REMOTE" ]; then
        log "local에만 새 커밋이 있습니다. push 가 필요합니다. (pull 건너뜀)"
        return 0
    fi

    if [ "$BASE" != "$LOCAL" ]; then
        log "local과 remote가 서로 다른 커밋을 갖고 있습니다 (충돌 가능성). 직접 git pull 해서 정리해야 합니다. (자동 pull 건너뜀)"
        return 0
    fi

    # 여기 도달 = remote에만 새 커밋이 있는, 안전하게 pull 가능한 상태
    if [ "$MODE" = "--check" ]; then
        log "remote에 새 커밋이 있습니다. (--check 모드라 pull 하지 않음)"
        return 0
    fi

    # 3) 커밋 안 한 수정 파일이 있으면 덮어쓰기 사고를 막기 위해 건너뜀
    #    (--untracked-files=no: git이 추적하지 않는 새 파일은 pull이 건드리지 않으므로 무시)
    DIRTY=$(g status --porcelain --untracked-files=no)
    if [ -n "$DIRTY" ]; then
        log "커밋하지 않은 변경이 있어 pull을 건너뜁니다:"
        echo "$DIRTY"
        return 0
    fi

    # 4) pull 실행. --ff-only = local 커밋과 합치는 일 없이, 단순히 따라가기만 허용
    if g pull --ff-only; then
        NEW=$(g rev-parse HEAD)
        log "업데이트 완료: $(echo "$LOCAL" | cut -c1-8) → $(echo "$NEW" | cut -c1-8)"
    else
        log "pull 실패"
        return 1
    fi
}

# 4.5) ~/.claude/skills/<name> 바로가기(심링크) 동기화.
#       repo의 skills/ 아래 각 skill 디렉토리를 ~/.claude/skills/ 에 심링크로 노출한다.
#       notion_logger 심링크(0단계)와 달리 pull 다음에 실행한다 — 새 skill 이 이번
#       pull 로 처음 들어오는 경우에도 같은 실행에서 바로가기가 걸리게 하기 위함이다.
#       (한 번 걸어두면 repo 파일이 pull 로 바뀔 때 자동으로 새 내용을 본다.)
sync_skills() {
    SKILLS_SRC="$REPO_DIR/skills"
    SKILLS_DST="$HOME/.claude/skills"

    [ -d "$SKILLS_SRC" ] || return 0  # repo에 skills/ 가 없으면 할 일 없음
    mkdir -p "$SKILLS_DST"

    for skill_path in "$SKILLS_SRC"/*/; do
        [ -d "$skill_path" ] || continue          # skills/ 가 비어 있으면 패턴이 그대로 남음
        target="${skill_path%/}"                  # 끝의 / 제거
        name=$(basename "$target")
        link="$SKILLS_DST/$name"

        if [ -L "$link" ]; then
            CURRENT=$(readlink "$link")
            if [ "$CURRENT" != "$target" ]; then
                ln -sf "$target" "$link"
                log "skill 바로가기 재연결: $link → $target"
            fi
        elif [ -e "$link" ]; then
            log "경고: $link 가 바로가기가 아니라 일반 파일/폴더입니다. 덮어쓰지 않았으니 직접 확인하세요."
        else
            ln -s "$target" "$link"
            log "skill 바로가기 생성: $link → $target"
        fi
    done
}

# 5) ~/.claude/settings.json 의 Stop 훅 동기화.
#    repo의 stop_hooks.json 이 원하는 상태(원본)이고, 파일 안의 {{REPO_DIR}} 는
#    적용하는 순간 실제 repo 경로로 바꿔 넣는다. settings.json 의 다른 설정
#    (다른 훅, 권한 등)은 건드리지 않고 hooks.Stop 항목만 교체한다.
sync_stop_hook() {
    SETTINGS="$HOME/.claude/settings.json"
    SPEC="$REPO_DIR/stop_hooks.json"

    # jq = JSON을 안전하게 읽고 고치는 명령줄 도구. 없으면 손대지 않고 건너뛴다.
    if ! command -v jq >/dev/null 2>&1; then
        log "경고: jq 가 없어 Stop 훅 동기화를 건너뜁니다. (mac: brew install jq / linux: apt install jq)"
        return 0
    fi
    if [ ! -f "$SPEC" ]; then
        log "경고: $SPEC 이 없어 Stop 훅 동기화를 건너뜁니다."
        return 0
    fi

    # {{REPO_DIR}} 자리표시자를 실제 경로로 바꿔서 "원하는 Stop 훅 상태"를 만든다
    DESIRED=$(sed "s|{{REPO_DIR}}|$REPO_DIR|g" "$SPEC")
    if ! printf '%s' "$DESIRED" | jq -e . >/dev/null 2>&1; then
        log "경고: stop_hooks.json 이 올바른 JSON이 아니어서 동기화를 건너뜁니다."
        return 0
    fi

    # settings.json 이 아직 없는 새 머신이면 빈 설정으로 시작한다
    if [ ! -f "$SETTINGS" ]; then
        mkdir -p "$HOME/.claude"
        echo '{}' > "$SETTINGS"
        log "$SETTINGS 가 없어 새로 만들었습니다."
    fi

    # 현재 상태와 비교 (jq -S = 키 순서를 정렬해서, 순서 차이는 무시하고 내용만 비교)
    CURRENT_STOP=$(jq -S '.hooks.Stop' "$SETTINGS" 2>/dev/null)
    WANT_STOP=$(printf '%s' "$DESIRED" | jq -S .)
    if [ "$CURRENT_STOP" = "$WANT_STOP" ]; then
        return 0  # 이미 최신 상태 — 조용히 통과
    fi

    if [ "$MODE" = "--check" ]; then
        log "Stop 훅이 stop_hooks.json 과 다릅니다. (--check 모드라 고치지 않음)"
        return 0
    fi

    # 백업을 남기고, 임시 파일에 완성본을 만든 뒤 통째로 교체한다
    # (쓰는 도중 끊겨도 원본 settings.json 이 깨지지 않게 하기 위함)
    cp "$SETTINGS" "$SETTINGS.bak.myflow"
    TMP="$SETTINGS.tmp.$$"
    if jq --argjson stop "$DESIRED" '.hooks.Stop = $stop' "$SETTINGS" > "$TMP" 2>/dev/null \
        && jq -e . "$TMP" >/dev/null 2>&1; then
        mv "$TMP" "$SETTINGS"
        log "Stop 훅을 stop_hooks.json 내용으로 갱신했습니다. (이전 설정 백업: $SETTINGS.bak.myflow)"
    else
        rm -f "$TMP"
        log "Stop 훅 갱신 실패 (settings.json 이 올바른 JSON인지 확인하세요). settings.json 은 바꾸지 않았습니다."
    fi
}

update_repo
UPDATE_RC=$?
sync_skills
sync_stop_hook
exit $UPDATE_RC
