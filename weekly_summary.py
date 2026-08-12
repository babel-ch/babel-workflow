#!/usr/bin/env python3
"""
Claude Code 주간 회고: daily_summary.py 가 페이지 본문에 쌓아 둔 날짜별 정리를
한 주(월~일) 단위로 다시 묶어, 같은 월 토글 안에
"yyyy.mm.dd~mm.dd (주간)" 토글로 기록한다.

동작 개요
  1. 페이지 본문의 "yyyy.mm" 토글 아래에서 "yyyy.mm.dd" 토글을 훑어,
     이번 주 포함 최근 3주 중 어느 주에 기록이 있는지 보여 주고 정리할 주를 묻는다.
  2. 고른 주의 날짜 토글에서 프로젝트 bullet(한 줄 요약)과 그 아래 세부 bullet 을
     모두 읽어, 로그상 프로젝트명별로 날짜순 정렬한다.
  3. `claude -p` 로 주간 정리 JSON 을 생성한다. 이때
     - 같은 프로젝트가 날마다 다른 이름으로 적혀 있으면 하나로 합치고(aliases 로 남김),
     - 한 이름 아래 성격이 다른 작업이 섞여 있으면 갈래(tracks)로 나눈다.
  4. 주 시작일이 속한 월의 "yyyy.mm" 토글 아래, 그 주 마지막 날짜 토글 바로 뒤에
     "yyyy.mm.dd~mm.dd (주간)" 토글을 만들어 기록한다.
     같은 주간 토글이 이미 있으면 지우고 다시 쓴다(재실행 안전).

사용법
  python3 weekly_summary.py                      # 대화형으로 주 선택
  python3 weekly_summary.py --week 2026-08-03    # 그 날짜가 속한 주를 바로 정리
  python3 weekly_summary.py --last-week          # 지난주
  python3 weekly_summary.py --dry-run            # Notion에 쓰지 않고 결과만 출력
"""

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta

# 토큰·DB·Notion 호출·블록 헬퍼는 daily_summary 와 같은 것을 쓴다.
# (daily_summary 는 import 만으로는 아무것도 실행하지 않는다)
from daily_summary import (
    CLAUDE_BIN,
    NOTION_TOKEN,
    NOTION_TOKEN_PATH,
    SKIP_ENV,
    _fair_budgets,
    append_toggle,
    block_plain_text,
    find_toggle,
    get_parent_page_id,
    list_children,
    notion_request,
    text_obj,
)

MONTH_RE = re.compile(r"^\d{4}\.\d{2}$")
DAY_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

# 주간 토글에 담을 최대 개수(너무 길어지면 읽히지 않으므로 잘라 낸다)
MAX_PROJECTS = 20
MAX_TRACKS = 6

# 요약 호출 제한시간(초). 한 주치 재구성은 출력이 길어 실측 4분 안팎이 걸린다.
CLAUDE_TIMEOUT = 900


# ─────────────────────────────────────────────────────────────
# 주 계산
# ─────────────────────────────────────────────────────────────
def week_start(d):
    """d 가 속한 주의 월요일."""
    return d - timedelta(days=d.weekday())


def week_dates(start):
    """월요일 start 부터 7일."""
    return [start + timedelta(days=i) for i in range(7)]


def week_toggle_text(start):
    """주간 토글 제목. 예: '2026.07.27~08.02 (주간)'"""
    end = start + timedelta(days=6)
    return f"{start:%Y.%m.%d}~{end:%m.%d} (주간)"


def week_label(start, today):
    """'이번 주' / '지난주' / 'N주 전' 라벨."""
    diff = (week_start(today) - start).days // 7
    return {0: "이번 주", 1: "지난주"}.get(diff, f"{diff}주 전")


# ─────────────────────────────────────────────────────────────
# 1) 페이지 본문에서 날짜별 정리 읽기
# ─────────────────────────────────────────────────────────────
def list_month_toggles(page_id):
    """페이지 최상위의 {'yyyy.mm': block_id}."""
    months = {}
    for b in list_children(page_id):
        if b.get("type") != "toggle":
            continue
        text = block_plain_text(b).strip()
        if MONTH_RE.match(text):
            months[text] = b["id"]
    return months


