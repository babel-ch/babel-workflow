#!/usr/bin/env python3
"""
Claude Code 일일 회고: Notion 작업 로그 DB에서 하루치 기록을 모아
프로젝트별로 정리한 뒤, DB가 있는 페이지 본문의 "yyyy.mm" 토글 아래에
"yyyy.mm.dd" 항목으로 기록한다.

동작 개요
  1. 작업 로그 DB에서 날짜 속성이 대상 날짜인 행을 모두 조회한다.
  2. 각 행(세션)의 제목 + 본문 [HH:MM] bullet 과 그 아래 '파일·명령' 토글(편집/생성한
     파일·돌린 명령)까지 수집해 프로젝트별로 묶는다. 파일·명령은 요약 근거로만 쓰고
     결과 페이지에는 남기지 않는다(사람이 읽기 좋은 요약만 기록).
  3. `claude -p` 로 프로젝트별 "한 줄 요약 + 세부 작업" JSON을 생성한다.
  4. DB의 부모 페이지에서 "yyyy.mm" 토글을 찾고(없으면 생성),
     그 아래 "yyyy.mm.dd" 토글을 만들어 프로젝트별 bullet(+ 세부 작업 bullet)로 기록한다.
     같은 날짜 토글이 이미 있으면 지우고 다시 쓴다(재실행 안전).

사용법
  python3 daily_summary.py                  # 오늘
  python3 daily_summary.py --date 2026-06-10
  python3 daily_summary.py --dry-run        # Notion에 쓰지 않고 결과만 출력
  python3 daily_summary.py --force          # 행이 많아도(임계값 초과) 강행
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from collections import Counter
from datetime import datetime
from pathlib import Path

# 저장된 toggle 의 도구 호출에도 로거와 '같은' 읽기-제외 규칙을 적용하기 위해 분류기를
# 재사용한다. 옛 toggle 은 필터 도입 전에 쓰였을 수 있으므로 읽을 때 한 번 더 거른다.
try:
    from notion_logger import is_recordable_action as _is_recordable_action
except Exception:
    _is_recordable_action = None

# ─────────────────────────────────────────────────────────────
# 설정 (notion_logger.py 와 동일한 토큰/DB)
# ─────────────────────────────────────────────────────────────
NOTION_TOKEN_PATH = Path.home() / ".claude" / "notion_token.txt"
NOTION_TOKEN = NOTION_TOKEN_PATH.read_text().strip() if NOTION_TOKEN_PATH.is_file() else ""
NOTION_DATABASE_ID = "37cf979eb42380fdadf4fcf02ca704f1"
NOTION_VERSION = "2022-06-28"

# 하루 행 수 방어선: 이 수를 넘으면 더미/배치 로그를 의심하고 중단한다.
# 행마다 Notion 조회(extract_turns)가 있어, 수백~수천 행이면 수집 단계에서
# 사실상 멈춘 것처럼 느려진다. --force 로 우회 가능.
MAX_ROWS_BEFORE_WARN = 100

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
SKIP_ENV = "CLAUDE_NOTION_LOGGER_SKIP"  # 요약용 claude -p 가 Stop 훅(로거)을 타지 않게


# ─────────────────────────────────────────────────────────────
# Notion API 공통
# ─────────────────────────────────────────────────────────────
def notion_request(method, path, payload=None):
    """Notion API 호출. 성공 시 응답 dict, 실패 시 None (에러 본문은 stderr로)."""
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
    except urllib.error.HTTPError as e:
        try:
            print(f"[notion] {method} {path} → {e.code}: {e.read().decode()[:300]}",
                  file=sys.stderr)
        except Exception:
            pass
        return None
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"[notion] {method} {path} → {e}", file=sys.stderr)
        return None


def list_children(block_id):
    """블록의 자식 전체 (페이지네이션 포함)."""
    results, cursor = [], None
    while True:
        path = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        resp = notion_request("GET", path)
        if not resp:
            break
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results


def block_plain_text(block):
    t = block.get("type", "")
    rich = block.get(t, {}).get("rich_text", [])
    return "".join(r.get("plain_text", "") for r in rich)


def text_obj(content, bold=False):
    obj = {"type": "text", "text": {"content": content[:2000]}}
    if bold:
        obj["annotations"] = {"bold": True}
    return obj


# ─────────────────────────────────────────────────────────────
# 1) 하루치 작업 로그 수집
# ─────────────────────────────────────────────────────────────
def fetch_rows(date_str):
    """날짜 속성이 date_str 인 DB 행 전체. 실패 시 None."""
    rows, cursor = [], None
    while True:
        payload = {
            "filter": {"property": "날짜", "date": {"equals": date_str}},
            "page_size": 100,
        }
        if cursor:
            payload["start_cursor"] = cursor
        resp = notion_request("POST", f"/databases/{NOTION_DATABASE_ID}/query", payload)
        if resp is None:
            return None
        rows.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return rows


def _action_recordable(text):
    """저장된 'Name: detail' 액션 문자열이 기록 대상(시스템 변화)인지.
    로거의 분류기를 재사용해, 옛 toggle 에 남아 있을 수 있는 Read·grep 등 읽기 호출을
    읽는 시점에 걸러 낸다. 분류기를 못 불러오면 보수적으로 모두 통과시킨다."""
    if _is_recordable_action is None:
        return True
    name, _, detail = text.partition(": ")
    block = {"name": name}
    if name == "Bash":
        block["input"] = {"command": detail}
    return _is_recordable_action(block)


def extract_turns(row_id):
    """행의 최상위 turn bullet 들을, 각 bullet 아래 접힌 '파일·명령' 토글 내용까지
    함께 뽑는다. [{"summary": "[HH:MM] …", "actions": ["Edit: …", …]}] 반환.

    notion_logger 가 턴 요약 bullet 아래에 도구 호출을 toggle 자식으로 매달아 두므로,
    bullet 한 단계 + 토글 한 단계를 더 내려가 읽는다. 토글이 없는 옛 행이나 도구 호출이
    없던 턴은 has_children 가드로 추가 조회 없이 actions=[] 가 된다.
    읽어 온 도구 호출은 _action_recordable 로 한 번 더 걸러(옛 toggle 의 Read 등 제거)."""
    turns = []
    for b in list_children(row_id):
        if b.get("type") != "bulleted_list_item":
            continue
        actions = []
        if b.get("has_children"):
            for child in list_children(b["id"]):
                if child.get("type") != "toggle":
                    continue
                for a in list_children(child["id"]):
                    typ = a.get("type")
                    if typ in ("code", "paragraph"):
                        # 새 형식: 도구 호출을 한 블록에 개행으로 모아 담음
                        lines = block_plain_text(a).split("\n")
                    elif typ == "bulleted_list_item":
                        # 옛 형식: 도구 호출마다 bullet 한 개 (하위호환)
                        lines = [block_plain_text(a)]
                    else:
                        continue
                    for text in lines:
                        text = text.strip()
                        if text and _action_recordable(text):
                            actions.append(text)
        turns.append({"summary": block_plain_text(b), "actions": actions})
    return turns


def collect_by_project(rows, progress=False):
    """행들을 {프로젝트: [{"title":…, "turns":[{"summary", "actions":[…]}]}]} 로 묶는다.
    turns[].summary 는 사람이 읽는 요약, turns[].actions 는 그 턴의 실제 도구 호출(근거).
    행마다 Notion 조회(extract_turns)가 있어 행이 많으면 느리다. progress=True 면
    진행 상황을 stderr 로 흘려, '멈춘 것처럼 보이는' 상태를 눈에 보이게 한다."""
    grouped = {}
    total = len(rows)
    for i, row in enumerate(rows, 1):
        props = row.get("properties", {})
        try:
            title = "".join(t["plain_text"] for t in props["작업"]["title"])
        except (KeyError, TypeError):
            title = "(제목 없음)"
        try:
            project = props["프로젝트"]["select"]["name"]
        except (KeyError, TypeError):
            project = "(미분류)"
        turns = extract_turns(row["id"])
        grouped.setdefault(project, []).append({"title": title, "turns": turns})
        if progress and (i % 25 == 0 or i == total):
            print(f"  수집 중… {i}/{total}", file=sys.stderr, flush=True)
    return grouped


# ─────────────────────────────────────────────────────────────
# 2) 프로젝트별 정리 (claude -p)
# ─────────────────────────────────────────────────────────────
def _fair_budgets(sizes, total):
    """섹션 크기 목록 sizes 에 예산 total 을 공평 분배한 몫 목록을 돌려준다.
    작은 섹션은 제 크기만큼만 쓰고, 남는 예산은 큰 섹션들이 다시 균등하게 나눈다
    ('물 채우기'). 덕분에 한 섹션이 통째로 0이 되어 사라지는 일이 없다."""
    budgets = [0] * len(sizes)
    idx = list(range(len(sizes)))
    remaining = total
    while idx:
        share = remaining // len(idx)
        small = [i for i in idx if sizes[i] <= share]
        if not small:  # 남은 섹션이 모두 share 초과 → 균등 배분하고 종료
            for i in idx:
                budgets[i] = share
            break
        for i in small:  # share 안에 들어오는 섹션은 제 크기만 확정, 남은 예산 회수
            budgets[i] = sizes[i]
            remaining -= sizes[i]
            idx.remove(i)
    return budgets


def build_log_text(grouped, total_budget=32000):
    """요약 LLM 에 넣을 텍스트. 턴 요약(summary)과 그 근거(actions: 편집/생성한
    파일·돌린 명령)를 함께 적어, 정확히 정리할 수 있게 한다. 정리 결과는 사람이 읽기
    좋게 만들도록 instruction 에서 따로 지시한다.

    프로젝트마다 예산을 공평하게 나눠 자른다. 전체를 한 번에 자르면 세션·도구 호출이
    폭증한 한 프로젝트가 예산을 다 먹어 나머지가 통째로 잘리고, 그 프로젝트들이
    요약에서 아예 누락된다(입력에 안 들어가므로)."""
    sections = []
    for project, sessions in grouped.items():
        lines = [f"## {project}"]
        for s in sessions:
            lines.append(f"- 세션: {s['title']}")
            for t in s["turns"]:
                lines.append(f"  - {t['summary']}")
                for a in t.get("actions", []):
                    lines.append(f"      · {a}")
        sections.append("\n".join(lines))

    budgets = _fair_budgets([len(s) for s in sections], total_budget)
    return "\n".join(s[:b] for s, b in zip(sections, budgets))


def summarize_day(date_str, grouped):
    """[{"name":…, "headline":…, "details":[…]}] 반환. claude 실패 시 raw fallback.
    actions(파일·명령)는 build_log_text 로 요약 근거에만 쓰고 결과에는 넣지 않는다."""
    instruction = (
        "아래는 하루 동안 Claude Code로 작업한 로그다. 프로젝트별로 한 일을 정리하라.\n"
        "다음 JSON 형식으로만, 코드블록 없이 순수 JSON 으로 답하라.\n"
        '{"projects": [{"name": "프로젝트명", '
        '"headline": "이 프로젝트에서 한 일 한 줄 요약", '
        '"details": ["세부 작업 1", "세부 작업 2"]}]}\n'
        "details 는 프로젝트당 1~5개. 같은 작업의 반복 시도는 하나로 합쳐라. "
        "name 은 로그의 프로젝트명을 그대로 쓴다.\n"
        "핵심: '무엇을 했는지'를 상위 수준의 의도·성과로 적어라. 로그에 파일명·경로·"
        "함수명·명령어가 나와도 그건 무슨 일을 했는지 파악하는 근거로만 쓰고, 결과 문장에 "
        "그대로 나열하지 마라. 여러 파일은 그것들이 함께 이루는 기능·모듈로 묶어 표현하라.\n"
        "  예) 나쁨: '핵심 파이프라인 구현(audioio.py, spans.py, stt.py, merge.py, cli.py)'\n"
        "      좋음: '오디오 입력부터 STT·PII 탐지·병합까지 잇는 핵심 파이프라인 구현'\n"
        "  예) 나쁨: 'compare.py 로 span 비교, split_fp.py 로 비-PII 분리'\n"
        "      좋음: '두 검출기 결과를 비교하고 오탐을 걸러 후보를 재정리'\n"
        "반드시 한국어로만 작성하라. 번역이 어색한 고유명사만 원문 그대로 둔다. "
        "군더더기 표현 없이 작업 내용만 적어라.\n\n"
        f"[{date_str}] 작업 로그\n{build_log_text(grouped)}"
    )
    env = dict(os.environ)
    env[SKIP_ENV] = "1"
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", instruction],
            capture_output=True, text=True, timeout=180, env=env,
        )
        out = result.stdout.strip()
        start, end = out.find("{"), out.rfind("}")
        parsed = json.loads(out[start:end + 1])
        projects = []
        for p in parsed.get("projects", []):
            name = str(p.get("name", "")).strip()
            headline = str(p.get("headline", "")).strip()
            details = [str(d).strip() for d in p.get("details", []) if str(d).strip()]
            if name and headline:
                projects.append({"name": name, "headline": headline, "details": details})
        if projects:
            return projects
    except Exception as e:
        print(f"[summarize] claude 요약 실패, raw fallback 사용: {e}", file=sys.stderr)

    # fallback: 세션 제목을 한 줄 요약으로, 턴 bullet 을 세부 항목으로
    projects = []
    for project, sessions in grouped.items():
        headline = " / ".join(s["title"] for s in sessions)[:200]
        details = [t["summary"] for s in sessions for t in s["turns"]][:10]
        projects.append({"name": project, "headline": headline, "details": details})
    return projects


# ─────────────────────────────────────────────────────────────
# 3) 페이지 본문에 yyyy.mm > yyyy.mm.dd 구조로 기록
# ─────────────────────────────────────────────────────────────
def get_parent_page_id():
    """DB가 들어 있는 페이지 ID. 페이지가 아니면 None."""
    resp = notion_request("GET", f"/databases/{NOTION_DATABASE_ID}")
    parent = (resp or {}).get("parent", {})
    return parent.get("page_id")


def find_toggle(block_id, text):
    """자식 중 plain text 가 text 와 같은 toggle 블록 ID. 없으면 None."""
    for b in list_children(block_id):
        if b.get("type") == "toggle" and block_plain_text(b).strip() == text:
            return b["id"]
    return None


def append_toggle(parent_id, text, bold=False):
    """parent 끝에 toggle 블록을 추가하고 그 ID 반환. 실패 시 None."""
    resp = notion_request("PATCH", f"/blocks/{parent_id}/children", {
        "children": [{
            "object": "block",
            "type": "toggle",
            "toggle": {"rich_text": [text_obj(text, bold=bold)]},
        }]
    })
    try:
        return resp["results"][0]["id"]
    except (TypeError, KeyError, IndexError):
        return None


def write_summary(date_str, projects):
    """페이지에 yyyy.mm 토글 > yyyy.mm.dd 토글 > 프로젝트 bullet(+ 세부 bullet) 기록.
    파일·명령은 남기지 않는다(사람이 읽기 좋은 요약만). 성공 여부 반환."""
    page_id = get_parent_page_id()
    if not page_id:
        print("DB의 부모가 페이지가 아니어서 기록할 위치를 찾지 못했습니다.", file=sys.stderr)
        return False

    y, m, d = date_str.split("-")
    month_text = f"{y}.{m}"
    date_text = f"{y}.{m}.{d}"

    month_id = find_toggle(page_id, month_text) or append_toggle(page_id, month_text, bold=True)
    if not month_id:
        return False

    # 재실행 안전: 같은 날짜 토글이 있으면 지우고 다시 쓴다
    old = find_toggle(month_id, date_text)
    if old:
        notion_request("DELETE", f"/blocks/{old}")

    date_id = append_toggle(month_id, date_text)
    if not date_id:
        return False

    # 날짜 토글 아래: 프로젝트별 bullet(헤더 "프로젝트 — 한 줄 요약") + 세부 작업 bullet 자식.
    children = []
    for p in projects:
        children.append({
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [text_obj(p["name"], bold=True),
                              text_obj(f" — {p['headline']}")],
                "children": [{
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": [text_obj(detail)]},
                } for detail in p["details"][:20]],
            },
        })
    resp = notion_request("PATCH", f"/blocks/{date_id}/children", {"children": children})
    return bool(resp)


# ─────────────────────────────────────────────────────────────
# 엔트리
# ─────────────────────────────────────────────────────────────
def main():
    date_str = datetime.now().strftime("%Y-%m-%d")
    if "--date" in sys.argv:
        date_str = sys.argv[sys.argv.index("--date") + 1]
    dry_run = "--dry-run" in sys.argv

    if not NOTION_TOKEN:
        print(f"Notion 토큰이 없습니다: {NOTION_TOKEN_PATH}", file=sys.stderr)
        sys.exit(1)

    rows = fetch_rows(date_str)
    if rows is None:
        print("작업 로그 DB 조회에 실패했습니다.", file=sys.stderr)
        sys.exit(1)
    if not rows:
        print(f"{date_str} 작업 로그가 없습니다. 기록을 건너뜁니다.")
        return

    # 방어: 하루 행 수가 비정상적으로 많으면 더미/배치 로그일 수 있다. 행마다 Notion
    # 조회가 있어 수백~수천 행이면 수집 단계에서 사실상 멈춘 듯 느려지므로, 프로젝트별
    # 개수를 보여 주고 --force 없이는 중단한다(원인을 눈으로 확인하고 진행하도록).
    if len(rows) > MAX_ROWS_BEFORE_WARN and "--force" not in sys.argv:
        by_proj = Counter()
        for r in rows:
            try:
                by_proj[r["properties"]["프로젝트"]["select"]["name"]] += 1
            except (KeyError, TypeError):
                by_proj["(미분류)"] += 1
        print(f"경고: {date_str} 행이 {len(rows)}개로 비정상적으로 많습니다 "
              f"(임계값 {MAX_ROWS_BEFORE_WARN}). 더미/배치 로그일 수 있습니다.",
              file=sys.stderr)
        for p, c in by_proj.most_common():
            print(f"    {p}: {c}", file=sys.stderr)
        print("각 행마다 Notion 조회가 일어나 매우 느립니다. 확인 후에도 진행하려면 "
              "--force 를 붙이세요.", file=sys.stderr)
        sys.exit(1)

    grouped = collect_by_project(rows, progress=len(rows) > 25)
    print(f"{date_str}: {len(rows)}개 세션, {len(grouped)}개 프로젝트 수집. 요약 생성 중…")
    projects = summarize_day(date_str, grouped)

    print()
    for p in projects:
        print(f"- {p['name']} — {p['headline']}")
        for detail in p["details"]:
            print(f"    - {detail}")
    print()

    if dry_run:
        print("(--dry-run: Notion에 기록하지 않음)")
        return

    if write_summary(date_str, projects):
        print(f"Notion 페이지에 {date_str} 회고를 기록했습니다.")
    else:
        print("Notion 기록에 실패했습니다.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
