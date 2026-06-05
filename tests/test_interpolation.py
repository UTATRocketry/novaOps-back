import pytest

from app.parsing import linear_interpolate


def test_linear_interpolate_inside_range() -> None:
    points = [(0.0, 0.0), (2.0, 100.0)]
    assert linear_interpolate(1.0, points) == pytest.approx(50.0)


def test_linear_interpolate_extrapolates_outside_range() -> None:
    # The fit is a straight line; values outside the calibration span extrapolate.
    points = [(0.0, 0.0), (2.0, 100.0)]
    assert linear_interpolate(-1.0, points) == pytest.approx(-50.0)
    assert linear_interpolate(3.0, points) == pytest.approx(150.0)


def test_single_point_calibration_returns_raw_value() -> None:
    # degree+1 = 2 points required; a single point passes the raw value through.
    assert linear_interpolate(7.3, [(1.0, 1.0)]) == 7.3


def test_empty_calibration_returns_raw_value() -> None:
    assert linear_interpolate(4.2, None) == 4.2
    assert linear_interpolate(4.2, []) == 4.2
