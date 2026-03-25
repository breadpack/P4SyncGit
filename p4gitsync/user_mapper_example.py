"""사용자 매핑 플러그인 예시.

공유 계정 CODE를 사용하는 P4 환경에서의 매핑 규칙:
  - Workspace: WORKSPACE_alice → user_id: "alice"
  - Description: [Alice Kim] 내용 → display_name: "Alice Kim"

config.toml:
  [user_mapping]
  script = "/app/user_mapper.py"
"""

import re


def p4_to_git(changelist_info: dict) -> dict:
    """P4 changelist 정보 → Git author 매핑.

    Args:
        changelist_info: {
            "user": "CODE",
            "workspace": "WORKSPACE_alice",
            "description": "[Alice Kim] 번역 언어 잘못 적용된 케이스 개선",
            "changelist": 220183,
        }

    Returns:
        {"name": "Alice Kim", "email": "alice@example.com"}
    """
    # workspace에서 user_id 추출
    ws = changelist_info["workspace"]
    m = re.match(r"CODE_(.+)\.dev", ws)
    user_id = m.group(1) if m else changelist_info["user"]

    # description에서 표시 이름 추출
    desc = changelist_info["description"]
    m2 = re.match(r"\[(.+?)\]", desc)
    display_name = m2.group(1) if m2 else user_id

    return {
        "name": display_name,
        "email": f"{user_id}@company.com",
    }


def git_to_p4(commit_info: dict) -> dict:
    """Git commit 정보 → P4 submit 정보 매핑.

    Args:
        commit_info: {
            "author_name": "Alice Kim",
            "author_email": "alice@example.com",
            "message": "번역 언어 잘못 적용된 케이스 개선",
        }

    Returns:
        {
            "user": "CODE",
            "workspace": "WORKSPACE_alice.sync",
            "description": "[Alice Kim] 번역 언어 잘못 적용된 케이스 개선",
        }
    """
    email = commit_info["author_email"]
    user_id = email.split("@")[0]
    name = commit_info["author_name"]

    return {
        "user": "CODE",
        "workspace": f"CODE_{user_id}.sync",
        "description": f"[{name}] {commit_info['message']}",
    }
