"""Snapshot proof, live reconfirmation and one-use mutation authorization."""

from dataclasses import asdict, replace
from hashlib import sha256
import hmac
from secrets import token_bytes
from typing import Any

import pymupdf

from ..domain.annotation_commands import AnnotationCommand, CommandContext, CommandSource
from ..domain.errors import DomainValidationError, MutationErrorCode
from ..domain.identifiers import AnnotationId, new_command_id
from ..domain.kinds import AnnotationKind
from ..domain.serialization import canonical_json, command_to_payload
from ..domain.targets import TextInsertionTarget, TextRangeTarget
from ..services.review_contract.corrections import validate_item
from ..util.paths import FileIdentity, identify_file
from .managed_corrections import collect_managed
from .review_localization import Localization, localize_document, localize_item, page_comment_positions
from .review_localization_text import PageMatcher, extract_runs, page_bounds
from .review_snapshot import ReviewError, ReviewSnapshotStore
from .review_targets import localized_command
from .text_service import WorkerTextService


OPERATIONS = {"replace": AnnotationKind.REPLACE_TEXT, "delete": AnnotationKind.DELETE_TEXT,
              "insert": AnnotationKind.INSERT_TEXT, "comment": AnnotationKind.STICKY_NOTE}


def _signature(page: Any) -> str:
    geometry = (tuple(page.rect), tuple(page.mediabox), tuple(page.cropbox), page.rotation)
    digest = sha256(repr(geometry).encode())
    for xref in page.get_contents():
        digest.update(sha256(page.parent.xref_stream(xref)).digest())
    return digest.hexdigest()


def _unchanged(identity: FileIdentity) -> bool:
    try:
        return identify_file(identity.canonical_path) == identity
    except (ValueError, OSError):
        return False


def _target_conflict(left: Any, right: Any) -> bool:
    if isinstance(left, TextInsertionTarget) and isinstance(right, TextInsertionTarget):
        return left.anchor == right.anchor and left.boundary_top == right.boundary_top
    if isinstance(left, TextRangeTarget) and isinstance(right, TextInsertionTarget):
        return _target_conflict(right, left)

    def rect(quad):
        return pymupdf.Quad(*[(point.x, point.y) for point in
                             (quad.upper_left, quad.upper_right, quad.lower_left, quad.lower_right)]).rect

    if isinstance(left, TextInsertionTarget) and isinstance(right, TextRangeTarget):
        top = left.boundary_top or left.anchor
        y = (left.anchor.y + top.y) / 2
        return any(bounds.x0 < left.anchor.x < bounds.x1 and bounds.y0 < y < bounds.y1
                   for bounds in map(rect, right.quads))
    return (isinstance(left, TextRangeTarget) and isinstance(right, TextRangeTarget)
            and any(rect(a).intersects(rect(b)) for a in left.quads for b in right.quads))


