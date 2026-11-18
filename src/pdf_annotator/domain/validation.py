"""Shared pure validation used by the application service and worker."""

from collections.abc import Callable

from .annotation_commands import (AnnotationCommand, DeleteAnnotation, DeleteExternalAnnotation, MoveFreeText, MoveInk,
                                  TransferFreeText, UpdateFreeText, MoveCallout, UpdateCallout,
                                  TransferCallout, command_page)
from .annotation_commands import Undo, Redo
from .errors import DomainValidationError, MutationErrorCode
from .geometry import PageBounds
from .targets import target_points
from .external_edits import EditExternalAnnotation
from .geometry import PdfPoint


def validate_command_target(command: AnnotationCommand,
                            bounds_for_page: Callable[[int], PageBounds]) -> None:
    if isinstance(command, (Undo, Redo)):
        return
    try:
        page = command_page(command)
        bounds = bounds_for_page(page)
    except (IndexError, KeyError):
        raise DomainValidationError(MutationErrorCode.PAGE_OUT_OF_RANGE) from None
    except DomainValidationError:
        raise
    except (TypeError, ValueError):
        raise DomainValidationError(MutationErrorCode.PAGE_OUT_OF_RANGE) from None
    if not isinstance(bounds, PageBounds):
        raise DomainValidationError(MutationErrorCode.PAGE_OUT_OF_RANGE)
    if isinstance(command, EditExternalAnnotation):
        # An imported PDF annotation is a valid native baseline even when its
        # Rect, RD or callout leader extends beyond the CropBox.  External
        # operations validate only their own finite typed payload; creation
        # commands retain the ordinary in-page target validation below.
        return
    if isinstance(command, (TransferFreeText, TransferCallout)):
        try:
            source_bounds = bounds_for_page(command.source_page_index)
        except (IndexError, KeyError, TypeError, ValueError):
            raise DomainValidationError(MutationErrorCode.PAGE_OUT_OF_RANGE) from None
        if not isinstance(source_bounds, PageBounds):
            raise DomainValidationError(MutationErrorCode.PAGE_OUT_OF_RANGE)
    if isinstance(command, (DeleteAnnotation, DeleteExternalAnnotation, MoveInk, MoveFreeText, UpdateFreeText,
                            MoveCallout, UpdateCallout)):
        return
    if any(not bounds.contains(point) for point in target_points(command.target)):
        raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY)
