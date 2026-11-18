from dataclasses import FrozenInstanceError
import json
import math
import subprocess
import sys

import pytest

from pdf_annotator.domain.annotation_commands import (
    CommandContext, CommandSource, CreateCallout, CreateDeleteText,
    CreateFreeText, CreateHighlight, CreateInk, CreateInsertText,
    CreateReplaceText, MoveInk,
    MoveFreeText, ResizeFreeText, UpdateFreeText,
    TransferFreeText,
)
from pdf_annotator.domain.annotation_models import AnnotationModel, AnnotationOwnership
from pdf_annotator.domain.errors import (DomainValidationError, MutationError,
                                         MutationErrorCode)
from pdf_annotator.domain.geometry import PdfPoint, PdfQuad, PdfRect
from pdf_annotator.domain.identifiers import PdfBackingReference
from pdf_annotator.domain.kinds import AnnotationKind
from pdf_annotator.domain.serialization import (
    annotation_from_payload, annotation_to_payload, canonical_json,
    command_from_payload, command_to_payload,
    result_from_payload, result_to_payload,
)
from pdf_annotator.domain.results import MutationOutcome, MutationResult
from pdf_annotator.domain.styles import FreeTextStyle, HighlightStyle, InkStyle, RgbColor
from pdf_annotator.domain.targets import (
    BoundaryAffinity, CalloutTarget, InkTarget, RectTarget,
    MAX_INK_POINTS, MAX_TEXT_QUADS, TextInsertionTarget, TextRangeTarget,
    clamp_ink_translation, translate_ink_target,
)


def _quad() -> PdfQuad:
    return PdfQuad(PdfPoint(10, 10), PdfPoint(80, 10),
                   PdfPoint(10, 24), PdfPoint(80, 24))


def commands(document_id="document", generation=1, source=CommandSource.UI):
    def context():
        return CommandContext.create(document_id, generation, source)

    text_range = lambda: TextRangeTarget.create(0, (_quad(),), "Original text")
    return (
        CreateInk(context(), InkTarget(0, ((PdfPoint(10, 10), PdfPoint(20, 20)),)),
                  InkStyle()),
        CreateHighlight(context(), text_range(), HighlightStyle()),
        CreateFreeText(context(), RectTarget(0, PdfRect(10, 30, 150, 80)),
                       "Plain text", FreeTextStyle()),
        CreateCallout(context(), CalloutTarget(0, PdfRect(100, 100, 240, 160),
                                               PdfPoint(50, 50), PdfPoint(80, 80)),
                      "Callout", FreeTextStyle()),
        CreateDeleteText(context(), text_range()),
        CreateInsertText(context(), TextInsertionTarget.create(
            0, PdfPoint(90, 20), BoundaryAffinity.AFTER, context_before="word"), "insert"),
        CreateReplaceText(context(), text_range(), "replacement"),
    )


def test_all_seven_commands_are_immutable_values_and_round_trip():
    for command in commands():
        payload = command_to_payload(command)
        assert command_from_payload(payload) == command
        assert json.loads(canonical_json(payload)) == payload
        with pytest.raises(FrozenInstanceError):
            command.context.document_generation = 2


def test_programmatic_ai_source_uses_same_source_independent_command_domain():
    command = commands("ai-document", source=CommandSource.AI)[1]
    assert isinstance(command, CreateHighlight)
    assert command.context.source == CommandSource.AI
    assert command_from_payload(command_to_payload(command)) == command


@pytest.mark.parametrize("constructor,args", [
    (PdfPoint, (math.nan, 0)),
    (PdfPoint, (math.inf, 0)),
    (PdfRect, (10, 10, 10, 20)),
    (RgbColor, (1.1, 0, 0)),
    (InkStyle, (RgbColor(0, 0, 0), 0, 1)),
    (HighlightStyle, (RgbColor(1, 1, 0), math.inf)),
])
def test_geometry_and_style_reject_invalid_or_nonfinite_values(constructor, args):
    with pytest.raises(ValueError):
        constructor(*args)


def test_text_targets_are_selection_derived_and_not_raster_dependent():
    ranged = commands()[1].target
    inserted = commands()[5].target
    assert ranged.original_text == "Original text" and ranged.quads == (_quad(),)
    assert inserted.context_before == "word" and inserted.affinity == BoundaryAffinity.AFTER
    with pytest.raises(ValueError):
        TextInsertionTarget.create(0, PdfPoint(1, 1), BoundaryAffinity.BEFORE)
    with pytest.raises(ValueError):
        CreateInsertText(CommandContext.create("document", 1), inserted, "bad\x00text")


def test_variable_size_targets_are_explicitly_bounded_for_ipc():
    point = PdfPoint(1, 1)
    with pytest.raises(ValueError):
        InkTarget(0, ((point,) * (MAX_INK_POINTS + 1),))
    with pytest.raises(ValueError):
        TextRangeTarget.create(0, (_quad(),) * (MAX_TEXT_QUADS + 1), "text")


