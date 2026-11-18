from pdf_annotator.domain.geometry import PdfPoint
from pdf_annotator.domain.targets import MAX_INK_POINTS
from pdf_annotator.viewer.pen_tool import PenGestureController


def test_begin_move_release_produces_single_page_target_and_metrics():
    gesture = PenGestureController()
    gesture.begin(3, PdfPoint(10, 20), (100, 200))
    assert gesture.active and gesture.page_index == 3
    assert gesture.update(PdfPoint(11, 20), (101, 200))
    assert gesture.update(PdfPoint(20, 30), (110, 210), final=True)
    target, metrics = gesture.finalize()
    assert target.page_index == 3
    assert target.paths == ((PdfPoint(10, 20), PdfPoint(11, 20), PdfPoint(20, 30)),)
    assert metrics.raw_events == 3 and metrics.final_points == 3
    assert not gesture.active


def test_click_tiny_motion_and_cancel_never_create_ink():
    gesture = PenGestureController()
    gesture.begin(0, PdfPoint(1, 1), (1, 1))
    target, _ = gesture.finalize()
    assert target is None
    gesture.begin(0, PdfPoint(1, 1), (1, 1))
    gesture.update(PdfPoint(1.5, 1), (1.5, 1), final=True)
    target, _ = gesture.finalize()
    assert target is None
    gesture.begin(0, PdfPoint(1, 1), (1, 1))
    gesture.cancel()
    assert not gesture.active and gesture.points == ()


def test_sampling_coalesces_nearby_points_and_adaptively_stays_bounded():
    gesture = PenGestureController()
    gesture.begin(0, PdfPoint(0, 0), (0, 0))
    for index in range(1, MAX_INK_POINTS * 4):
        gesture.update(PdfPoint(index / 10, 10), (float(index), 10.0))
    gesture.update(PdfPoint(500, 10), (5000, 10), final=True)
    target, metrics = gesture.finalize()
    assert target is not None
    assert 2 <= len(target.paths[0]) <= MAX_INK_POINTS
    assert metrics.raw_events == MAX_INK_POINTS * 4 + 1
    assert target.paths[0][0] == PdfPoint(0, 0)
    assert target.paths[0][-1] == PdfPoint(500, 10)
