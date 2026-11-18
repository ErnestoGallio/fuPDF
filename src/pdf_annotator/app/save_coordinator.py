"""GUI save barrier, destination reservation and response adoption; no file I/O."""

from dataclasses import asdict, dataclass, replace
import os
from time import perf_counter
from typing import TYPE_CHECKING

from ..util.paths import FileIdentity, SaveTarget
from ..worker.protocol import ErrorInfo, Operation
from .document_session import DocumentSession, Lifecycle
from .file_resolver import MAX_PENDING_OPENS, ResolvedFile

if TYPE_CHECKING:
    from .document_manager import DocumentManager


@dataclass
class PendingSave:
    token: int
    document_id: str
    overwrite: bool
    save_as: bool
    target: SaveTarget | None = None
    awaiting_confirmation: bool = False
    requested_at: float = 0.0
    barrier_ready_at: float | None = None


class SaveCoordinator:
    def __init__(self, manager: "DocumentManager") -> None:
        self.manager = manager
        self.pending: dict[str, PendingSave] = {}

    def request(self, document_id: str, path: str | None = None, *, overwrite: bool = False) -> bool:
        manager = self.manager
        session = manager.sessions.get(document_id)
        if (session is None or session.lifecycle != Lifecycle.READY or session.saving
                or manager.shutting_down or len(manager._pending) + len(self.pending) >= MAX_PENDING_OPENS):
            return False
        manager._request_serial += 1
        token = manager._request_serial
        self.pending[document_id] = PendingSave(token, document_id, overwrite, path is not None,
                                                requested_at=perf_counter())
        session.saving = True
        session.worker.pause_review(True)
        session.worker.cancel_renders()
        session.worker.cancel_annotation_appearances()
        manager._resolver.submit(token, path or session.canonical_path, destination=True)
        manager.session_changed.emit(document_id)
        return True

    def resolved(self, result: ResolvedFile) -> bool:
        pending = next((item for item in self.pending.values() if item.token == result.request_id), None)
        if pending is None:
            return False
        session = self.manager.sessions[pending.document_id]
        if result.error is not None:
            self._finish(session, False, result.error)
            return True
        target = result.target
        path_key = os.path.normcase(target.canonical_path)
        existing = target.existing
        other = self.manager._by_path.get(path_key)
        if other is None and existing is not None and existing.file_key is not None:
            other = self.manager._by_file.get(existing.file_key)
        if other is not None and other != session.document_id:
            self._finish(session, False, ErrorInfo("SAVE_TARGET_OPEN", "La destinazione è già aperta in un altro documento."))
            return True
        pending.target = target
        # Reserve both canonical spelling and inode until success/failure. This
        # covers simultaneous Save As and Open, including hard links and case.
        self.manager._by_path[path_key] = session.document_id
        if existing is not None and existing.file_key is not None:
            self.manager._by_file[existing.file_key] = session.document_id
        if pending.save_as and existing is not None and not pending.overwrite:
            pending.awaiting_confirmation = True
            self.manager.save_overwrite_required.emit(session.document_id, target.canonical_path)
        return True

    def confirm_overwrite(self, document_id: str, confirmed: bool) -> None:
        pending = self.pending.get(document_id)
        if pending is None or not pending.awaiting_confirmation:
            return
        if not confirmed:
            self._finish(self.manager.sessions[document_id], False)
            return
        pending.overwrite = True
        pending.awaiting_confirmation = False

    def poll(self, session: DocumentSession) -> None:
        pending = self.pending.get(session.document_id)
        if pending is None:
            return
        worker = session.worker
        if session.pending_mutations:
            return
        if pending.barrier_ready_at is None:
            pending.barrier_ready_at = perf_counter()
        if session.save_request_id is None:
            # Review replies have their own consumer and stay pending until it
            # adopts the file handles. Drain the ordinary pre-barrier reads.
            while (delivery := worker.poll(include_review=False)) is not None:
                delivery.release()
            session.inventory_request_id = None
            session.inventory_operation = None
            if worker.pending_count or pending.target is None or pending.awaiting_confirmation:
                return
            session.save_request_id = worker.submit(Operation.SAVE, {
                "target": asdict(pending.target), "revision": session.document_revision,
                "overwrite": pending.overwrite})
            return
        delivery = worker.poll(session.save_request_id)
        if delivery is None:
            return
        with delivery:
            response = delivery.response
            if (response.document_id != session.document_id or response.operation != Operation.SAVE
                    or response.document_generation != session.document_generation
                    or response.request_id != session.save_request_id):
                self.manager._fail(session, ErrorInfo("SAVE_PROTOCOL", "Risposta di salvataggio non valida."))
                return
            payload = response.payload
            timings = payload.get("timing_ms")
            if isinstance(timings, dict):
                session.last_save_timing_ms = {
                    key: float(value) for key, value in timings.items()
                    if isinstance(key, str) and isinstance(value, (int, float))
                }
                if pending.barrier_ready_at is not None:
                    session.last_save_timing_ms["mutation_barrier_wait"] = (
                        pending.barrier_ready_at - pending.requested_at) * 1000
                session.last_save_timing_ms["gui_save_round_trip"] = (
                    perf_counter() - pending.requested_at) * 1000
            generation = payload.get("new_generation")
            if generation is not None:
                if type(generation) is not int or generation != session.document_generation + 1:
                    self.manager._fail(session, ErrorInfo("SAVE_PROTOCOL", "Generazione di salvataggio non valida."))
                    return
                session.document_generation = generation
                session.annotations = {key: replace(model, document_generation=generation)
                                       for key, model in session.annotations.items()}
                self.manager.text_cache.remove_document(session.document_id)
                session.annotation_details.clear()
                session.annotation_inventory.clear()
                session.inventory_cursor = 0
                session.inventory_complete = False
                session.inventory_reload_pending = False
                session.inventory_detail_id = None
                session.inventory_detail_buffer = ""
                self.manager._seed_app_inventory(session)
                if "identity" in payload:
                    self._change_identity(session, FileIdentity(**payload["identity"]))
            if response.success:
                if payload.get("saved_revision") != session.document_revision or generation is None:
                    self.manager._fail(session, ErrorInfo("SAVE_PROTOCOL", "Revisione salvata non valida."))
                    return
                session.saved_revision = payload["saved_revision"]
                session.saved_history_state_id = session.history_state_id
            if payload.get("unusable"):
                self.manager._fail(session, response.error)
                return
            self._finish(session, response.success, response.error)
            if generation is not None:
                self.manager.document_reopened.emit(session.document_id)
                self.manager.inventory_changed.emit(session.document_id)
            self.manager.start_annotation_inventory(session.document_id)

    def _change_identity(self, session: DocumentSession, identity: FileIdentity) -> None:
        manager = self.manager
        for mapping in (manager._by_path, manager._by_file):
            for key, owner in tuple(mapping.items()):
                if owner == session.document_id:
                    del mapping[key]
        session.identity = identity
        manager._by_path[identity.path_key] = session.document_id
        if identity.file_key is not None:
            manager._by_file[identity.file_key] = session.document_id

    def _finish(self, session: DocumentSession, success: bool, error: ErrorInfo | None = None) -> None:
        self.pending.pop(session.document_id, None)
        self._change_identity(session, session.identity)
        session.saving = False
        session.save_request_id = None
        session.worker.pause_review(False)
        if error is not None:
            self.manager._report(session.document_id, error)
        self.manager.session_changed.emit(session.document_id)
        self.manager.save_finished.emit(session.document_id, success)

    def fail(self, session: DocumentSession) -> None:
        if session.document_id in self.pending:
            self._finish(session, False)
