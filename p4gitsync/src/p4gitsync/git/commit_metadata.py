from __future__ import annotations

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
        base = f"{self.message}\n\n[P4CL: {self.p4_changelist}]"
        if self.integration_info:
            base += self.integration_info.format_footer()
        return base


@dataclass
class IntegrationCommitInfo:
    source_stream: str
    target_stream: str
    source_changelist: int | None = None
    integrated_files: int = 0

    def format_footer(self) -> str:
        lines = [
            f"\n[Integration: {self.source_stream} -> {self.target_stream}]",
        ]
        if self.source_changelist is not None:
            lines.append(f"[Source CL: {self.source_changelist}]")
        if self.integrated_files > 0:
            lines.append(f"[Integrated files: {self.integrated_files}]")
        return "\n".join(lines)
