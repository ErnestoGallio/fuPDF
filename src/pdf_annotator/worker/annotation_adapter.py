"""M7 product adapters selected behind one authoritative worker boundary."""

from dataclasses import dataclass, replace
from contextlib import contextmanager, ExitStack
from typing import Any, Iterator

from ..domain.annotation_commands import (AnnotationCommand, CreateFreeText, CreateCallout, CreateHighlight, CreateInk,
                                           DeleteAnnotation, DeleteExternalAnnotation, MoveFreeText, MoveInk,
                                           ResizeFreeText, TransferFreeText, UpdateFreeText)
from ..domain.annotation_commands import (MoveCallout, MoveCalloutAnchor, ResizeCallout,
                                           TransferCallout, UpdateCallout)
from ..domain.annotation_models import AnnotationModel
from ..domain.results import HistoryDelta
from ..domain.annotation_inventory import AnnotationSummary
from ..domain.kinds import AnnotationKind
from ..domain.errors import DomainValidationError, MutationErrorCode
from .annotation_dispatch import AppliedMutation
from .highlight_adapter import (apply_annotation_command as apply_highlight_command,
                                delete_highlight)
from .ink_adapter import create_ink, delete_ink, move_ink
from .freetext_adapter import (create_free_text, delete_free_text, move_free_text,
                               resize_free_text, transfer_free_text, update_free_text)
from .callout_adapter import apply_callout, create_callout, delete_callout
from .text_service import WorkerTextService
from ..domain.annotation_commands import CreateDeleteText, CreateInsertText, CreateReplaceText, CreateStickyNote
from .editorial_adapter import EDITORIAL_KINDS, create_editorial, delete_editorial
from .editorial_adapter import _page_transaction
from .managed_corrections import collect_managed, has_marker
from .external_delete import (ExternalDeleteSnapshot, NativeAnnotationSnapshot,
                              delete_external_annotation, restore_external_annotation,
                              restore_native_annotation, snapshot_external_annotation,
                              snapshot_native_annotation)
from .annotation_inventory import NativeAnnotationInventory
from ..domain.external_edits import EditExternalAnnotation
from .external_edit_adapter import apply_external_edit, authorize
from .sticky_note_adapter import create_sticky_note

TRANSACTIONAL_KINDS = (*EDITORIAL_KINDS, AnnotationKind.STICKY_NOTE)


@dataclass(frozen=True, slots=True)
class ProductHistoryEntry:
    before: AnnotationModel | None
    after: AnnotationModel | None
    before_native: NativeAnnotationSnapshot | None = None
    after_native: NativeAnnotationSnapshot | None = None
    external: ExternalDeleteSnapshot | None = None
    external_summary: AnnotationSummary | None = None
    external_edit_before: AnnotationSummary | None = None
    external_edit_after: AnnotationSummary | None = None
    before_group: tuple[AnnotationSummary, ...] = ()
    after_group: tuple[AnnotationSummary, ...] = ()

    @property
    def byte_size(self) -> int:
        # DTOs are small compared with native /AP and ink payloads.  The
        # conservative repr accounting avoids unbounded state without sending
        # any worker data to the GUI process.
        return sum(len(repr(item).encode("utf-8")) for item in (self.before, self.after,
                   self.external_summary, self.external_edit_before, self.external_edit_after)
                   if item is not None) + sum(item.byte_size for item in
                   (self.before_native, self.after_native) if item is not None) + (
                   self.external.byte_size if self.external else 0) + len(repr((self.before_group, self.after_group)).encode("utf-8"))


