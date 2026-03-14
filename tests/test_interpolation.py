from app.parsing import linear_interpolate


def test_linear_interpolate_inside_range() -> None:
    points = [(0.0, 0.0), (2.0, 100.0)]
    assert linear_interpolate(1.0, points) == 50.0


def test_linear_interpolate_clamps_outside_range() -> None:
    points = [(0.0, 0.0), (2.0, 100.0)]
    assert linear_interpolate(-1.0, points) == 0.0
    assert linear_interpolate(3.0, points) == 100.0