def list_day_toggles(month_id):
    """월 토글 아래의 {'yyyy.mm.dd': block_id}. 주간 토글 등 다른 제목은 무시한다."""
    days = {}
    for b in list_children(month_id):
        if b.get("type") != "toggle":
            continue
        text = block_plain_text(b).strip()
        if DAY_RE.match(text):
            days[text] = b["id"]
    return days


def day_map(page_id, months_needed):
    """필요한 월만 조회해 {'yyyy.mm.dd': block_id} 를 모은다.
    months_needed 는 {'yyyy.mm', …}. 주 후보를 보여 줄 때는 이 정도만 있으면 되므로,
    날짜 토글 안까지 내려가지 않아 조회가 가볍다."""
    result = {}
    months = list_month_toggles(page_id)
    for m in sorted(months_needed):
        if m in months:
            result.update(list_day_toggles(months[m]))
    return result


def parse_day_toggle(day_id):
    """날짜 토글 아래의 프로젝트 bullet 을 읽어
    [{"project":…, "headline":…, "details":[…]}] 로 돌려준다.

    daily_summary.write_summary 가 쓴 bullet 은 rich_text = [프로젝트명(bold), ' — 요약']
    구조다. bold 조각을 프로젝트명으로 보되, 서식이 사라진 경우를 대비해 ' — ' 분리도
    폴백으로 둔다."""
    items = []
    for b in list_children(day_id):
        if b.get("type") != "bulleted_list_item":
            continue
        rich = b["bulleted_list_item"].get("rich_text", [])
        project, headline = "", ""
        if rich and rich[0].get("annotations", {}).get("bold"):
            project = rich[0].get("plain_text", "").strip()
            headline = "".join(r.get("plain_text", "") for r in rich[1:])
        else:
            project, _, headline = block_plain_text(b).partition(" — ")
        headline = headline.lstrip(" —").strip()
        project = project.strip() or "(미분류)"

        details = []
        if b.get("has_children"):
            for c in list_children(b["id"]):
                text = block_plain_text(c).strip()
                if text:
                    details.append(text)
        items.append({"project": project, "headline": headline, "details": details})
    return items


def collect_week(page_id, start, progress=True):
    """한 주치를 {'yyyy.mm.dd': [{"project","headline","details"}]} 로 모은다.
    기록이 없는 날은 키 자체가 없다."""
    dates = week_dates(start)
    months = {f"{d:%Y.%m}" for d in dates}
    days = day_map(page_id, months)

    collected = {}
    targets = [f"{d:%Y.%m.%d}" for d in dates if f"{d:%Y.%m.%d}" in days]
    for i, key in enumerate(targets, 1):
        items = parse_day_toggle(days[key])
        if items:
            collected[key] = items
        if progress:
            print(f"  수집 중… {i}/{len(targets)} ({key})", file=sys.stderr, flush=True)
    return collected


# ─────────────────────────────────────────────────────────────
# 2) 주 선택 (대화형)
# ─────────────────────────────────────────────────────────────
def survey_weeks(page_id, today, count=3):
    """이번 주 포함 최근 count 주의 [(월요일, 기록된 날짜 목록, 이미 정리됐는지)].
    날짜 토글 목록만 보고 판단하므로 토글 내부는 읽지 않는다."""
    starts = [week_start(today) - timedelta(weeks=i) for i in range(count)]
    months = {f"{d:%Y.%m}" for s in starts for d in week_dates(s)}
    days = day_map(page_id, months)
    month_toggles = list_month_toggles(page_id)

    survey = []
    for s in starts:
        logged = [f"{d:%Y.%m.%d}" for d in week_dates(s) if f"{d:%Y.%m.%d}" in days]
        month_id = month_toggles.get(f"{s:%Y.%m}")
        done = bool(month_id and find_toggle(month_id, week_toggle_text(s)))
        survey.append((s, logged, done))
    return survey


