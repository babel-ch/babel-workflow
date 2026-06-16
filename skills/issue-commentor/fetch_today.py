#!/usr/bin/env python3
"""issue-commentor "오늘 모드" 헬퍼의 진입점(shim).

실제 로직은 repo 루트의 notion_today.py 한 곳에 있다. 이 파일은 그쪽으로
인자를 그대로 넘겨 실행할 뿐이며, issue-generator 의 fetch_today.py 와
완전히 동일한 shim 이다(로직 복제 없음). issue-generator 를 거치지 않는다.

이 파일은 ~/.claude/skills/issue-commentor/fetch_today.py 심링크로 노출된다.
resolve() 로 심링크를 풀면 repo 안 실제 경로가 되고, 두 단계 위가 repo 루트다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from notion_today import main

if __name__ == "__main__":
    main()
