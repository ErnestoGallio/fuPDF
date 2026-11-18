"""Reconfirm one offline M20.2 plan inside the sole document-owning worker."""

from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from hashlib import file_digest
from pathlib import Path
from typing import Any

from pdf_annotator.domain.annotation_commands import (
    AnnotationCommand, CommandContext, CommandSource,
)
from pdf_annotator.domain.errors import DomainValidationError, MutationErrorCode
from pdf_annotator.domain.identifiers import new_annotation_id
from pdf_annotator.domain.serialization import command_to_payload
from pdf_annotator.domain.text import TextCharacter, TextPageIndex
from pdf_annotator.util.paths import FileIdentity, identify_file
from pdf_annotator.worker.document_service import DocumentSource, run_document_worker
from pdf_annotator.worker.review_targets import locate_glyph, localized_command
from pdf_annotator.worker.text_service import WorkerTextService

from .localization_text import PageMatcher, extract_runs, page_bounds
from .localize import Localization, localize_document


@dataclass(frozen=True)
class ApplicationPlan:
    document_sha256: str
    corrections_sha256: str
    localization_sha256: str
    proposals: list[dict[str, Any]]
    localizations: list[dict[str, Any]]


def source_matches(identity: FileIdentity, expected_sha256: str) -> bool:
    if identify_file(identity.canonical_path) != identity:
        return False
    with Path(identity.canonical_path).open("rb") as stream:
        digest = file_digest(stream, "sha256").hexdigest()
    return digest == expected_sha256 and identify_file(identity.canonical_path) == identity


@dataclass
class WorkerPlan:
    document_id: str
    identity: FileIdentity
    plan: ApplicationPlan
    source_verified: bool = field(default=False, init=False)
    by_page: dict[int, list[int]] = field(default_factory=lambda: defaultdict(list), init=False)
    fresh: dict[int, Localization] = field(default_factory=dict, init=False)
    page: int | None = field(default=None, init=False)
    matcher: PageMatcher | None = field(default=None, init=False)
    text_index: TextPageIndex | None = field(default=None, init=False)
    characters: tuple[TextCharacter, ...] = field(default=(), init=False)
    issued: set[int] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        for index, proposal in enumerate(self.plan.proposals):
            self.by_page[proposal["page_index"]].append(index)

    def prepare(self, service: WorkerTextService, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"proposal_index"}:
            return payload  # Preserve ordinary commands, including Undo and Redo.
        if service.document_id != self.document_id or service.generation != 1:
            raise DomainValidationError(MutationErrorCode.STALE_GENERATION)
        try:
            unchanged = identify_file(self.identity.canonical_path) == self.identity
        except (OSError, ValueError):
            unchanged = False
        if not unchanged:
            raise DomainValidationError(MutationErrorCode.STALE_GENERATION)
        index = payload["proposal_index"]
        if type(index) is not int or not 0 <= index < len(self.plan.proposals):
            raise DomainValidationError(MutationErrorCode.INVALID_COMMAND)
        if index in self.issued:
            raise DomainValidationError(MutationErrorCode.DUPLICATE_COMMAND)
        if not self.source_verified:
            if not source_matches(self.identity, self.plan.document_sha256):
                raise DomainValidationError(MutationErrorCode.STALE_GENERATION, reason="source_hash_mismatch")
            self.source_verified = True
        if index not in self.fresh:
            indices = self.by_page[self.plan.proposals[index]["page_index"]]
            # Evaluate all proposals on this page together, before applying any
            # of them, to preserve the localizer's fallback spacing and order.
            page_results = localize_document(service.document, [self.plan.proposals[i] for i in indices])
            self.fresh.update((i, replace(result, correction_index=i))
                              for i, result in zip(indices, page_results, strict=True))
        fresh = self.fresh[index]
        if not fresh.applicable or asdict(fresh) != self.plan.localizations[index]:
            raise DomainValidationError(MutationErrorCode.STALE_TEXT_TARGET,
                                        reason=fresh.reason or "localization_reverification_mismatch")
        try:
            command = self.command(service, fresh, self.plan.proposals[index])
        except DomainValidationError:
            raise
        except ValueError as exc:
            raise DomainValidationError(MutationErrorCode.INVALID_COMMAND) from exc
        serialized = command_to_payload(command)
        self.issued.add(index)
        return serialized

    def command(self, service: WorkerTextService, verified: Localization,
                proposal: dict[str, Any]) -> AnnotationCommand:
        context = CommandContext.create(self.document_id, service.generation, CommandSource.AI)
        annotation_id = new_annotation_id()
        if verified.operation != "comment" and self.page != verified.page_index:
            self.load_page(service, verified.page_index)
        return localized_command(service, context, annotation_id, verified, proposal,
                                 self.matcher, self.text_index, self.characters)

    def load_page(self, service: WorkerTextService, page_number: int) -> None:
        page = service.document[page_number]
        bounds = page_bounds(page)
        matcher = PageMatcher(extract_runs(page, bounds), bounds)
        snapshot = service.page_index(page_number)
        characters = tuple(char for chunk_index in range(len(snapshot.chunks))
                           for char in service.chunk(page_number, snapshot.page_text_id, chunk_index).characters)
        self.page, self.matcher, self.text_index, self.characters = page_number, matcher, snapshot, characters


def application_worker(conn, document_id: str, source: DocumentSource, plan: ApplicationPlan) -> None:
    worker_plan = WorkerPlan(document_id, source.identity, plan)
    run_document_worker(conn, document_id, source, prepare_annotation_command=worker_plan.prepare)