def ask_week(survey, today):
    """정리할 주를 물어 월요일 date 를 돌려준다. 취소하면 None."""
    print("\n정리할 주를 고르세요.\n")
    for i, (start, logged, done) in enumerate(survey, 1):
        end = start + timedelta(days=6)
        note = f"기록 {len(logged)}일" if logged else "기록 없음"
        if logged:
            note += " (" + ", ".join(d[5:] for d in logged) + ")"
        if done:
            note += " · 이미 정리됨 → 다시 쓰면 덮어씀"
        print(f"  {i}) {start:%Y.%m.%d}~{end:%m.%d}  [{week_label(start, today)}]  {note}")

    default = next((i for i, (_, logged, done) in enumerate(survey, 1) if logged and not done), 1)
    print()
    try:
        raw = input(f"번호 입력 [{default}] (q=취소): ").strip()
    except EOFError:
        return None
    if raw.lower() in ("q", "quit", "n", "no"):
        return None
    if not raw:
        return survey[default - 1][0]
    if raw.isdigit() and 1 <= int(raw) <= len(survey):
        return survey[int(raw) - 1][0]
    print("번호를 잘못 입력했습니다.", file=sys.stderr)
    return None


# ─────────────────────────────────────────────────────────────
# 3) 주간 정리 (claude -p)
# ─────────────────────────────────────────────────────────────
def build_week_text(collected, total_budget=48000):
    """요약 LLM 에 넣을 텍스트. 로그에 적힌 프로젝트명별로 묶고, 각 항목에 날짜를 단다.
    이름별로 묶어 두면 '같은 프로젝트의 다른 이름' 을 나란히 놓고 판단하기 쉽고,
    날짜가 붙어 있어 한 주의 진행 흐름을 읽을 수 있다.

    프로젝트마다 예산을 공평하게 나눠 자른다(daily_summary 와 같은 이유 — 한 프로젝트가
    예산을 다 먹어 나머지가 통째로 누락되는 것을 막는다)."""
    by_project = {}
    for day, items in sorted(collected.items()):
        for it in items:
            by_project.setdefault(it["project"], []).append((day, it))

    sections = []
    for project, entries in by_project.items():
        lines = [f"## {project}"]
        for day, it in entries:
            d = datetime.strptime(day, "%Y.%m.%d").date()
            lines.append(f"- ({day[5:]} {WEEKDAY_KO[d.weekday()]}) {it['headline']}")
            for detail in it["details"]:
                lines.append(f"    · {detail}")
        sections.append("\n".join(lines))

    budgets = _fair_budgets([len(s) for s in sections], total_budget)
    return "\n\n".join(s[:b] for s, b in zip(sections, budgets))


def _fallback(collected):
    """claude 실패 시: 로그상 프로젝트명을 그대로 쓰고, 날짜별 한 줄 요약을 갈래로 나열."""
    by_project = {}
    for day, items in sorted(collected.items()):
        for it in items:
            by_project.setdefault(it["project"], []).append((day, it["headline"]))
    projects = []
    for name, entries in by_project.items():
        projects.append({
            "name": name,
            "aliases": [],
            "headline": f"{len(entries)}일에 걸쳐 작업",
            "tracks": [{"title": day[5:], "detail": headline, "days": [day[5:]]}
                       for day, headline in entries],
        })
    return projects