def test_move_ink_round_trips_and_clamps_the_whole_path_at_page_edges():
    target = InkTarget(0, ((PdfPoint(2, 3), PdfPoint(15, 8), PdfPoint(9, 18)),))
    command = MoveInk(CommandContext.create("document", 1), commands()[0].annotation_id,
                      0, -100, 100)
    assert command_from_payload(command_to_payload(command)) == command
    delta = clamp_ink_translation(target, 100, 80, command.delta_x, command.delta_y)
    assert delta == (-2.0, 62.0)
    translated = translate_ink_target(target, *delta)
    assert translated.paths == ((PdfPoint(0, 65), PdfPoint(13, 70), PdfPoint(7, 80)),)
    with pytest.raises(ValueError):
        MoveInk(CommandContext.create("document", 1), commands()[0].annotation_id,
                0, math.inf, 0)
    with pytest.raises(ValueError):
        MoveInk(CommandContext.create("document", 1), commands()[0].annotation_id, 0, 0, 0)


def test_freetext_update_move_and_resize_are_explicit_typed_commands():
    create = commands()[2]
    updates = (
        UpdateFreeText(CommandContext.create("document", 1), create.annotation_id, 0,
                       "Edited", FreeTextStyle(font_size=13)),
        MoveFreeText(CommandContext.create("document", 1), create.annotation_id, 0, 5, -2),
        ResizeFreeText(CommandContext.create("document", 1), create.annotation_id,
                       RectTarget(0, PdfRect(20, 40, 180, 120))),
        TransferFreeText(CommandContext.create("document", 1), create.annotation_id, 0,
                         RectTarget(1, PdfRect(20, 40, 180, 120))),
    )
    for command in updates:
        assert command_from_payload(command_to_payload(command)) == command


def test_freetext_font_style_is_bounded_to_pdf_base_families_and_pdf_point_range():
    assert FreeTextStyle(font_size=6, font_family="Courier").font_family == "Courier"
    with pytest.raises(ValueError):
        FreeTextStyle(font_size=5.9)
    with pytest.raises(ValueError):
        FreeTextStyle(font_family="System Font")


def test_unknown_and_malformed_command_payloads_are_typed_failures():
    payload = command_to_payload(commands()[0])
    payload["kind"] = "execute_anything"
    with pytest.raises(DomainValidationError) as unknown:
        command_from_payload(payload)
    assert unknown.value.code == MutationErrorCode.UNSUPPORTED_COMMAND
    payload.pop("document_id")
    with pytest.raises(DomainValidationError) as malformed:
        command_from_payload(payload)
    assert malformed.value.code == MutationErrorCode.INVALID_COMMAND


def test_annotation_identity_is_distinct_from_pdf_backing_and_serializable():
    command = commands()[2]
    model = AnnotationModel(command.annotation_id, command.context.document_id, 1,
                            AnnotationKind.TEXT_BOX, command.target, command.text,
                            command.style, AnnotationOwnership.CREATED_BY_APP,
                            command.context.source,
                            PdfBackingReference("app-nm", 19, (20, 21)))
    restored = annotation_from_payload(annotation_to_payload(model))
    assert restored == model
    assert restored.annotation_id.value != str(restored.backing.xref)
    assert restored.editable
    external = AnnotationModel(commands()[1].annotation_id, "document", 1,
                               AnnotationKind.HIGHLIGHT, commands()[1].target, None,
                               HighlightStyle(), AnnotationOwnership.EXTERNAL_READ_ONLY,
                               None, PdfBackingReference(xref=99))
    assert not external.editable


def test_typed_mutation_result_and_error_round_trip():
    command = commands()[0]
    success = MutationResult(command.context.command_id, command.context.document_id, 1,
                             MutationOutcome.SUCCESS, 4, 5, command.annotation_id)
    assert result_from_payload(result_to_payload(success)) == success
    failure = MutationResult(command.context.command_id, command.context.document_id, 1,
                             MutationOutcome.FAILED, 4, 4,
                             error=MutationError.create(MutationErrorCode.INVALID_GEOMETRY))
    assert result_from_payload(result_to_payload(failure)) == failure


def test_domain_imports_headlessly_without_qt_pymupdf_or_rendering():
    script = (
        "import sys; import pdf_annotator.domain.annotation_commands; "
        "import pdf_annotator.services.annotation_service; "
        "assert 'PySide6' not in sys.modules; assert 'pymupdf' not in sys.modules; "
        "assert not any(name.startswith('pdf_annotator.viewer') for name in sys.modules); print('PASS')"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=15, check=True)
    assert result.stdout.strip() == "PASS"
