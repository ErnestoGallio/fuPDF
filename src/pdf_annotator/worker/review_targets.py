"""Build native annotation commands from localized and worker-confirmed targets."""

from typing import Any

from ..domain.annotation_commands import (
    AnnotationCommand, CommandContext, CreateDeleteText, CreateInsertText, CreateReplaceText, CreateStickyNote,
)
from ..domain.editorial_metadata import EditorialMetadata
from ..domain.errors import DomainValidationError, MutationErrorCode
from ..domain.identifiers import AnnotationId, new_command_id
from ..domain.serialization import point_from_payload
from ..domain.text import TextCharacter, TextPageIndex
from .review_localization import Localization
from .review_localization_text import Match, PageMatcher
from .sticky_note_adapter import NOTE_AUTHOR, note_target
from .text_service import WorkerTextService


def locate_glyph(characters: tuple[TextCharacter, ...], expected: TextCharacter, *, match_block: bool = True) -> int:
    matches = [char.index for char in characters if char.text == expected.text
               and (not match_block or char.block_index == expected.block_index) and char.quad == expected.quad]
    if len(matches) != 1:
        raise DomainValidationError(MutationErrorCode.STALE_TEXT_TARGET, reason="glyph_not_unique_in_snapshot")
    return matches[0]


def localized_command(service: WorkerTextService, context: CommandContext, annotation_id: AnnotationId,
                      verified: Localization, proposal: dict[str, Any],
                      matcher: PageMatcher | None = None, snapshot: TextPageIndex | None = None,
                      characters: tuple[TextCharacter, ...] = (), *, match_block: bool = True
                      ) -> AnnotationCommand:
    page_number = verified.page_index
    if verified.operation == "comment":
        target = note_target(service.document[page_number], point_from_payload(verified.comment_position))
        return CreateStickyNote(context, target, proposal["reason"], annotation_id)

    if matcher is None or snapshot is None or verified.source_range is None:
        raise DomainValidationError(MutationErrorCode.STALE_TEXT_TARGET)
    source = verified.source_range
    match = Match(source["run_index"], source["start"], source["end_exclusive"])
    glyphs, quads = matcher.geometry(match, new_command_id(), collapsed_whitespace=False)
    if not glyphs:
        raise DomainValidationError(MutationErrorCode.STALE_TEXT_TARGET)
    metadata = EditorialMetadata(proposal["reason"], NOTE_AUTHOR)
    if verified.operation == "insert":
        boundary = verified.insertion
        side = boundary["side"]
        glyph = glyphs[0] if side == "before" else glyphs[-1]
        char_index = locate_glyph(characters, glyph, match_block=match_block)
        status, target = service.resolve_insertion({"page_index": page_number,
            "page_text_id": snapshot.page_text_id, "char_index": char_index, "affinity": side})
        if (status != "confirmed" or target is None
                or target.anchor != point_from_payload(boundary["point"])
                or target.boundary_top != point_from_payload(boundary["edge_top"])):
            raise DomainValidationError(MutationErrorCode.STALE_TEXT_TARGET)
        return CreateInsertText(context, target, proposal["replacement_text"], annotation_id, metadata)

    first = locate_glyph(characters, glyphs[0], match_block=match_block)
    last = locate_glyph(characters, glyphs[-1], match_block=match_block)
    target = service.confirm_range(page_number, snapshot.page_text_id, first, last)
    if target.original_text != verified.matched_text or target.quads != quads:
        raise DomainValidationError(MutationErrorCode.STALE_TEXT_TARGET,
                                    reason="worker_range_differs_from_verified_glyphs")
    if verified.operation == "delete":
        return CreateDeleteText(context, target, annotation_id, metadata)
    return CreateReplaceText(context, target, proposal["replacement_text"], annotation_id, metadata)
