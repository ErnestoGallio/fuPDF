import json

import pytest

from pdf_annotator.services.review_batches import (
    REVIEW_PDF_LIMIT, ReviewSnapshot, plan_batches, validate_correction_pages,
)


def snapshot(pages):
    return ReviewSnapshot("snapshot", "document", 1, 2, pages, "hash", 120, "snapshot.pdf")


@pytest.mark.parametrize(("pages", "batch_count"), [(1, 1), (4, 1), (5, 2), (10001, 2501)])
def test_full_plan_covers_all_physical_pages_once(pages, batch_count):
    frozen = snapshot(pages)
    plan = plan_batches(frozen)
    assert len(plan.batches) == batch_count
    assert [page for batch in plan.batches for page in batch.target_pages] == list(range(pages))
    assert len({batch.batch_id for batch in plan.batches}) == batch_count
    assert plan == plan_batches(frozen)
    assert plan.batches[0].context_pages == ((4,) if pages > 4 else ())
    assert plan.batches[-1].context_pages == (((batch_count - 1) * 4 - 1,) if pages > 4 else ())
    document = json.loads(json.dumps(plan.to_payload()))
    assert document["target_page_count"] == pages and document["batch_count"] == batch_count
    assert not document["unprocessable_pages"]


def test_subrange_has_context_outside_requested_interval_and_rejects_corrections_there():
    plan = plan_batches(snapshot(10), 2, 6)
    first, last = plan.batches
    assert first.target_pages == (2, 3, 4, 5) and first.context_pages == (1, 6)
    assert last.target_pages == (6,) and last.context_pages == (5, 7)
    assert [(row.extract_index, row.page_index, row.role) for row in first.page_map] == [
        (0, 1, "context"), (1, 2, "target"), (2, 3, "target"),
        (3, 4, "target"), (4, 5, "target"), (5, 6, "context"),
    ]
    validate_correction_pages(first, [{"page_index": 2}, {"page_index": 5}])
    for page in (0, 1, 6, 7, None, False, "2"):
        with pytest.raises(ValueError, match="assigned target"):
            validate_correction_pages(first, [{"page_index": page}])
    assert REVIEW_PDF_LIMIT == 20 * 1024 * 1024


@pytest.mark.parametrize(("first", "last"), [(-1, 2), (0, 5), (4, 3), (True, 3)])
def test_invalid_range(first, last):
    with pytest.raises(ValueError, match="physical page interval"):
        plan_batches(snapshot(5), first, last)