def summarize_week(start, collected):
    """(projects, ok) 반환. projects 는
    [{"name", "aliases", "headline", "tracks":[{"title","detail","days"}]}].
    ok=False 면 claude 요약이 실패해 fallback(날짜별 나열)을 돌려준 것이다 —
    주간 정리로서는 값이 없으므로 호출자가 기록 여부를 판단한다."""
    end = start + timedelta(days=6)
    instruction = (
        f"아래는 {start:%Y-%m-%d}(월)~{end:%Y-%m-%d}(일) 한 주 동안 Claude Code로 작업한 "
        "날짜별 기록이다. 이것을 한 주 단위 회고로 다시 묶어라.\n"
        "다음 JSON 형식으로만, 코드블록 없이 순수 JSON 으로 답하라.\n"
        '{"projects": [{"name": "대표 프로젝트명", "aliases": ["같은 프로젝트의 다른 이름"], '
        '"headline": "이 프로젝트에서 이번 주에 한 일 한 줄 요약", '
        '"tracks": [{"title": "작업 갈래 이름", "detail": "그 갈래에서 한 일과 결과", '
        '"days": ["MM.DD"]}]}]}\n'
        "\n"
        "[프로젝트 정리 규칙 — 가장 중요]\n"
        "1) 이름이 다르지만 같은 프로젝트인 것들은 하나로 합쳐라. 표기 차이(대소문자, "
        "하이픈·언더바, 축약·전체 이름), 같은 저장소의 하위 디렉토리, 같은 대상·목표를 "
        "다루는 기록이면 같은 프로젝트로 본다. name 에는 로그에 실제로 등장한 이름 중 "
        "가장 대표적인 것을 쓰고, 합쳐진 나머지 이름을 aliases 에 넣어라. 이름을 새로 "
        "지어내지 마라. 합친 게 없으면 aliases 는 빈 배열.\n"
        "2) 반대로, 한 이름 아래에 성격이 전혀 다른 작업이 섞여 있으면 tracks 로 나눠라. "
        "'common', 'misc' 같은 포괄적인 이름일수록 그럴 가능성이 높다. 나눌 근거가 "
        "약하면 억지로 나누지 말고 하나로 둬라.\n"
        "3) 이름이 달라도 실제로 별개의 일이면 합치지 마라. 근거 없는 병합보다 그대로 "
        "두는 편이 낫다.\n"
        "\n"
        "[내용 작성 규칙]\n"
        "- tracks 는 프로젝트당 1~5개. 각 track 은 한 주에 걸친 흐름(무엇을 하려 했고, "
        "어떻게 진행됐고, 어디까지 갔는지)이 드러나게 적어라. 날짜별 기록을 그대로 "
        "옮겨 붙이지 말고, 이어지는 시도·재시도는 하나로 합쳐 결말까지 적어라.\n"
        "- days 에는 그 갈래가 실제로 진행된 날짜만 'MM.DD' 형식으로 넣어라.\n"
        "- headline 은 이번 주 그 프로젝트의 성과를 한 문장으로. 목록 나열이 아니라 결론.\n"
        "- '무엇을 했는지'를 상위 수준의 의도·성과로 적어라. 로그에 파일명·경로·함수명·"
        "명령어가 나와도 그건 근거로만 쓰고 결과 문장에 그대로 나열하지 마라.\n"
        "  예) 나쁨: '월요일에 A 수정, 화요일에 B 수정, 수요일에 C 추가'\n"
        "      좋음: '수집부터 검증까지 파이프라인을 이어 붙여 주 후반에 전체 실행에 성공'\n"
        "- 진행 중이라 끝나지 않은 일은 어디까지 갔는지와 남은 것을 적어라.\n"
        "- 반드시 한국어로만 작성하라. 번역이 어색한 고유명사만 원문 그대로 둔다. "
        "군더더기 표현 없이 작업 내용만 적어라.\n\n"
        f"[{start:%Y.%m.%d}~{end:%m.%d}] 날짜별 기록\n{build_week_text(collected)}"
    )
    env = dict(os.environ)
    env[SKIP_ENV] = "1"
    try:
        # 한 주치를 재구성하느라 출력이 길어(실측 1.8만 토큰, 4분) daily 보다 넉넉히 잡는다.
        result = subprocess.run(
            [CLAUDE_BIN, "-p", instruction],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, env=env,
        )
        out = result.stdout.strip()
        if result.returncode != 0:
            raise RuntimeError(
                f"claude 종료코드 {result.returncode}: {(result.stderr or out)[:300]}")
        start_i, end_i = out.find("{"), out.rfind("}")
        if start_i < 0 or end_i <= start_i:
            raise ValueError(f"JSON 을 찾지 못함. 응답 앞부분: {out[:300]!r}")
        parsed = json.loads(out[start_i:end_i + 1])
        projects = []
        for p in parsed.get("projects", []):
            name = str(p.get("name", "")).strip()
            headline = str(p.get("headline", "")).strip()
            if not (name and headline):
                continue
            aliases = [str(a).strip() for a in p.get("aliases", []) if str(a).strip()]
            aliases = [a for a in aliases if a != name]
            tracks = []
            for t in p.get("tracks", []):
                title = str(t.get("title", "")).strip()
                detail = str(t.get("detail", "")).strip()
                days = [str(d).strip() for d in t.get("days", []) if str(d).strip()]
                if title or detail:
                    tracks.append({"title": title, "detail": detail, "days": days})
            projects.append({"name": name, "aliases": aliases,
                             "headline": headline, "tracks": tracks})
        if projects:
            return projects, True
        raise ValueError("JSON 은 받았으나 쓸 만한 projects 가 없음")
    except subprocess.TimeoutExpired:
        print(f"[summarize] claude 요약이 {CLAUDE_TIMEOUT}초 안에 끝나지 않았습니다.",
              file=sys.stderr)
    except Exception as e:
        print(f"[summarize] claude 요약 실패: {type(e).__name__}: {e}", file=sys.stderr)

    return _fallback(collected), False


