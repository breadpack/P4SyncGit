"""P4GitSync 공용 예외."""

from __future__ import annotations


class ContentExtractionError(RuntimeError):
    """changelist의 add/edit 파일 내용 추출에 실패했을 때 발생.

    조용히 파일을 누락하면 Git tree가 P4와 silently 어긋나므로, 추출 실패는
    해당 CL 전체를 실패로 승격시켜 재시도/수동 처리(obliterate 등) 대상으로 만든다.
    """

    def __init__(self, changelist: int, missing: list[str]) -> None:
        self.changelist = changelist
        self.missing = missing
        preview = ", ".join(missing[:5])
        if len(missing) > 5:
            preview += f" 외 {len(missing) - 5}건"
        super().__init__(
            f"CL {changelist}: 파일 내용 추출 실패 {len(missing)}건 — {preview}"
        )
