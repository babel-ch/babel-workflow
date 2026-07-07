#!/usr/bin/env python3
"""
Claude Code Stop hook: 세션 단위로 작업 내용을 Notion 데이터베이스에 기록한다.

구조
  - 세션 1개 = 부모 행 1개 + 그 아래 날짜별 자식 행(sub-item).
      부모 행: 세션 전체 제목(작업) + 세션ID. 날짜는 비워 둔다.
      자식 행: 날짜 = 그날, Parent item = 부모. 그날의 턴 bullet 들이 본문에 쌓인다.
    날짜별 view 는 자식 행으로, 프로젝트별 view 는 부모 토글로 보는 구조.
  - 제목(작업)은 세션 전체를 아우르는 짧은 한 줄로 턴마다 갱신 (부모·자식 동일).
  - 턴별 상세는 자식 행 본문에 "[HH:MM] 요약" bullet 로 append 된다. 요약은
    명령·파일명 나열이 아니라 작업의 의도·역할 중심이고, 그 bullet 아래에 접힌
    '파일·명령' 토글로 실제 도구 호출(편집/생성한 파일·명령)을 함께 남긴다.
    (안전망: 행의 마지막 기록일과 오늘이 다르면 bullet 앞에 날짜 heading 을 넣는다)
  - 세션 ↔ 행 매핑: 로컬 캐시(session_map.json) → DB 의 세션ID 속성 조회 → 새 행 생성.
    캐시 값은 {"parent_id", "days": {날짜: child_id}} dict.
    구버전 캐시 값(문자열, {"page_id"} dict)과 구버전 평면 행은 무시하고 DB 조회로 처리.

동작 개요
  1. Stop 훅으로 stdin JSON(session_id, transcript_path, cwd 등)을 받는다.
  2. 무한 재귀 가드: 요약용 `claude -p` 호출이 다시 Stop 훅을 트리거하므로
     CLAUDE_NOTION_LOGGER_SKIP 환경변수가 있으면 즉시 종료한다.
     자정 가드: 23:50~00:10 에는 날짜 귀속이 틀어질 수 있어 기록하지 않는다.
  3. 턴을 블로킹하지 않도록 실제 작업(요약+전송)은 백그라운드 워커로 분리하고
     훅 본체는 곧바로 exit 0 한다.
  4. 워커: transcript에서 마지막 턴의 사용자 프롬프트 + 응답 + 도구 호출 목록
     (Bash 명령, 수정한 파일 등)을 뽑아 `claude -p`로 세션 제목 + 턴 요약(JSON)을
     만든 뒤 Notion에 기록. 요약은 도구 호출 내역을 사실 확인 근거로만 쓰고,
     명령·파일명 나열이 아니라 작업의 의도·역할 중심으로 작성하게 한다.
  5. Notion 기록 실패 시 로컬 파일(~/.claude/notion-log/)에 fallback 기록.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────
NOTION_TOKEN_PATH = Path.home() / ".claude" / "notion_token.txt"
if NOTION_TOKEN_PATH.is_file():
    NOTION_TOKEN = NOTION_TOKEN_PATH.read_text().strip()
else:
    NOTION_TOKEN = ""
NOTION_DATABASE_ID = "37cf979eb42380fdadf4fcf02ca704f1"  # 행을 추가할 데이터베이스 ID

NOTION_VERSION = "2022-06-28"
LOCAL_LOG_DIR = Path.home() / ".claude" / "notion-log"
SESSION_MAP_PATH = LOCAL_LOG_DIR / "session_map.json"  # session_id -> notion page_id
# claude 바이너리 탐색: PATH 우선, 흔한 설치 경로 fallback (맥/리눅스 공용)
CLAUDE_BIN = (
    shutil.which("claude")
    or next(
        (p for p in (
            os.path.expanduser("~/.local/bin/claude"),
            os.path.expanduser("~/.claude/local/claude"),
            "/usr/local/bin/claude",
        ) if os.path.exists(p)),
        "claude",
    )
)
SKIP_ENV = "CLAUDE_NOTION_LOGGER_SKIP"


# ─────────────────────────────────────────────────────────────
# Notion API 공통
# ─────────────────────────────────────────────────────────────
def notion_request(method, path, payload=None):
    """Notion API 호출. 성공 시 응답 dict, 실패 시 None."""
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None


# ─────────────────────────────────────────────────────────────
# transcript 파싱
# ─────────────────────────────────────────────────────────────
# 도구 호출 한 줄 요약에 쓸 input 필드 (앞에 있는 키부터 먼저 매칭)
TOOL_INPUT_KEYS = ("command", "file_path", "notebook_path", "pattern",
                   "query", "url", "prompt", "skill", "description")


def describe_tool_use(block):
    """tool_use 블록을 'Bash: git status' 같은 한 줄로. 없으면 도구 이름만."""
    name = block.get("name", "")
    if not name:
        return ""
    inp = block.get("input") or {}
    for key in TOOL_INPUT_KEYS:
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            detail = " ".join(val.split())  # 개행·연속 공백을 한 칸으로
            return f"{name}: {detail[:200]}"
    return name


# ─────────────────────────────────────────────────────────────
# '작업한 파일·명령' 기록 필터: 시스템에 변화를 준 것만 남긴다
# ─────────────────────────────────────────────────────────────
# 읽기/탐색/조회·오케스트레이션 전용 (항상 제외)
READONLY_TOOLS = {
    "Read", "NotebookRead", "Grep", "Glob", "LS",
    "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "Skill",
}
# 파일을 바꾸는 도구 (항상 기록)
FILE_MUTATING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# 기타(MCP 등) 도구 이름에 이 동사 토큰이 있으면 '변화'로 보고 기록
MUTATING_VERBS = {
    "create", "save", "update", "delete", "insert", "write", "append",
    "remove", "move", "rename", "patch", "upload", "put", "post", "set",
    "add", "edit", "apply", "register", "unregister", "duplicate",
}
# bash 에서 (대체로) 읽기/파싱 전용으로 보는 명령
READONLY_BASH = {
    "grep", "rg", "egrep", "fgrep", "ag", "ls", "cat", "bat", "head", "tail",
    "less", "more", "find", "fd", "pwd", "which", "whereis", "type", "echo",
    "printf", "wc", "sort", "uniq", "diff", "cmp", "file", "stat", "du", "df",
    "ps", "env", "printenv", "date", "tree", "jq", "yq", "cut", "tr", "column",
    "basename", "dirname", "realpath", "readlink", "test", "man", "cloc",
    "tac", "nl", "awk", "sed", "xxd", "od", "comm", "true", "false",
}
# 읽기 전용으로 보는 git 하위명령
READONLY_GIT_SUB = {
    "status", "log", "diff", "show", "ls-files", "blame", "rev-parse",
    "remote", "describe", "shortlog", "reflog", "cat-file", "grep", "branch",
}
# 실행은 하지만 소스/시스템을 바꾸지 않는 검증·빌드 명령 (읽기로 취급해 제외).
# `python x.py` 같은 일반 스크립트 실행은 여기 없으므로 그대로 기록된다.
READONLY_EXEC = {"pytest", "py.test", "make", "gmake"}
# `python -m <이것>` 형태도 검증/컴파일이라 읽기로 취급 (py_compile, pytest)
READONLY_PY_MODULES = {"pytest", "py_compile"}
# 실제 명령 앞에 붙는 러너/래퍼 — 벗겨내고 그 뒤를 본다 (uv run, poetry run, time …)
RUN_WRAPPERS = {"uv", "uvx", "poetry", "pdm", "hatch", "pipenv", "rye",
                "time", "nice", "sudo", "env"}


def _segment_is_readonly(seg):
    """파이프/체인으로 나눈 한 구간이 읽기/검증 전용인지.
    앞쪽 env 할당과 러너 래퍼(uv run 등)를 벗겨낸 뒤 실질 명령으로 판정한다."""
    toks = seg.split()
    i = 0
    while i < len(toks):
        t = toks[i]
        if "=" in t and not t.startswith("-"):      # FOO=bar 환경변수 할당
            i += 1
            continue
        if t in RUN_WRAPPERS:                         # uv / poetry / time / sudo …
            i += 1
            if i < len(toks) and toks[i] == "run":    # uv run / poetry run
                i += 1
            continue
        break
    rest = toks[i:]
    if not rest:
        return True
    cmd = rest[0]
    if cmd == "git":
        return len(rest) > 1 and rest[1] in READONLY_GIT_SUB
    if cmd == "sed":
        return not any(t == "-i" or t.startswith("-i") for t in rest[1:])  # sed -i 는 변화
    if cmd in READONLY_EXEC:
        return True
    if cmd in ("python", "python3", "py"):
        # python x.py 같은 일반 실행은 기록. 단 -m pytest / -m py_compile 은 검증 → 제외
        if "-m" in rest:
            mi = rest.index("-m")
            if mi + 1 < len(rest) and rest[mi + 1] in READONLY_PY_MODULES:
                return True
        return False
    return cmd in READONLY_BASH


def _bash_is_readonly(cmd):
    """bash 명령이 시스템 변화 없이 읽기/파싱만 하는지 (휴리스틱).
    파일로의 출력 리다이렉트(>, >>)·tee 가 있으면 변화로 본다(2>&1 같은 fd 복제는 제외).
    모든 파이프/체인 구간이 읽기 전용 명령이면 읽기 전용으로 본다."""
    if not cmd.strip():
        return True
    if re.search(r">>?(?!&)", cmd) or re.search(r"\btee\b", cmd):
        return False
    segs = re.split(r"\|\||&&|[|;]", cmd)
    return all(_segment_is_readonly(s) for s in segs if s.strip())


def is_recordable_action(block):
    """이 tool_use 가 '작업한 파일·명령' 으로 남길 만한 시스템 변화인지.
    읽기/탐색/조회는 빼고, 파일 변경·변화를 일으킨 명령·외부 변경만 기록한다."""
    name = block.get("name", "")
    if name in FILE_MUTATING_TOOLS:
        return True
    if name in READONLY_TOOLS:
        return False
    if name == "Bash":
        return not _bash_is_readonly((block.get("input") or {}).get("command", ""))
    # 기타(MCP 등): 이름 토큰에 변경 동사가 있을 때만 (list/get/fetch 류는 제외)
    return bool(set(re.split(r"[^a-z]+", name.lower())) & MUTATING_VERBS)


def is_user_prompt(obj):
    """user 엔트리가 실제 사용자 입력인지 (tool_result 가 아닌지)."""
    content = obj.get("message", {}).get("content", "")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(c, dict) and c.get("type") == "text" for c in content)
    return False


def extract_turn(transcript_path):
    """transcript JSONL에서 마지막 턴의
    (사용자 프롬프트, 마지막 응답 텍스트, 도구 호출 목록)을 뽑는다.
    실제 사용자 입력(user 텍스트 메시지)을 만날 때마다 턴이 새로 시작된 것으로 보고
    이전 턴의 응답·도구 기록은 버린다."""
    last_prompt = ""
    user_text = ""  # last-prompt 엔트리가 없을 때의 fallback
    last_assistant = ""
    actions = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return last_prompt, last_assistant, actions

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = obj.get("type")
        if t == "last-prompt":
            # 실제 사용자가 입력한 프롬프트가 그대로 저장됨
            last_prompt = obj.get("lastPrompt", "") or last_prompt
        elif t == "user":
            if is_user_prompt(obj):
                actions = []
                last_assistant = ""
                content = obj.get("message", {}).get("content", "")
                if isinstance(content, str):
                    user_text = content
                else:
                    user_text = "\n".join(
                        c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
        elif t == "assistant":
            content = obj.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            texts = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text"):
                    texts.append(c["text"])
                elif c.get("type") == "tool_use":
                    if not is_recordable_action(c):
                        continue  # 읽기/탐색(Read·grep·ls 등)은 기록에서 제외
                    desc = describe_tool_use(c)
                    if desc and (not actions or actions[-1] != desc):
                        actions.append(desc)
            joined = "\n".join(texts)
            if joined:
                last_assistant = joined

    return last_prompt or user_text, last_assistant, actions


# ─────────────────────────────────────────────────────────────
# 요약 (세션 제목 + 턴 요약을 JSON 으로 한 번에)
# ─────────────────────────────────────────────────────────────
MAX_ACTIONS_IN_PROMPT = 30
MAX_ACTIONS_IN_LOG = 50  # 턴 bullet 아래 '파일·명령' 토글에 남길 도구 호출 최대 개수
                         # (코드 블록 한 개에 개행으로 모아 담으므로 블록 수와 무관.
                         #  목록이 지나치게 길어지는 것만 막는 상한)


def summarize(project, prompt, response, actions, current_title):
    """claude -p 로 세션 제목과 턴 요약을 생성. (title, turn) 반환."""
    title_part = (
        f"기존 세션 제목: {current_title}\n"
        "새 턴 내용을 반영해 세션 전체를 아우르도록 제목을 갱신하라. "
        "기존 제목이 여전히 적절하면 그대로 둬도 된다."
        if current_title else
        "이 세션의 첫 턴이다. 세션 제목을 새로 지어라."
    )
    actions_part = ""
    if actions:
        shown = actions[:MAX_ACTIONS_IN_PROMPT]
        listed = "\n".join(f"- {a}" for a in shown)
        if len(actions) > MAX_ACTIONS_IN_PROMPT:
            listed += f"\n- … 외 {len(actions) - MAX_ACTIONS_IN_PROMPT}건"
        actions_part = f"[이 턴에서 실행한 도구 호출]\n{listed}\n\n"
    instruction = (
        "아래는 Claude Code 세션의 한 턴이다. 다음 JSON 형식으로만 답하라. "
        "코드블록 없이 순수 JSON 한 줄만 출력하라.\n"
        '{"title": "세션 전체를 아우르는 짧은 제목 (15자 내외)", '
        '"turn": "이 턴에서 사용자가 무엇을 원했고 그래서 무엇이 이뤄졌는지, '
        '나중에 사람이 읽고 바로 이해되도록 담은 한두 문장"}\n\n'
        f"프로젝트: {project}\n{title_part}\n"
        "turn 요약은 '코드를 모르는 사람이 읽어도 무슨 작업이었는지 알 수 있는' "
        "한두 문장이어야 한다. 다음을 지켜라.\n"
        "(1) 사용자가 어떤 의도·이유로 무엇을 요청했는지 짧게 담아라. "
        "프롬프트에 배경이 드러나면 함께 적는다.\n"
        "(2) 그 결과 실제로 무엇이 이뤄졌는지를, 그 작업의 '역할·목적·효과' 중심으로 "
        "풀어 써라. 아래 도구 호출 목록은 실제로 무슨 일이 일어났는지 확인하는 "
        "근거로만 쓰고, 결과 문장에는 파일 경로·함수명·명령어·도구 이름을 "
        "그대로 나열하지 마라. 대신 그 파일·명령이 맡은 역할로 바꿔 표현하라.\n"
        "  예) 나쁨: 'auth.py의 login()을 고치고 config.yaml에 timeout 추가, npm test 실행'\n"
        "      좋음: '로그인이 일정 시간 뒤 자동으로 풀리도록 세션 만료 처리를 더하고 "
        "동작을 테스트로 확인함'\n"
        "조사·탐색에 그친 호출(Read, ls, grep 등)은 빼고 결과를 바꾼 작업 위주로 적어라. "
        "Claude 응답의 표현을 그대로 옮기지 마라.\n"
        "반드시 한국어로만 작성하라. 영어·일본어 등 다른 언어 단어를 섞지 마라. "
        "번역이 어색한 고유명사만 원문 그대로 둔다. "
        "군더더기 표현 없이 작업 내용만 적어라.\n\n"
        f"[사용자 요청]\n{prompt[:3000]}\n\n"
        f"{actions_part}"
        f"[Claude 응답]\n{response[:4000]}"
    )
    env = dict(os.environ)
    env[SKIP_ENV] = "1"  # 이 claude 호출의 Stop 훅 재귀 방지
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "--no-session-persistence", instruction],
            capture_output=True, text=True, timeout=120, env=env,
        )
        out = result.stdout.strip()
        # 모델이 코드블록으로 감쌌을 경우 벗겨냄
        if out.startswith("```"):
            out = out.strip("`")
            out = out[out.find("{"):]
        start, end = out.find("{"), out.rfind("}")
        parsed = json.loads(out[start:end + 1])
        title = str(parsed.get("title", "")).strip()
        turn = str(parsed.get("turn", "")).strip()
        if turn:
            return title or current_title or f"{project} 작업", turn
    except Exception:
        pass
    return current_title or f"{project} 작업", "(요약 실패)"


# ─────────────────────────────────────────────────────────────
# 세션 ↔ Notion 행 매핑
# ─────────────────────────────────────────────────────────────
def load_session_map():
    try:
        return json.loads(SESSION_MAP_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_session_map(mapping):
    LOCAL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_MAP_PATH.write_text(json.dumps(mapping, indent=2))


def get_row_title(page_id):
    """행의 현재 제목을 읽는다. 실패 시 빈 문자열."""
    resp = notion_request("GET", f"/pages/{page_id}")
    try:
        return "".join(
            t["plain_text"] for t in resp["properties"]["작업"]["title"]
        )
    except (TypeError, KeyError):
        return ""


def to_local_date(iso_ts):
    """Notion의 UTC ISO 타임스탬프를 로컬 YYYY-MM-DD 로. 실패 시 None."""
    try:
        return (datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
                .astimezone().strftime("%Y-%m-%d"))
    except (ValueError, AttributeError):
        return None


def get_last_log_date(page_id):
    """행 본문 마지막 블록이 기록된 로컬 날짜.
    본문이 비어 있으면 행의 날짜 속성, 그것도 없으면 None."""
    last_created = None
    cursor = None
    while True:
        path = f"/blocks/{page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        resp = notion_request("GET", path)
        if not resp or resp.get("object") == "error":
            return None
        if resp.get("results"):
            last_created = resp["results"][-1].get("created_time")
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    if last_created:
        return to_local_date(last_created)
    page = notion_request("GET", f"/pages/{page_id}")
    try:
        return page["properties"]["날짜"]["date"]["start"]
    except (TypeError, KeyError):
        return None


PARENT_REL = "Parent item"  # sub-item 기능이 만든 부모 참조 relation 속성 이름


def query_one(filt):
    """DB 에서 필터에 맞는 첫 행. 없거나 실패 시 None."""
    resp = notion_request("POST", f"/databases/{NOTION_DATABASE_ID}/query", {
        "filter": filt, "page_size": 1,
    })
    if resp and resp.get("results"):
        return resp["results"][0]
    return None


def row_title(row):
    try:
        return "".join(t["plain_text"] for t in row["properties"]["작업"]["title"])
    except (TypeError, KeyError):
        return ""


def create_row(project, title, session_id, date=None, parent_id=None):
    """DB 행 생성. 성공 시 page_id, 실패 시 None."""
    props = {
        "작업": {"title": [{"type": "text", "text": {"content": title[:2000]}}]},
        "프로젝트": {"select": {"name": project[:100]}},
        "세션ID": {"rich_text": [{"type": "text", "text": {"content": session_id}}]},
    }
    if date:
        props["날짜"] = {"date": {"start": date}}
    if parent_id:
        props[PARENT_REL] = {"relation": [{"id": parent_id}]}
    resp = notion_request("POST", "/pages", {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": props,
    })
    if not resp or resp.get("object") == "error":
        return None
    return resp["id"]


def find_or_create_day_row(session_id, project):
    """오늘 날짜의 자식 행(턴 기록 대상)을 찾거나 만든다.
    (child_id, parent_id, current_title, last_date) 반환. 실패 시 child_id=None.
    last_date 는 자식 행의 마지막 기록일. 모르면 None (record_turn 이 직접 확인)."""
    mapping = load_session_map()
    today = datetime.now().strftime("%Y-%m-%d")

    entry = mapping.get(session_id)
    if isinstance(entry, dict) and ("parent_id" in entry or "days" in entry):
        parent_id = entry.get("parent_id")
        days = dict(entry.get("days") or {})
    else:
        # 구버전 캐시(문자열·{"page_id"})는 평면 행을 가리키므로 무시 → DB 조회로
        parent_id, days = None, {}

    # 1) 로컬 캐시: 오늘 자식 행
    child_id = days.get(today)
    if child_id:
        return child_id, parent_id, get_row_title(parent_id or child_id), today

    # 2) DB 에서 오늘 자식 행 조회 (캐시 유실 대비. 구버전 평면 행도 생성 당일이면 잡힘)
    row = query_one({"and": [
        {"property": "세션ID", "rich_text": {"equals": session_id}},
        {"property": "날짜", "date": {"equals": today}},
    ]})
    if row:
        rel = (row["properties"].get(PARENT_REL) or {}).get("relation") or []
        parent_id = parent_id or (rel[0]["id"] if rel else None)
        title = get_row_title(parent_id) if parent_id else row_title(row)
        mapping[session_id] = {"parent_id": parent_id, "days": {**days, today: row["id"]}}
        save_session_map(mapping)
        return row["id"], parent_id, title, None

    # 3) 부모 행 확보: 캐시 → DB(세션ID 일치 + 날짜 비어 있는 행) → 새로 생성
    current_title = ""
    if parent_id:
        current_title = get_row_title(parent_id)
    else:
        prow = query_one({"and": [
            {"property": "세션ID", "rich_text": {"equals": session_id}},
            {"property": "날짜", "date": {"is_empty": True}},
        ]})
        if prow:
            parent_id, current_title = prow["id"], row_title(prow)
    if not parent_id:
        parent_id = create_row(project, f"{project} 작업", session_id)
        if not parent_id:
            return None, None, "", None

    # 4) 오늘 자식 행 생성
    child_id = create_row(project, current_title or f"{project} 작업",
                          session_id, date=today, parent_id=parent_id)
    if not child_id:
        return None, parent_id, current_title, None
    mapping[session_id] = {"parent_id": parent_id, "days": {**days, today: child_id}}
    save_session_map(mapping)
    return child_id, parent_id, current_title, today


def update_title(page_id, title):
    """행 제목 갱신. 성공 여부 반환."""
    return bool(notion_request("PATCH", f"/pages/{page_id}", {
        "properties": {
            "작업": {"title": [{"type": "text", "text": {"content": title[:2000]}}]},
        },
    }))


def _rich_text_chunks(text, limit=2000):
    """긴 문자열을 Notion rich_text 배열로 나눈다(각 조각의 content 는 ≤ limit).
    한 블록 안의 rich_text 여러 조각은 이어져 보이므로, 블록 수는 늘리지 않고
    2000자 한도만 우회한다."""
    if not text:
        return [{"type": "text", "text": {"content": ""}}]
    return [{"type": "text", "text": {"content": text[i:i + limit]}}
            for i in range(0, len(text), limit)]


def _actions_code_block(actions):
    """도구 호출 목록을 toggle 안에 넣을 '코드 블록 하나'로 만든다.
    예전엔 호출마다 bullet(별도 블록)을 만들었는데, 블록 수가 불어나면 한 PATCH 의
    블록 한도(100)에 가까워지고 페이지도 무거워진다. 여기선 개행으로 한 블록에 모아
    담아 항상 블록 1개로 유지한다. 상한을 넘으면 끝에 '… 외 N건' 을 덧붙인다."""
    shown = actions[:MAX_ACTIONS_IN_LOG]
    text = "\n".join(shown)
    extra = len(actions) - MAX_ACTIONS_IN_LOG
    if extra > 0:
        text += f"\n… 외 {extra}건"
    return {
        "object": "block",
        "type": "code",
        "code": {
            "language": "plain text",
            "rich_text": _rich_text_chunks(text),
        },
    }


def record_turn(page_id, title, turn_summary, actions=None, last_date=None):
    """행 제목 갱신 + 본문에 턴 bullet append. 성공 여부 반환.
    - 마지막 기록일(last_date)과 오늘이 다르면 bullet 앞에 날짜 heading 을 먼저 넣는다.
    - actions(이 턴의 도구 호출)이 있으면 턴 bullet 아래에 접힌 '파일·명령' 토글로
      매단다. 사람이 읽는 요약 bullet 은 그대로 두고 raw 근거(편집/생성한 파일·명령)는
      펼쳐야 보이게 해, 화면은 깔끔하되 나중에(또는 LLM이) 정확히 되짚을 수 있게 한다.
      토글은 bullet 의 자식이라 daily_summary 의 최상위-bullet 수집에는 섞이지 않는다."""
    ok_title = update_title(page_id, title)
    today = datetime.now().strftime("%Y-%m-%d")
    if last_date is None:
        last_date = get_last_log_date(page_id) or today
    now = datetime.now().strftime("%H:%M")

    # 1) (날짜 바뀌었으면 날짜 heading +) 턴 요약 bullet 을 행 본문 끝에 추가
    children = []
    if last_date != today:
        children.append({
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": f"📅 {today}"}}]
            },
        })
    children.append({
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [{
                "type": "text",
                "text": {"content": f"[{now}] {turn_summary}"[:2000]},
            }]
        },
    })
    resp = notion_request("PATCH", f"/blocks/{page_id}/children", {"children": children})
    if not resp or resp.get("object") == "error":
        return False

    # 2) 방금 만든 턴 bullet 아래에 접힌 '파일·명령' 토글을 자식으로 매단다.
    #    (토글 추가가 실패해도 턴 요약은 이미 기록됐으므로 전체는 성공으로 본다)
    if actions:
        turn_block_id = (resp.get("results") or [{}])[-1].get("id")
        if turn_block_id:
            notion_request("PATCH", f"/blocks/{turn_block_id}/children", {"children": [{
                "object": "block",
                "type": "toggle",
                "toggle": {
                    "rich_text": [{"type": "text",
                                   "text": {"content": f"🔧 파일·명령 {len(actions)}건"}}],
                    "children": [_actions_code_block(actions)],
                },
            }]})

    return bool(ok_title)


def log_local(project, summary):
    """로컬 fallback 기록 (날짜별 마크다운)."""
    LOCAL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%H:%M")
    path = LOCAL_LOG_DIR / f"{day}.md"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- [{now}] **{project}** — {summary}\n")


# ─────────────────────────────────────────────────────────────
# 워커 / 엔트리
# ─────────────────────────────────────────────────────────────
def run_worker(data):
    transcript_path = data.get("transcript_path", "")
    cwd = data.get("cwd", "") or os.getcwd()
    project = os.path.basename(cwd.rstrip("/")) or cwd
    session_id = data.get("session_id", "")

    prompt, response, actions = extract_turn(transcript_path)
    if not prompt and not response:
        return  # 기록할 게 없음

    if not (NOTION_TOKEN and NOTION_DATABASE_ID and session_id):
        _, turn = summarize(project, prompt, response, actions, "")
        log_local(project, turn)
        return

    child_id, parent_id, current_title, last_date = find_or_create_day_row(session_id, project)
    title, turn = summarize(project, prompt, response, actions, current_title)

    if child_id is None or not record_turn(child_id, title, turn, actions, last_date):
        log_local(project, turn)
        return

    # 부모 행 제목도 같은 세션 제목으로 갱신 (실패해도 턴 기록은 이미 성공)
    if parent_id:
        update_title(parent_id, title)


def main():
    # 재귀 가드: 요약용 claude -p 가 트리거한 Stop 훅이면 즉시 종료
    if os.environ.get(SKIP_ENV):
        sys.exit(0)

    # 자정 가드: 23:50~00:10 에는 기록하지 않는다 (early fail).
    # 자식 행 선택(find_or_create_day_row)과 기록(record_turn) 사이에 날짜가
    # 바뀌면 턴이 전날 행에 들어가 daily_summary 의 날짜별 귀속이 틀어지므로,
    # 그 경계 구간 자체를 피한다. 이 구간의 턴은 기록을 포기한다.
    now = datetime.now()
    minutes = now.hour * 60 + now.minute
    if minutes >= 23 * 60 + 50 or minutes < 10:
        sys.exit(0)

    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}

    # 워커 모드: 실제 작업 수행
    if "--worker" in sys.argv:
        run_worker(data)
        sys.exit(0)

    # 훅 모드: 백그라운드 워커를 띄우고 즉시 리턴 (턴 블로킹 방지)
    # 워커 env 에는 SKIP 을 넣지 않는다 (넣으면 워커 main()이 맨 앞 가드에 걸려 죽음).
    # 재귀 방지는 summarize() 안의 claude -p 호출에만 SKIP 을 걸어 처리한다.
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # 부모(훅)와 분리
    )
    try:
        proc.stdin.write(raw.encode("utf-8"))
        proc.stdin.close()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
