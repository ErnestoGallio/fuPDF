"""Spawn-safe mutation adapters used only by M5 tests."""

from time import sleep

from pdf_annotator.worker.annotation_dispatch import AppliedMutation
from pdf_annotator.worker.document_service import run_document_worker


def synthetic_mutation(document, command) -> AppliedMutation:
    del document, command
    return AppliedMutation()


def delayed_synthetic_mutation(document, command) -> AppliedMutation:
    del document, command
    sleep(.25)
    return AppliedMutation()


def failing_synthetic_mutation(document, command) -> AppliedMutation:
    del document, command
    raise RuntimeError("synthetic adapter failure")


def synthetic_worker(conn, document_id, source) -> None:
    run_document_worker(conn, document_id, source,
                        mutation_adapter=synthetic_mutation)


def delayed_synthetic_worker(conn, document_id, source) -> None:
    run_document_worker(conn, document_id, source,
                        mutation_adapter=delayed_synthetic_mutation)


def failing_synthetic_worker(conn, document_id, source) -> None:
    run_document_worker(conn, document_id, source,
                        mutation_adapter=failing_synthetic_mutation)

