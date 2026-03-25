from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class CommitMetadata:
    author_name: str
    author_email: str
    author_timestamp: int         # Unix timestamp
    message: str
    p4_changelist: int
    integration_info: IntegrationCommitInfo | None = None

    def format_message(self) -> str:
        lines = [self.message, ""]  # 빈 줄로 본문과 trailer 분리
        lines.append(f"P4CL: {self.p4_changelist}")
        if self.integration_info:
            lines.extend(self.integration_info.format_trailer_lines())
        return "\n".join(lines)


@dataclass
class IntegrationCommitInfo:
    source_stream: str
    target_stream: str
    source_changelist: int | None = None
    integrated_files: int = 0

    def format_trailer_lines(self) -> list[str]:
        lines = [
            f"Integration: {self.source_stream} -> {self.target_stream}",
        ]
        if self.source_changelist is not None:
            lines.append(f"Source-CL: {self.source_changelist}")
        if self.integrated_files > 0:
            lines.append(f"Integrated-Files: {self.integrated_files}")
        return lines

    def format_footer(self) -> str:
        return "\n" + "\n".join(self.format_trailer_lines())


_P4CL_PATTERN = re.compile(r"(?:\[P4CL:\s*(\d+)\]|^P4CL:\s*(\d+)$)", re.MULTILINE)
_GIT_COMMIT_PATTERN = re.compile(r"(?:\[GitCommit:\s*([0-9a-f]+)\]|^GitCommit:\s*([0-9a-f]+)$)", re.MULTILINE)


def parse_p4cl_from_message(message: str) -> int | None:
    """commit message에서 P4CL 번호를 추출. [P4CL: NNN] 및 P4CL: NNN 모두 지원."""
    m = _P4CL_PATTERN.search(message)
    if m:
        return int(m.group(1) or m.group(2))
    return None


def parse_git_commit_from_description(description: str) -> str | None:
    """P4 changelist description에서 GitCommit SHA를 추출."""
    m = _GIT_COMMIT_PATTERN.search(description)
    if m:
        return m.group(1) or m.group(2)
    return None
