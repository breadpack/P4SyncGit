def depot_to_git_path(depot_path: str, stream: str, stream_prefix_len: int) -> str | None:
    """depot path를 Git 리포지토리 내의 상대 경로로 변환.

    예: //ProjectSTAR/main/src/foo.py -> src/foo.py
    """
    if not depot_path.startswith(stream + "/"):
        return None
    return depot_path[stream_prefix_len:]