# ─────────────────────────────────────────────────────────────
# 4) 월 토글 안에 주간 토글로 기록
# ─────────────────────────────────────────────────────────────
def append_toggle_after(parent_id, text, after_id=None, bold=False):
    """parent 아래에 toggle 을 추가하고 ID 반환. after_id 를 주면 그 블록 바로 뒤에 넣어
    날짜 토글과 시간순이 어긋나지 않게 한다. after 를 못 쓰면 맨 뒤로 폴백."""
    payload = {
        "children": [{
            "object": "block",
            "type": "toggle",
            "toggle": {"rich_text": [text_obj(text, bold=bold)]},
        }]
    }
    if after_id:
        resp = notion_request("PATCH", f"/blocks/{parent_id}/children",
                              dict(payload, after=after_id))
        try:
            return resp["results"][0]["id"]
        except (TypeError, KeyError, IndexError):
            print("[notion] after 삽입에 실패해 맨 뒤에 추가합니다.", file=sys.stderr)
    return append_toggle(parent_id, text, bold=bold)


def write_summary(start, projects):
    """주 시작일이 속한 월의 yyyy.mm 토글 아래에
    'yyyy.mm.dd~mm.dd (주간)' 토글 > 프로젝트 bullet(+ 갈래 bullet) 기록."""
    page_id = get_parent_page_id()
    if not page_id:
        print("DB의 부모가 페이지가 아니어서 기록할 위치를 찾지 못했습니다.", file=sys.stderr)
        return False

    month_text = f"{start:%Y.%m}"
    week_text = week_toggle_text(start)

    months = list_month_toggles(page_id)
    month_id = months.get(month_text) or append_toggle(page_id, month_text, bold=True)
    if not month_id:
        return False

    # 재실행 안전: 같은 주간 토글이 있으면 지우고 다시 쓴다
    old = find_toggle(month_id, week_text)
    if old:
        notion_request("DELETE", f"/blocks/{old}")

    # 그 주에 속하는 날짜 토글 중 마지막 것 뒤에 놓아 시간순을 유지한다.
    # (월을 걸치는 주면 시작월 쪽 날짜만 해당된다)
    days = list_day_toggles(month_id)
    in_week = [f"{d:%Y.%m.%d}" for d in week_dates(start) if f"{d:%Y.%m.%d}" in days]
    after_id = days[in_week[-1]] if in_week else None

    week_id = append_toggle_after(month_id, week_text, after_id=after_id)
    if not week_id:
        return False

    children = []
    for p in projects[:MAX_PROJECTS]:
        header = [text_obj(p["name"], bold=True)]
        if p["aliases"]:
            header.append(text_obj(" (" + ", ".join(p["aliases"]) + ")"))
        header.append(text_obj(f" — {p['headline']}"))

        sub = []
        for t in p["tracks"][:MAX_TRACKS]:
            rich = []
            if t["title"]:
                rich.append(text_obj(t["title"], bold=True))
                if t["detail"]:
                    rich.append(text_obj(f" — {t['detail']}"))
            else:
                rich.append(text_obj(t["detail"]))
            if t["days"]:
                rich.append(text_obj(" (" + ", ".join(t["days"]) + ")"))
            sub.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": rich},
            })

        block = {
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": header},
        }
        if sub:
            block["bulleted_list_item"]["children"] = sub
        children.append(block)

    resp = notion_request("PATCH", f"/blocks/{week_id}/children", {"children": children})
    return bool(resp)


