from dataclasses import dataclass


@dataclass
class CommitMetadata:
    author_name: str
    author_email: str
    author_timestamp: int         # Unix timestamp
    message: str
    p4_changelist: int

    def format_message(self) -> str:
        return f"{self.message}\n\n[P4CL: {self.p4_changelist}]"
