#!/usr/bin/env python3
"""
Claude Code Stop hook: 세션 단위로 작업 내용을 Notion 데이터베이스에 기록한다.

구조
  - 세션 1개 = 부모 행 1개 + 그 아래 날짜별 자식 행(sub-item).
      부모 행: 세션 전체 제목(작업) + 세션ID. 날짜는 비워 둔다.
      자식 행: 날짜 = 그날, Parent item = 부모. 그날의 턴 bullet 들이 본문에 쌓인다.
    날짜별 view 는 자식 행으로, 프로젝트별 view 는 부모 토글로 보는 구조.
  - 제목(작업)은 세션 전체를 아우르는 짧은 한 줄로 턴마다 갱신 (부모·자식 동일).
  - 턴별 상세는 자식 행 본문에 "[HH:MM] 요약" bullet 로 append 된다.
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
     만든 뒤 Notion에 기록. 요약은 도구 호출 내역을 근거로 작성하게 한다.
  5. Notion 기록 실패 시 로컬 파일(~/.claude/notion-log/)에 fallback 기록.
"""

import json
import os
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
        '"turn": "사용자가 무엇을 요청했고 그에 대해 실제로 무엇을 했는지 담은 한두 문장"}\n\n'
        f"프로젝트: {project}\n{title_part}\n"
        "turn 요약은 두 가지를 모두 담아라. "
        "(1) 사용자가 어떤 의도로 무슨 요청을 했는지 — 프롬프트에 배경·이유가 "
        "드러나 있으면 짧게 함께 적어라. "
        "(2) 그에 대해 실제로 한 작업 — Claude 응답의 표현을 옮기지 말고, "
        "실제 실행된 도구 호출(어떤 명령을 돌렸고 어떤 파일을 고쳤는지)을 근거로 "
        "작성하라. 핵심이 되는 명령어·파일명은 원문 그대로 포함하고, "
        "조사·탐색용 호출(Read, ls 등)은 빼고 결과를 바꾼 작업 위주로 적어라.\n"
        "반드시 한국어로만 작성하라. 영어·일본어 등 다른 언어 단어를 섞지 마라. "
        "고유명사나 코드/명령어 등 번역이 어색한 것만 원문 그대로 둔다. "
        "군더더기 표현 없이 작업 내용만 적어라.\n\n"
        f"[사용자 요청]\n{prompt[:3000]}\n\n"
        f"{actions_part}"
        f"[Claude 응답]\n{response[:4000]}"
    )
    env = dict(os.environ)
    env[SKIP_ENV] = "1"  # 이 claude 호출의 Stop 훅 재귀 방지
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", instruction],
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


def record_turn(page_id, title, turn_summary, last_date=None):
    """행 제목 갱신 + 본문에 턴 bullet append. 성공 여부 반환.
    마지막 기록일(last_date)과 오늘이 다르면 bullet 앞에 날짜 heading 을 먼저 넣는다."""
    ok_title = update_title(page_id, title)
    today = datetime.now().strftime("%Y-%m-%d")
    if last_date is None:
        last_date = get_last_log_date(page_id) or today
    now = datetime.now().strftime("%H:%M")
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
    ok_body = notion_request("PATCH", f"/blocks/{page_id}/children", {"children": children})
    return bool(ok_title) and bool(ok_body)


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

    if child_id is None or not record_turn(child_id, title, turn, last_date):
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
