"""Deterministic page assignments for offline PDF review preparation."""

from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

REVIEW_BATCH_PAGES = 4
REVIEW_PDF_LIMIT = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    snapshot_id: str
    document_id: str
    document_generation: int
    document_revision: int
    page_count: int
    sha256: str
    size_bytes: int
    path: str


@dataclass(frozen=True, slots=True)
class ReviewPage:
    extract_index: int
    page_index: int
    role: Literal["target", "context"]


@dataclass(frozen=True, slots=True)
class ReviewBatch:
    batch_id: str
    target_pages: tuple[int, ...]
    context_pages: tuple[int, ...]
    page_map: tuple[ReviewPage, ...]
    status: Literal["planned", "ready", "too_large"] = "planned"
    size_bytes: int | None = None
    path: str | None = None
    sha256: str | None = None
    extract_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    snapshot: ReviewSnapshot
    first_page: int
    last_page: int
    batches: tuple[ReviewBatch, ...]

    def to_payload(self) -> dict:
        return {**asdict(self), "target_page_count": self.last_page - self.first_page + 1,
                "batch_count": len(self.batches),
                "unprocessable_pages": [page for batch in self.batches if batch.status == "too_large"
                                          for page in batch.target_pages]}


def check_interval(snapshot: ReviewSnapshot, first: int, last: int) -> None:
    if type(first) is not int or type(last) is not int or not 0 <= first <= last < snapshot.page_count:
        raise ValueError("Invalid physical page interval")


def context_variants(snapshot: ReviewSnapshot, first: int, last: int) -> Iterator[tuple[int, ...]]:
    before = (first - 1,) if first else ()
    after = (last + 1,) if last + 1 < snapshot.page_count else ()
    yield from dict.fromkeys((before + after, before, after, ()))


def make_batch(snapshot: ReviewSnapshot, first: int, last: int, context: tuple[int, ...]) -> ReviewBatch:
    targets = tuple(range(first, last + 1))
    mapping = tuple(ReviewPage(index, page, "context" if page in context else "target")
                    for index, page in enumerate(sorted(targets + context)))
    return ReviewBatch(f"{snapshot.snapshot_id}:{first}-{last}", targets, context, mapping)


def plan_batches(snapshot: ReviewSnapshot, first: int = 0, last: int | None = None) -> ReviewPlan:
    end = snapshot.page_count - 1 if last is None else last
    check_interval(snapshot, first, end)
    batches = []
    for start in range(first, end + 1, REVIEW_BATCH_PAGES):
        stop = min(start + REVIEW_BATCH_PAGES - 1, end)
        batches.append(make_batch(snapshot, start, stop, next(context_variants(snapshot, start, stop))))
    return ReviewPlan(snapshot, first, end, tuple(batches))


def validate_correction_pages(batch: ReviewBatch, corrections: Sequence[dict]) -> None:
    """Check assignments independently of the unchanged M20 correction schema."""
    for correction in corrections:
        page = correction.get("page_index")
        if batch.status == "too_large" or type(page) is not int or page not in batch.target_pages:
            raise ValueError("Correction page is not an assigned target")