class ReviewApplicationWorker:
    """Lives in the PDF process. Only the current command permit is retained."""

    def __init__(self, snapshots: ReviewSnapshotStore) -> None:
        self._snapshots = snapshots
        self._key = token_bytes(32)
        self._authorized: AnnotationCommand | None = None

    def _base(self, data: dict, *, confirmed: bool = False) -> tuple:
        keys = {"snapshot_id", "job_id", "annotation_id", "proposal"}
        if confirmed:
            keys |= {"location", "seal", "command_id"}
        if set(data) != keys:
            raise ReviewError("REVIEW_INVALID_REQUEST", "Richiesta di applicazione non valida.")
        snapshot = self._snapshots.checked_snapshot(data["snapshot_id"])
        annotation_id = AnnotationId(data["annotation_id"])
        AnnotationId(data["job_id"])
        proposal = data["proposal"]
        page = proposal["page_index"]
        validate_item(proposal, 0, snapshot.page_count - 1)
        return snapshot, annotation_id, proposal, page

    def _seal(self, data: dict, location: dict) -> str:
        fields = {key: data[key] for key in ("snapshot_id", "job_id", "annotation_id", "proposal")}
        return hmac.new(self._key, canonical_json({**fields, "location": location}).encode(),
                        "sha256").hexdigest()

    def localize(self, data: dict) -> dict:
        snapshot, _annotation_id, proposal, number = self._base(data)
        with pymupdf.open(snapshot.path) as frozen:
            if frozen.page_count != snapshot.page_count or frozen.is_repaired:
                raise ReviewError("REVIEW_SNAPSHOT_CHANGED", "Snapshot non valido.")
            page = frozen[number]
            proof = {"page_digest": _signature(page),
                     "localization": asdict(localize_document(frozen, [proposal], base_only=True)[0])}
        self._snapshots.checked_snapshot(snapshot.snapshot_id)
        return {"location": proof, "seal": self._seal(data, proof)}

    def confirm(self, data: dict, service: WorkerTextService, identity: FileIdentity) -> dict:
        self._authorized = None
        snapshot, annotation_id, proposal, number = self._base(data, confirmed=True)
        proof = data["location"]
        if not hmac.compare_digest(data["seal"], self._seal(data, proof)):
            raise ReviewError("REVIEW_INVALID_PROOF", "Prova di localizzazione non valida.")
        if snapshot.document_id != service.document_id or not _unchanged(identity):
            raise ReviewError("REVIEW_SOURCE_CHANGED", "Il documento della revisione è cambiato.")
        if snapshot.page_count != service.document.page_count:
            raise ReviewError("REVIEW_SOURCE_CHANGED", "Il numero di pagine è cambiato.")
        page = service.document[number]
        if _signature(page) != proof["page_digest"]:
            return {"status": "target_changed", "reason": "base_page_changed"}

        historical = Localization(**proof["localization"])
        bounds = page_bounds(page)
        matcher = PageMatcher(extract_runs(page, bounds, base_only=True), bounds)
        live = localize_item(0, proposal, matcher, new_command_id())
        if proposal["operation"] == "comment" and (historical.status == "page_fallback" or live != historical):
            live = replace(historical, status="page_fallback",
                           comment_position=page_comment_positions(page, 1, avoid_notes=True)[0])
        elif live != historical:
            return {"status": "ambiguous" if live.status == "ambiguous" else "target_changed",
                    "reason": live.reason or "snapshot_target_mismatch"}

        context = CommandContext(data["command_id"], service.document_id, service.generation,
                                 CommandSource.AI, review_snapshot_id=snapshot.snapshot_id)
        index = None
        characters = ()
        if live.operation != "comment":
            index = service.page_index(number)
            characters = tuple(char for i in range(len(index.chunks))
                               for char in service.chunk(number, index.page_text_id, i).characters)
        self._authorized = localized_command(service, context, annotation_id, live, proposal,
                                             matcher, index, characters, match_block=False)
        return {"status": "confirmed", "command": command_to_payload(self._authorized),
                "live_status": live.status}

    def authorize(self, command: AnnotationCommand, adapter: Any, identity: FileIdentity) -> None:
        if self._authorized != command:
            raise DomainValidationError(MutationErrorCode.STALE_TEXT_TARGET)
        self._authorized = None
        try:
            self._snapshots.checked_snapshot(command.context.review_snapshot_id)
        except ReviewError:
            raise DomainValidationError(MutationErrorCode.REVIEW_DOCUMENT_CHANGED) from None
        if not _unchanged(identity):
            raise DomainValidationError(MutationErrorCode.REVIEW_DOCUMENT_CHANGED)
        if adapter.review_owned(command.annotation_id.value) is not None:
            raise DomainValidationError(MutationErrorCode.DUPLICATE_COMMAND)
        if isinstance(command.target, (TextRangeTarget, TextInsertionTarget)) and any(
                _target_conflict(command.target, other.target)
                for other in adapter.review_editorial_on_page(command.target.page_index)):
            raise DomainValidationError(MutationErrorCode.REVIEW_CONFLICT)

    def lookup(self, data: dict, service: WorkerTextService, adapter: Any, identity: FileIdentity) -> dict:
        if not _unchanged(identity) or set(data) != {"annotation_id", "page_index", "operation"}:
            raise ReviewError("REVIEW_SOURCE_CHANGED", "Il documento della revisione è cambiato.")
        annotation_id = AnnotationId(data["annotation_id"]).value
        page = service.document[data["page_index"]]
        roots = [xref for xref, _kind, name in page.annot_xrefs() if name == annotation_id]
        model = adapter.review_owned(annotation_id)
        if len(roots) != 1 or model is None or model.page_index != page.number or (
                model.kind != OPERATIONS[data["operation"]] or model.source != CommandSource.AI):
            return {}
        root = roots[0]
        if model.kind == AnnotationKind.STICKY_NOTE:
            note = page.load_annot(root)
            if (service.document.xref_get_key(root, "Subtype")[1] != "/Text"
                    or note.info["content"] != model.text or not note.has_popup):
                return {}
            others = (note.popup_xref,)
        else:
            group = collect_managed(service.document, page.number, root)
            others = tuple(xref for xref in group.components if xref != root)
        return {"pdf_nm": annotation_id, "xref": root, "associated_xrefs": others}

    def clear(self) -> None:
        self._authorized = None
