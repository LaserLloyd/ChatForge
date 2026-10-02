"""Pure popup placement: lower-right of the work area, inside it, with the margin."""

from __future__ import annotations

import pytest

from chatforge.desktop.win32util import Rect, place, resize_from

W, H, MARGIN = 420, 620, 12


def _check_inside_with_margin(rect: Rect, work: Rect, dpi: int) -> None:
    scale = dpi / 96
    m = round(MARGIN * scale)
    assert work.contains(rect), (rect, work)
    assert rect.right == work.right - m
    assert rect.bottom == work.bottom - m
    assert rect.width == round(W * scale)
    assert rect.height == round(H * scale)


@pytest.mark.parametrize(
    ("dpi", "work"),
    [
        # 100 %, taskbar at the bottom (1920x1080)
        (96, Rect(0, 0, 1920, 1040)),
        # 150 %, taskbar at the bottom (2560x1440)
        (144, Rect(0, 0, 2560, 1380)),
        # 200 %, taskbar at the bottom (2880x1800, the target laptop)
        (192, Rect(0, 0, 2880, 1716)),
        # 175 %, what the target laptop reported in the spike
        (168, Rect(0, 0, 2880, 1716)),
    ],
)
def test_bottom_taskbar(dpi: int, work: Rect) -> None:
    rect = place(work, dpi, W, H, MARGIN)
    _check_inside_with_margin(rect, work, dpi)


@pytest.mark.parametrize("dpi", [96, 144, 192])
def test_left_taskbar(dpi: int) -> None:
    # The work area starts to the right of a left-docked taskbar.
    work = Rect(80, 0, 2880, 1800)
    rect = place(work, dpi, W, H, MARGIN)
    _check_inside_with_margin(rect, work, dpi)
    assert rect.left >= 80


@pytest.mark.parametrize("dpi", [96, 144, 192])
def test_top_taskbar(dpi: int) -> None:
    work = Rect(0, 84, 2880, 1800)
    rect = place(work, dpi, W, H, MARGIN)
    _check_inside_with_margin(rect, work, dpi)
    assert rect.top >= 84
    assert rect.bottom == 1800 - round(MARGIN * dpi / 96)


@pytest.mark.parametrize("dpi", [96, 144, 192])
def test_second_monitor_offset(dpi: int) -> None:
    # A second monitor to the right of the primary: its work area has a large left offset
    # and starts above the primary's top edge (negative y).
    work = Rect(2880, -200, 2880 + 2560, -200 + 1440 - 40)
    rect = place(work, dpi, W, H, MARGIN)
    _check_inside_with_margin(rect, work, dpi)
    assert rect.left >= 2880


def test_second_monitor_too_small_for_200_percent_shrinks_to_fit() -> None:
    # A 1080p secondary at 200 %: the 1240 px popup cannot fit a 1040 px work area, so it
    # fills the height (the margin gives way) and still stays inside.
    work = Rect(2880, -200, 2880 + 1920, -200 + 1080 - 40)
    rect = place(work, 192, W, H, MARGIN)
    assert work.contains(rect)
    assert rect.height == work.height
    assert rect.right == work.right - 24


def test_second_monitor_left_of_primary_negative_coords() -> None:
    work = Rect(-1920, 0, 0, 1040)
    rect = place(work, 96, W, H, MARGIN)
    _check_inside_with_margin(rect, work, 96)
    assert rect.right == -12


def test_tuple_input_accepted() -> None:
    rect = place((0, 0, 1920, 1040), 96, W, H, MARGIN)
    assert rect == place(Rect(0, 0, 1920, 1040), 96, W, H, MARGIN)


def test_scaling_is_exact_at_200_percent() -> None:
    rect = place(Rect(0, 0, 2880, 1716), 192, W, H, MARGIN)
    assert (rect.width, rect.height) == (840, 1240)
    assert rect.as_tuple() == (2880 - 24 - 840, 1716 - 24 - 1240, 2880 - 24, 1716 - 24)


def test_small_work_area_shrinks_window_and_keeps_it_inside() -> None:
    work = Rect(0, 0, 400, 500)  # smaller than the popup at 100 %
    rect = place(work, 96, W, H, MARGIN)
    assert work.contains(rect)
    assert rect.width <= work.width
    assert rect.height <= work.height
    assert rect.width > 0 and rect.height > 0


def test_margin_collapses_before_the_window_shrinks() -> None:
    # Exactly enough room for the window but not for the margin: the margin gives way.
    work = Rect(0, 0, 430, 630)
    rect = place(work, 96, W, H, MARGIN)
    assert work.contains(rect)
    assert (rect.width, rect.height) == (420, 620)


def test_rect_helpers() -> None:
    outer = Rect(0, 0, 100, 100)
    assert outer.contains(Rect(10, 10, 90, 90))
    assert not outer.contains(Rect(10, 10, 101, 90))
    assert outer.width == 100 and outer.height == 100


# --- resizing from the top-left (resize_from) -------------------------------------------

START = Rect(1488, 408, 1908, 1028)  # 420 x 620 in the corner of a 1920x1040 work area
WORK = Rect(0, 0, 1920, 1040)
PRESS = (1492, 412)  # 4 px into the grip
MIN = (320, 400)


def test_the_grip_moves_the_top_left_corner_and_keeps_the_bottom_right() -> None:
    rect = resize_from(START, (True, True), PRESS, (1292, 212), MIN, WORK)
    assert rect == Rect(1288, 208, 1908, 1028)
    # Dragged inwards it shrinks the same way.
    assert resize_from(START, (True, True), PRESS, (1542, 462), MIN, WORK) == Rect(
        1538, 458, 1908, 1028
    )


def test_an_edge_moves_one_side_only() -> None:
    assert resize_from(START, (True, False), PRESS, (1292, 212), MIN, WORK) == Rect(
        1288, 408, 1908, 1028
    )
    assert resize_from(START, (False, True), PRESS, (1292, 212), MIN, WORK) == Rect(
        1488, 208, 1908, 1028
    )


def test_never_smaller_than_the_minimum() -> None:
    rect = resize_from(START, (True, True), PRESS, (1900, 1020), MIN, WORK)
    assert (rect.width, rect.height) == MIN
    assert (rect.right, rect.bottom) == (START.right, START.bottom)


def test_never_grows_past_the_work_area() -> None:
    rect = resize_from(START, (True, True), PRESS, (-500, -300), MIN, WORK)
    assert rect == Rect(0, 0, 1908, 1028)
    # A second monitor to the left: its own work area is the limit.
    left_work = Rect(-1920, 0, 0, 1040)
    start = Rect(-432, 408, -12, 1028)
    assert resize_from(start, (True, True), (-428, 412), (-3000, -50), MIN, left_work) == Rect(
        -1920, 0, -12, 1028
    )


def test_a_window_already_past_the_work_area_is_not_pulled_in() -> None:
    start = Rect(-40, 408, 380, 1028)  # dragged partly off the left of the screen
    # It does not grow further out, and is not snapped back in either.
    assert resize_from(start, (True, False), (-36, 412), (-46, 412), MIN, WORK).left == -40
    assert resize_from(start, (True, False), (-36, 412), (-30, 412), MIN, WORK).left == -34


def test_no_move_no_change() -> None:
    assert resize_from(START, (True, True), PRESS, PRESS, MIN, WORK) == START