class ProductAnnotationAdapter:
    """Per-document adapter state used to authorize app-created Ink deletion."""

    def __init__(self, text_service: WorkerTextService) -> None:
        self._text_service = text_service
        self._owned: dict[str, AnnotationModel] = {}

    def rebind(self, text_service: WorkerTextService, generation: int) -> None:
        self._text_service = text_service
        self._owned = {key: replace(model, document_generation=generation)
                       for key, model in self._owned.items()}

    def review_owned(self, annotation_id: str) -> AnnotationModel | None:
        return self._owned.get(annotation_id)

    def review_editorial_on_page(self, page_index: int) -> Iterator[AnnotationModel]:
        return (model for model in self._owned.values()
                if model.page_index == page_index and model.kind in EDITORIAL_KINDS)

    @contextmanager
    def atomic_mutation(self, document: Any, command: AnnotationCommand,
                        entry: ProductHistoryEntry | None = None):
        """Extend the existing page transaction through dispatcher finalization."""
        from ..domain.annotation_commands import Undo
        owned = self._owned.copy()
        model = owned.get(getattr(getattr(command, "annotation_id", None), "value", ""))
        transactional = isinstance(command, (CreateDeleteText, CreateInsertText, CreateReplaceText, CreateStickyNote))
        transactional = transactional or (model is not None and model.kind in TRANSACTIONAL_KINDS)
        native_group = None
        if isinstance(command, DeleteExternalAnnotation):
            root = int(command.logical_id.rsplit(":", 1)[1])
            if has_marker(document, root):
                native_group = collect_managed(document, command.page_index, root)
                transactional = True
        if entry is not None:
            transactional = bool(entry.before_group or entry.after_group) or any(
                item is not None and item.kind in TRANSACTIONAL_KINDS for item in (entry.before, entry.after))
            active = entry.after if isinstance(command, Undo) else entry.before
            if active is not None and active.metadata is not None and active.metadata.reason is not None:
                collect_managed(document, active.page_index, active.backing.xref)
            if entry.external is not None and not isinstance(command, Undo) and entry.before_group:
                page_index = entry.before_group[0].page_index
                root = int(entry.external.logical_id.rsplit(":", 1)[1])
                collect_managed(document, page_index, root)
        if not transactional:
            yield
            return
        pages: set[int] = set()
        components: set[int] = set(self._backing_xrefs(model))
        target_page = getattr(getattr(command, "target", None), "page_index", None)
        explicit_page = getattr(command, "page_index", None)
        pages.update(number for number in (target_page, explicit_page) if type(number) is int)
        if model is not None:
            pages.add(model.page_index)
        if native_group is not None:
            pages.add(native_group.page_index)
            components.update(native_group.components)
        if entry is not None:
            snapshots = (entry.before_native, entry.after_native,
                         entry.external.native if entry.external else None)
            for snapshot in snapshots:
                if snapshot is not None:
                    pages.update(number for number, *_rest in snapshot.pages)
                    components.update(xref for xref, _source in snapshot.component_sources)
        try:
            with ExitStack() as stack:
                for number in sorted(pages):
                    stack.enter_context(_page_transaction(document, document.load_page(number), tuple(components)))
                yield
        except BaseException:
            self._owned = owned
            raise

    @staticmethod
    def _group_rows(document: Any, model: AnnotationModel | None) -> tuple[AnnotationSummary, ...]:
        if model is None:
            return ()
        logical_id = f"native:{model.page_index}:{model.backing.xref}" if model.backing else None
        if model.kind == AnnotationKind.STICKY_NOTE and logical_id:
            return (NativeAnnotationInventory(document).summary_for_logical_id(logical_id),)
        if model.metadata is None or model.metadata.reason is None:
            return ()
        return NativeAnnotationInventory(document).group_summaries(logical_id)

    def __call__(self, document: Any, command: AnnotationCommand) -> AppliedMutation:
        if isinstance(command, EditExternalAnnotation):
            apply_external_edit(document, command)
            summary = NativeAnnotationInventory(document).summary_for_edit(command.logical_id)
            return AppliedMutation(history_delta=HistoryDelta(inventory_updated=(summary,),
                appearance_invalidated_ids=(summary.logical_id,)))
        if isinstance(command, (CreateDeleteText, CreateInsertText, CreateReplaceText)):
            applied = create_editorial(document, command, self._text_service)
            self._owned[command.annotation_id.value] = applied.annotation
            rows = self._group_rows(document, applied.annotation)
            if rows:
                return replace(applied, history_delta=HistoryDelta(added=(applied.annotation,), inventory_added=rows))
            return applied
        if isinstance(command, CreateStickyNote):
            applied = create_sticky_note(document, command)
            model = applied.annotation
            self._owned[command.annotation_id.value] = model
            return replace(applied, history_delta=HistoryDelta(
                added=(model,), inventory_added=self._group_rows(document, model)))
        if isinstance(command, CreateInk):
            applied = create_ink(document, command)
            if applied.annotation is not None:
                self._owned[command.annotation_id.value] = applied.annotation
            return applied
        if isinstance(command, MoveInk):
            model = self._owned.get(command.annotation_id.value)
            if model is None:
                raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
            if model.page_index != command.page_index:
                raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
            applied = move_ink(document, command, model)
            if applied.annotation is not None:
                self._owned[command.annotation_id.value] = applied.annotation
            return applied
        if isinstance(command, CreateFreeText):
            applied = create_free_text(document, command)
            if applied.annotation is not None:
                self._owned[command.annotation_id.value] = applied.annotation
            return applied
        if isinstance(command, CreateCallout):
            applied = create_callout(document, command)
            if applied.annotation is not None:
                self._owned[command.annotation_id.value] = applied.annotation
            return applied
        if isinstance(command, CreateHighlight):
            applied = apply_highlight_command(document, command, self._text_service)
            if applied.annotation is not None:
                self._owned[command.annotation_id.value] = applied.annotation
            return applied
        if isinstance(command, (UpdateCallout, MoveCallout, ResizeCallout,
                                MoveCalloutAnchor, TransferCallout)):
            model = self._owned.get(command.annotation_id.value)
            if model is None or model.kind != AnnotationKind.CALLOUT:
                raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
            applied = apply_callout(document, command, model)
            if applied.annotation is not None:
                self._owned[command.annotation_id.value] = applied.annotation
            return applied
        if isinstance(command, (UpdateFreeText, MoveFreeText, ResizeFreeText, TransferFreeText)):
            model = self._owned.get(command.annotation_id.value)
            if model is None or model.kind != AnnotationKind.TEXT_BOX:
                raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
            if isinstance(command, UpdateFreeText):
                applied = update_free_text(document, command, model)
            elif isinstance(command, MoveFreeText):
                if model.page_index != command.page_index:
                    raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
                applied = move_free_text(document, command, model)
            elif isinstance(command, TransferFreeText):
                if model.page_index != command.source_page_index:
                    raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
                applied = transfer_free_text(document, command, model)
            else:
                if model.page_index != command.target.page_index:
                    raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
                applied = resize_free_text(document, command, model)
            if applied.annotation is not None:
                self._owned[command.annotation_id.value] = applied.annotation
            return applied
        if isinstance(command, DeleteAnnotation):
            model = self._owned.get(command.annotation_id.value)
            if model is None:
                raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
            if model.page_index != command.page_index:
                raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
            if model.kind in EDITORIAL_KINDS:
                rows = self._group_rows(document, model)
                applied = delete_editorial(document, command, model)
                self._owned.pop(command.annotation_id.value)
                if rows:
                    return replace(applied, history_delta=HistoryDelta(
                        removed_ids=(model.annotation_id.value,),
                        inventory_removed_ids=tuple(row.logical_id for row in rows)))
                return applied
            if model.kind == AnnotationKind.STICKY_NOTE:
                row, = self._group_rows(document, model)
                delete_external_annotation(document, row.logical_id)
                self._owned.pop(command.annotation_id.value)
                return AppliedMutation(history_delta=HistoryDelta(
                    removed_ids=(model.annotation_id.value,), inventory_removed_ids=(row.logical_id,)))
            applied = (delete_ink(document, command) if model.kind == AnnotationKind.INK
                       else delete_highlight(document, command) if model.kind == AnnotationKind.HIGHLIGHT
                       else delete_callout(document, command) if model.kind == AnnotationKind.CALLOUT
                       else delete_free_text(document, command))
            self._owned.pop(command.annotation_id.value, None)
            return applied
        if isinstance(command, DeleteExternalAnnotation):
            root = int(command.logical_id.rsplit(":", 1)[1])
            rows = (NativeAnnotationInventory(document).group_summaries(command.logical_id)
                    if has_marker(document, root) else ())
            delete_external_annotation(document, command.logical_id)
            return AppliedMutation(history_delta=HistoryDelta(
                inventory_removed_ids=tuple(row.logical_id for row in rows)) if rows else None)
        return apply_highlight_command(document, command, self._text_service)

    def history_before(self, document: Any, command: AnnotationCommand) -> ProductHistoryEntry:
        if isinstance(command, EditExternalAnnotation):
            _page, annot, _profile = authorize(document, command)
            return ProductHistoryEntry(None, None,
                before_native=snapshot_native_annotation(
                    document, (command.page_index,), (annot.xref,), include_normal_appearance=True),
                external_edit_before=NativeAnnotationInventory(document).summary_for_edit(command.logical_id))
        if isinstance(command, DeleteExternalAnnotation):
            root = int(command.logical_id.rsplit(":", 1)[1])
            rows = (NativeAnnotationInventory(document).group_summaries(command.logical_id)
                    if has_marker(document, root) else ())
            return ProductHistoryEntry(
                None, None,
                external=snapshot_external_annotation(document, command.logical_id),
                external_summary=rows[0] if rows else NativeAnnotationInventory(document).summary_for_logical_id(
                    command.logical_id), before_group=rows)
        annotation_id = getattr(command, "annotation_id", None)
        before = self._owned.get(annotation_id.value) if annotation_id else None
        return ProductHistoryEntry(before, None,
                                   before_native=self._snapshot_for(document, command, before),
                                   before_group=self._group_rows(document, before))

    def history_after(self, document: Any, before: ProductHistoryEntry,
                      applied: AppliedMutation) -> ProductHistoryEntry:
        if before.external_edit_before is not None:
            summary = applied.history_delta.inventory_updated[0]
            if before.before_native is None:
                raise DomainValidationError(MutationErrorCode.INTERNAL_PDF_MUTATION)
            # A layout edit leaves the imported AP allocated but no longer
            # referenced. It is still part of the native document state and
            # must survive a redo; otherwise restore_native_annotation treats
            # it as a newly-created object and nulls it.
            prior_components = tuple(xref for xref, _source
                                     in before.before_native.component_sources)
            return replace(before, external_edit_after=summary,
                after_native=snapshot_native_annotation(document, (summary.page_index,),
                    (int(summary.logical_id.rsplit(":", 1)[1]), *prior_components),
                    include_normal_appearance=True))
        if before.external is not None:
            return before
        return ProductHistoryEntry(before.before, applied.annotation, before.before_native,
                                   self._snapshot_for(document, None, applied.annotation,
                                                      before.before_native),
                                   before_group=before.before_group,
                                   after_group=self._group_rows(document, applied.annotation))

    def rollback_history_before(self, document: Any, entry: ProductHistoryEntry) -> None:
        """Undo an unacknowledged external write if post-write adoption fails.

        ``apply_external_edit`` already restores its own failures.  This second
        guard covers failures while making the incremental inventory delta or
        history snapshot: those happen after the PDF write but before the
        dispatcher acknowledges a revision.
        """
        if entry.external_edit_before is None or entry.before_native is None:
            return
        pages = tuple(page_index for page_index, _xref, _kind, _value
                      in entry.before_native.pages)
        root = int(entry.external_edit_before.logical_id.rsplit(":", 1)[1])
        current = snapshot_native_annotation(document, pages, (root,),
                                             include_normal_appearance=True)
        restore_native_annotation(document, entry.before_native, current)

    def undo_history(self, document: Any, entry: ProductHistoryEntry) -> AnnotationModel | None:
        if entry.external_edit_before is not None:
            restore_native_annotation(document, entry.before_native, entry.after_native)
            return None
        if entry.external is not None:
            restore_external_annotation(document, entry.external)
            return None
        return self._restore_owned_state(document, entry.before, entry.after,
                                         entry.before_native, entry.after_native)

    def redo_history(self, document: Any, entry: ProductHistoryEntry) -> AnnotationModel | None:
        if entry.external_edit_before is not None:
            restore_native_annotation(document, entry.after_native, entry.before_native)
            return None
        if entry.external is not None:
            delete_external_annotation(document, entry.external.logical_id)
            return None
        return self._restore_owned_state(document, entry.after, entry.before,
                                         entry.after_native, entry.before_native)

    @staticmethod
    def history_delta(entry: ProductHistoryEntry, *, undo: bool) -> HistoryDelta:
        """Return the one logical object affected by this exact native restore."""
        if entry.external_edit_before is not None:
            summary = entry.external_edit_before if undo else entry.external_edit_after
            return HistoryDelta(inventory_updated=(summary,),
                                appearance_invalidated_ids=(summary.logical_id,))
        if entry.external is not None:
            if entry.external_summary is None:
                raise DomainValidationError(MutationErrorCode.INTERNAL_PDF_MUTATION)
            logical_id = entry.external.logical_id
            rows = entry.before_group or (entry.external_summary,)
            return (HistoryDelta(inventory_added=rows,
                                 appearance_invalidated_ids=(logical_id,)) if undo else
                    HistoryDelta(inventory_removed_ids=tuple(row.logical_id for row in rows),
                                 appearance_invalidated_ids=(logical_id,)))
        desired = entry.before if undo else entry.after
        opposite = entry.after if undo else entry.before
        if desired is None and opposite is None:
            raise DomainValidationError(MutationErrorCode.INTERNAL_PDF_MUTATION)
        if desired is None:
            rows = entry.after_group if undo else entry.before_group
            return HistoryDelta(removed_ids=(opposite.annotation_id.value,),
                                inventory_removed_ids=tuple(row.logical_id for row in rows))
        if opposite is None:
            rows = entry.before_group if undo else entry.after_group
            return HistoryDelta(added=(desired,), inventory_added=rows)
        return HistoryDelta(updated=(desired,))

    @staticmethod
    def _backing_xrefs(model: AnnotationModel | None) -> tuple[int, ...]:
        if model is None or model.backing is None or model.backing.xref is None:
            return ()
        return (model.backing.xref, *model.backing.associated_xrefs)

    def _snapshot_for(self, document: Any, command: AnnotationCommand | None,
                      model: AnnotationModel | None,
                      prior: NativeAnnotationSnapshot | None = None) -> NativeAnnotationSnapshot:
        pages = {index for index, _xref, _kind, _value in prior.pages} if prior else set()
        if model is not None:
            pages.add(model.page_index)
        if command is not None:
            target = getattr(command, "target", None)
            page_index = getattr(target, "page_index", None)
            if type(page_index) is int:
                pages.add(page_index)
            source_page = getattr(command, "source_page_index", None)
            if type(source_page) is int:
                pages.add(source_page)
            explicit_page = getattr(command, "page_index", None)
            if type(explicit_page) is int:
                pages.add(explicit_page)
        return snapshot_native_annotation(document, pages, self._backing_xrefs(model))

    def _restore_owned_state(self, document: Any, desired: AnnotationModel | None,
                             opposite: AnnotationModel | None,
                             native: NativeAnnotationSnapshot | None,
                             opposite_native: NativeAnnotationSnapshot | None) -> AnnotationModel | None:
        if native is None or opposite_native is None:
            raise DomainValidationError(MutationErrorCode.INTERNAL_PDF_MUTATION)
        identity_model = desired if desired is not None else opposite
        if identity_model is None:
            raise DomainValidationError(MutationErrorCode.INTERNAL_PDF_MUTATION)
        restore_native_annotation(document, native, opposite_native)
        if desired is None:
            self._owned.pop(identity_model.annotation_id.value, None)
            return None
        self._owned[desired.annotation_id.value] = desired
        return desired

def apply_annotation_command(document: Any, command: AnnotationCommand,
                             text_service: WorkerTextService) -> AppliedMutation:
    """Single-command compatibility entry point used by focused adapter tests."""
    if isinstance(command, CreateInk):
        return create_ink(document, command)
    if isinstance(command, CreateFreeText):
        return create_free_text(document, command)
    if isinstance(command, CreateCallout):
        return create_callout(document, command)
    return apply_highlight_command(document, command, text_service)
