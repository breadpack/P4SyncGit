"""사용자 매핑 플러그인.

환경에 맞게 정규식 패턴과 도메인을 수정하세요.

P4 환경:
  - 공유 계정: CODE
  - Workspace 패턴: CODE_{user_id}.dev
  - Description 패턴: [{표시이름}] 내용
"""

import re

# -- 환경에 맞게 수정 --
WORKSPACE_PATTERN = re.compile(r"CODE_(.+)\.(?:dev|sync)")
DESCRIPTION_PATTERN = re.compile(r"^\[(.+?)\]")
EMAIL_DOMAIN = "company.com"
SHARED_ACCOUNT = "CODE"
SUBMIT_WORKSPACE_TEMPLATE = "CODE_{user_id}.sync"
# ----------------------


def p4_to_git(changelist_info: dict) -> dict:
    """P4 changelist → Git author."""
    ws = changelist_info.get("workspace", "")
    m = WORKSPACE_PATTERN.match(ws)
    user_id = m.group(1) if m else changelist_info.get("user", "unknown")

    desc = changelist_info.get("description", "")
    m2 = DESCRIPTION_PATTERN.match(desc)
    display_name = m2.group(1) if m2 else user_id

    return {
        "name": display_name,
        "email": f"{user_id}@{EMAIL_DOMAIN}",
    }


def git_to_p4(commit_info: dict) -> dict:
    """Git commit → P4 submit 정보."""
    email = commit_info.get("author_email", "")
    user_id = email.split("@")[0]
    name = commit_info.get("author_name", user_id)

    return {
        "user": SHARED_ACCOUNT,
        "workspace": SUBMIT_WORKSPACE_TEMPLATE.format(user_id=user_id),
        "description": f"[{name}] {commit_info.get('message', '')}",
    }
