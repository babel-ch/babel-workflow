#!/bin/sh
# myflow 자동 업데이트: GitHub(remote)의 최신 상태와 local을 비교해서
# remote에 새 커밋이 있으면 pull 해서 반영한다.
#
# 동작 개요
#   1. `git fetch` 로 remote의 최신 정보만 받아온다 (이 단계에서 코드는 안 바뀜).
#   2. local 커밋과 remote 커밋이 같으면 아무것도 하지 않는다.
#   3. remote에만 새 커밋이 있으면 `git pull --ff-only` 로 반영한다.
#   4. 아래 경우에는 덮어쓰지 않고 경고만 남긴다 (사람이 직접 정리해야 함):
#      - local에 커밋하지 않은 수정 파일이 있을 때
#      - local에만 있는 커밋이 있을 때 (push 필요)
#      - 양쪽이 서로 다른 커밋을 갖고 있을 때 (충돌 가능성)
#
# 사용법
#   ./auto_update.sh            # 1회 확인 후 필요하면 pull
#   ./auto_update.sh --check    # pull 하지 않고 비교 결과만 출력
#
# 첫 줄의 #!/bin/sh 덕분에 사용자가 bash를 쓰든 zsh를 쓰든 상관없이
# 항상 같은 방식으로 실행된다.
#
# 주기 실행 등록 예 (cron, 10분마다):
#   */10 * * * * /path/to/myflow/auto_update.sh >> ~/.myflow_update.log 2>&1

# 이 파일이 있는 폴더 = 저장소 위치. 어느 서버에 어떤 경로로 clone 해도 그대로 동작한다.
REPO_DIR=$(cd "$(dirname "$0")" && pwd)

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# 저장소에서 실행하는 git 명령을 짧게 쓰기 위한 함수
g() {
    git -C "$REPO_DIR" "$@"
}

# 1) remote의 최신 정보 받아오기 (코드는 아직 안 바뀜)
if ! g fetch 2>&1; then
    log "fetch 실패 (네트워크/인증 문제일 수 있음)"
    exit 1
fi

# 2) local 커밋, remote 커밋, 공통 조상 커밋의 ID를 구해서 비교
if ! LOCAL=$(g rev-parse HEAD 2>/dev/null); then
    log "이 저장소에는 아직 커밋이 없습니다. 먼저 commit & push 하세요."
    exit 1
fi
# @{u} = 현재 브랜치가 따라가는 remote 브랜치
if ! REMOTE=$(g rev-parse '@{u}' 2>/dev/null); then
    log "이 브랜치가 따라갈 remote 브랜치가 설정돼 있지 않습니다. 예: git branch --set-upstream-to=origin/main"
    exit 1
fi
BASE=$(g merge-base HEAD '@{u}')

if [ "$LOCAL" = "$REMOTE" ]; then
    log "이미 최신 상태입니다."
    exit 0
fi

if [ "$BASE" = "$REMOTE" ]; then
    log "local에만 새 커밋이 있습니다. push 가 필요합니다. (pull 건너뜀)"
    exit 0
fi

if [ "$BASE" != "$LOCAL" ]; then
    log "local과 remote가 서로 다른 커밋을 갖고 있습니다 (충돌 가능성). 직접 git pull 해서 정리해야 합니다. (자동 pull 건너뜀)"
    exit 0
fi

# 여기 도달 = remote에만 새 커밋이 있는, 안전하게 pull 가능한 상태
if [ "$1" = "--check" ]; then
    log "remote에 새 커밋이 있습니다. (--check 모드라 pull 하지 않음)"
    exit 0
fi

# 3) 커밋 안 한 수정 파일이 있으면 덮어쓰기 사고를 막기 위해 건너뜀
#    (--untracked-files=no: git이 추적하지 않는 새 파일은 pull이 건드리지 않으므로 무시)
DIRTY=$(g status --porcelain --untracked-files=no)
if [ -n "$DIRTY" ]; then
    log "커밋하지 않은 변경이 있어 pull을 건너뜁니다:"
    echo "$DIRTY"
    exit 0
fi

# 4) pull 실행. --ff-only = local 커밋과 합치는 일 없이, 단순히 따라가기만 허용
if g pull --ff-only; then
    NEW=$(g rev-parse HEAD)
    log "업데이트 완료: $(echo "$LOCAL" | cut -c1-8) → $(echo "$NEW" | cut -c1-8)"
else
    log "pull 실패"
    exit 1
fi
