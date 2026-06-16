#!/usr/bin/env python3
"""issue-generator / issue-commentor 의 "오늘 모드" 공용 모듈.

Notion 작업 로그 DB에서 특정 프로젝트의 특정 날짜(기본: 오늘) 기록을 모아
JSON 으로 출력한다. 조회 로직은 daily_summary.py 의 함수를 그대로 재사용한다.

이 파일이 단일 소스다. 각 skill 폴더의 fetch_today.py 는 이 모듈을 호출하는
얇은 진입점(shim)일 뿐이며, 로직을 복제하지 않는다.

출력(JSON, stdout):
  {"project": "myflow", "date": "2026-06-15",
   "sessions": [{"title": "...", "turns": ["[10:20] ...", ...]}, ...]}

sessions 가 비어 있으면 그날 그 프로젝트의 로그가 없다는 뜻이다.

사용법(직접 실행, 개발용):
  python3 notion_today.py --project myflow
  python3 notion_today.py --project myflow --date 2026-06-10
평소에는 각 skill 의 fetch_today.py shim 을 통해 호출된다.
"""

import argparse
import json
import sys
from datetime import datetime


def main():
    # daily_summary 는 repo 루트에 있다. shim 이 repo 루트를 sys.path 에 넣어주므로
    # 여기서 바로 import 된다. import 자체가 실패하면 그 사실을 JSON 으로 알린다.
    try:
        from daily_summary import fetch_rows, collect_by_project, NOTION_TOKEN
    except ImportError as e:
        print(json.dumps({"error": f"daily_summary import 실패: {e}",
                          "sessions": []}, ensure_ascii=False))
        sys.exit(1)

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
