"""Typed schemas for safe inbox ingestion and assignment production."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class InboxSource(str, Enum):
    WHATSAPP = "whatsapp"
    DISCORD = "discord"
    GMAIL = "gmail"
    DOWNLOADS = "downloads"
    MANUAL_IMPORT = "manual_import"
    FOLDER_WATCH = "folder_watch"


@dataclass(frozen=True)
class InboxMessageContext:
    text: str = ""
    sender: str = ""
    channel: str = ""
    timestamp: str = ""
    available: bool = False
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class InboxAttachment:
    attachment_id: str
    filename: str
    local_path: str = ""
    media_type: str = "application/octet-stream"
    size: int | None = None
    file_profile: dict[str, Any] = field(default_factory=dict)
    source_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InboxItem:
    inbox_item_id: str
    source: InboxSource
    message_context: InboxMessageContext
    attachments: tuple[InboxAttachment, ...] = ()
    extracted_instructions: tuple[str, ...] = ()
    deadline: str = ""
    subject: str = ""
    confidence: float = 0.0
    missing_information: tuple[str, ...] = ()
    safety_notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source"] = self.source.value
        return value


@dataclass(frozen=True)
class AttachmentAnalysis:
    attachment_id: str
    path: str
    short_summary: str
    document_type: str
    subject: str = ""
    instructions: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    headings: tuple[str, ...] = ()
    required_deliverable: str = ""
    missing_information: tuple[str, ...] = ()
    confidence: float = 0.0
    text_extraction: str = "metadata_only"
    extracted_text: str = ""
    file_profile: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AssignmentTask:
    task_id: str
    task_type: str
    instructions: str
    source_reference: str = ""
    required_format: str = ""
    deadline: str = ""
    subject: str = ""
    marks_or_word_limit: str = ""
    output_sections: tuple[str, ...] = ()
    confidence: float = 0.0
    missing_information: tuple[str, ...] = ()
    user_review_required: bool = True


@dataclass(frozen=True)
class AssignmentPlan:
    assignment_id: str
    inbox_item_id: str
    output_type: str
    document_structure: tuple[str, ...]
    steps: tuple[str, ...]
    sources: tuple[str, ...]
    outside_knowledge_needed: bool
    user_input_needed: tuple[str, ...] = ()
    completion_status: str = "planned"
    assumptions: tuple[str, ...] = ()
    tasks: tuple[AssignmentTask, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AssignmentOutput:
    assignment_id: str
    output_directory: str
    analysis_path: str
    plan_path: str
    draft_path: str
    sources_path: str
    report_path: str
    final_path: str = ""
    review_required: bool = True
    submitted: bool = False


@dataclass(frozen=True)
class SubmissionDraft:
    assignment_id: str
    connector: str
    recipient: str
    message: str
    attachment_paths: tuple[str, ...]
    requires_confirmation: bool = True
    sent: bool = False


@dataclass(frozen=True)
class InboxIngestionResult:
    success: bool
    item: InboxItem | None = None
    analyses: tuple[AttachmentAnalysis, ...] = ()
    assignment_id: str = ""
    ambiguous_candidates: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    next_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.item is not None:
            value["item"] = self.item.to_dict()
        return value
