#!/usr/bin/env python3
"""
Claude Code 일일 회고: Notion 작업 로그 DB에서 하루치 기록을 모아
프로젝트별로 정리한 뒤, DB가 있는 페이지 본문의 "yyyy.mm" 토글 아래에
"yyyy.mm.dd" 항목으로 기록한다.

동작 개요
  1. 작업 로그 DB에서 날짜 속성이 대상 날짜인 행을 모두 조회한다.
  2. 각 행(세션)의 제목 + 본문 [HH:MM] bullet 타임라인을 수집해 프로젝트별로 묶는다.
  3. `claude -p` 로 프로젝트별 "한 줄 요약 + 세부 작업" JSON을 생성한다.
  4. DB의 부모 페이지에서 "yyyy.mm" 토글을 찾고(없으면 생성),
     그 아래 "yyyy.mm.dd" 토글을 만들어 프로젝트별 bullet 로 기록한다.
     같은 날짜 토글이 이미 있으면 지우고 다시 쓴다(재실행 안전).

사용법
  python3 daily_summary.py                  # 오늘
  python3 daily_summary.py --date 2026-06-10
  python3 daily_summary.py --dry-run        # Notion에 쓰지 않고 결과만 출력
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
# 설정 (notion_logger.py 와 동일한 토큰/DB)
# ─────────────────────────────────────────────────────────────
NOTION_TOKEN_PATH = Path.home() / ".claude" / "notion_token.txt"
NOTION_TOKEN = NOTION_TOKEN_PATH.read_text().strip() if NOTION_TOKEN_PATH.is_file() else ""
NOTION_DATABASE_ID = "37cf979eb42380fdadf4fcf02ca704f1"
NOTION_VERSION = "2022-06-28"

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


def collect_by_project(rows):
    """행들을 {프로젝트: [{"title":…, "turns":[…]}]} 로 묶는다."""
    grouped = {}
    for row in rows:
        props = row.get("properties", {})
        try:
            title = "".join(t["plain_text"] for t in props["작업"]["title"])
        except (KeyError, TypeError):
            title = "(제목 없음)"
        try:
            project = props["프로젝트"]["select"]["name"]
        except (KeyError, TypeError):
            project = "(미분류)"
        turns = [
            block_plain_text(b)
            for b in list_children(row["id"])
            if b.get("type") == "bulleted_list_item"
        ]
        grouped.setdefault(project, []).append({"title": title, "turns": turns})
    return grouped


# ─────────────────────────────────────────────────────────────
# 2) 프로젝트별 정리 (claude -p)
# ─────────────────────────────────────────────────────────────
def build_log_text(grouped):
    parts = []
    for project, sessions in grouped.items():
        parts.append(f"## {project}")
        for s in sessions:
            parts.append(f"- 세션: {s['title']}")
            parts.extend(f"  - {t}" for t in s["turns"])
    return "\n".join(parts)[:12000]


def summarize_day(date_str, grouped):
    """[{"name":…, "headline":…, "details":[…]}] 반환. claude 실패 시 raw fallback."""
    instruction = (
        "아래는 하루 동안 Claude Code로 작업한 로그다. 프로젝트별로 한 일을 정리하라.\n"
        "다음 JSON 형식으로만, 코드블록 없이 순수 JSON 으로 답하라.\n"
        '{"projects": [{"name": "프로젝트명", '
        '"headline": "이 프로젝트에서 한 일 한 줄 요약", '
        '"details": ["세부 작업 1", "세부 작업 2"]}]}\n'
        "details 는 프로젝트당 1~5개. 같은 작업의 반복 시도는 하나로 합쳐라. "
        "name 은 로그의 프로젝트명을 그대로 쓴다. "
        "반드시 한국어로만 작성하라. 고유명사·코드·명령어만 원문 그대로 둔다. "
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
        details = [t for s in sessions for t in s["turns"]][:10]
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
    """페이지에 yyyy.mm 토글 > yyyy.mm.dd 토글 > 프로젝트 bullet 기록. 성공 여부 반환."""
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

    grouped = collect_by_project(rows)
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
