#!/usr/bin/env python3
"""
issue-generator skill 의 "오늘 모드" 헬퍼.

Notion 작업 로그 DB에서 특정 프로젝트의 특정 날짜(기본: 오늘) 기록을 모아
JSON 으로 출력한다. 조회 로직은 daily_summary.py 의 함수를 그대로 재사용한다.

출력(JSON, stdout):
  {"project": "myflow", "date": "2026-06-15",
   "sessions": [{"title": "...", "turns": ["[10:20] ...", ...]}, ...]}

sessions 가 비어 있으면 그날 그 프로젝트의 로그가 없다는 뜻이다.

사용법
  python3 fetch_today.py --project myflow
  python3 fetch_today.py --project myflow --date 2026-06-10
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 이 파일은 repo 의 skills/issue-generator/ 안에 있고, ~/.claude/skills/ 에는
# 심링크로 노출된다. resolve() 로 심링크를 풀면 항상 repo 안의 실제 경로가 되므로,
# 거기서 두 단계 위(repo 루트)를 sys.path 에 넣어 daily_summary 를 import 한다.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from daily_summary import fetch_rows, collect_by_project, NOTION_TOKEN
except ImportError as e:
    print(json.dumps({"error": f"daily_summary import 실패: {e}",
                      "sessions": []}), ensure_ascii=False)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True,
                        help="Notion 로그의 프로젝트명 (보통 cwd 폴더명)")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="YYYY-MM-DD (기본: 오늘)")
    args = parser.parse_args()

    if not NOTION_TOKEN:
        print(json.dumps({"error": "Notion 토큰 없음 (~/.claude/notion_token.txt)",
                          "project": args.project, "date": args.date,
                          "sessions": []}, ensure_ascii=False))
        sys.exit(1)

    rows = fetch_rows(args.date)
    if rows is None:
        print(json.dumps({"error": "Notion DB 조회 실패",
                          "project": args.project, "date": args.date,
                          "sessions": []}, ensure_ascii=False))
        sys.exit(1)

    grouped = collect_by_project(rows)
    sessions = grouped.get(args.project, [])
    print(json.dumps({"project": args.project, "date": args.date,
                      "sessions": sessions}, ensure_ascii=False))


if __name__ == "__main__":
    main()