# ─────────────────────────────────────────────────────────────
# 엔트리
# ─────────────────────────────────────────────────────────────
def resolve_start(argv, page_id, today):
    """옵션 또는 대화형 선택으로 정리할 주의 월요일을 정한다. 취소·불가면 None."""
    if "--week" in argv:
        try:
            d = datetime.strptime(argv[argv.index("--week") + 1], "%Y-%m-%d").date()
        except (IndexError, ValueError):
            print("--week 는 YYYY-MM-DD 형식이어야 합니다.", file=sys.stderr)
            return None
        return week_start(d)
    if "--last-week" in argv:
        return week_start(today) - timedelta(weeks=1)
    if "--this-week" in argv:
        return week_start(today)

    if not sys.stdin.isatty():
        print("대화형 입력을 쓸 수 없습니다. --week YYYY-MM-DD 또는 --last-week 를 쓰세요.",
              file=sys.stderr)
        return None

    print("최근 3주 기록을 확인하는 중…", file=sys.stderr)
    return ask_week(survey_weeks(page_id, today), today)


def main():
    argv = sys.argv
    dry_run = "--dry-run" in argv
    today = date.today()

    if not NOTION_TOKEN:
        print(f"Notion 토큰이 없습니다: {NOTION_TOKEN_PATH}", file=sys.stderr)
        sys.exit(1)

    page_id = get_parent_page_id()
    if not page_id:
        print("DB의 부모 페이지를 찾지 못했습니다.", file=sys.stderr)
        sys.exit(1)

    start = resolve_start(argv, page_id, today)
    if start is None:
        print("취소했습니다.")
        return
    end = start + timedelta(days=6)

    print(f"\n{start:%Y.%m.%d}~{end:%m.%d} 기록을 읽는 중…")
    collected = collect_week(page_id, start)
    if not collected:
        print(f"{start:%Y.%m.%d}~{end:%m.%d} 에 일일 정리 기록이 없습니다. "
              "daily_summary.py 를 먼저 돌렸는지 확인하세요.")
        return

    total_items = sum(len(v) for v in collected.values())
    print(f"{len(collected)}일 / 항목 {total_items}개 수집. 주간 정리 생성 중…")
    projects, ok = summarize_week(start, collected)

    print()
    for p in projects:
        alias = f" ({', '.join(p['aliases'])})" if p["aliases"] else ""
        print(f"- {p['name']}{alias} — {p['headline']}")
        for t in p["tracks"]:
            days = f" ({', '.join(t['days'])})" if t["days"] else ""
            head = f"{t['title']} — {t['detail']}" if t["title"] and t["detail"] else (
                t["title"] or t["detail"])
            print(f"    - {head}{days}")
    print()

    if dry_run:
        print("(--dry-run: Notion에 기록하지 않음)")
        return

    # 요약이 실패한 fallback 결과는 날짜별 나열이라 일일 회고와 다를 바 없다.
    # 그대로 쓰면 기존 주간 토글까지 덮어써 버리므로, 기본은 기록하지 않는다.
    if not ok and "--allow-fallback" not in argv:
        print("요약 생성에 실패해 Notion에 기록하지 않았습니다. 위 내용은 날짜별 나열 "
              "fallback 입니다. 다시 실행하거나, 그래도 기록하려면 --allow-fallback 을 "
              "붙이세요.", file=sys.stderr)
        sys.exit(1)

    if write_summary(start, projects):
        print(f"Notion 페이지 {start:%Y.%m} 토글에 "
              f"{week_toggle_text(start)} 회고를 기록했습니다.")
    else:
        print("Notion 기록에 실패했습니다.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
