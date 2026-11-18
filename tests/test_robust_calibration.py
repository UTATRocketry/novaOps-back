import pytest

from app.models import ConvertMethod, SystemConfig
from app.services.data_service import (
    RollingAverageStore,
    SensorParser,
    linear_interpolate,
    robust_interpolate,
)

ROBUST = [ConvertMethod.THEIL_SEN, ConvertMethod.REPEATED_MEDIAN]

# y = 10x, except the last point, which is badly off.
ONE_BAD_POINT = [(0.0, 0.0), (1.0, 10.0), (2.0, 20.0), (3.0, 30.0), (4.0, 400.0)]
# y = 10x for 5 of 7 points: more bad points than Theil-Sen can ignore, fewer than half.
TWO_BAD_OF_SEVEN = [(0.0, 0.0), (1.0, 10.0), (2.0, 20.0), (3.0, 30.0), (4.0, 40.0), (5.0, 500.0), (6.0, 900.0)]


@pytest.mark.parametrize("method", ROBUST)
def test_clean_points_match_least_squares(method: ConvertMethod) -> None:
    points = [(0.0, 0.0), (2.0, 100.0)]
    assert robust_interpolate(1.0, points, method) == pytest.approx(linear_interpolate(1.0, points))


@pytest.mark.parametrize("method", ROBUST)
def test_one_bad_point_does_not_skew_the_line(method: ConvertMethod) -> None:
    # Least squares is pulled well away from y = 10x; the robust fits aren't.
    assert linear_interpolate(2.0, ONE_BAD_POINT) != pytest.approx(20.0, abs=1.0)
    assert robust_interpolate(2.0, ONE_BAD_POINT, method) == pytest.approx(20.0)


@pytest.mark.parametrize("method", ROBUST)
def test_extrapolates_past_the_outermost_points(method: ConvertMethod) -> None:
    assert robust_interpolate(-1.0, ONE_BAD_POINT, method) == pytest.approx(-10.0)
    assert robust_interpolate(10.0, ONE_BAD_POINT, method) == pytest.approx(100.0)


def test_repeated_median_ignores_one_bad_point_in_four() -> None:
    points = [(0.0, 0.0), (1.0, 10.0), (2.0, 20.0), (3.0, 330.0)]
    assert robust_interpolate(2.0, points, ConvertMethod.REPEATED_MEDIAN) == pytest.approx(20.0)


def test_repeated_median_survives_more_bad_points_than_theil_sen() -> None:
    assert robust_interpolate(2.0, TWO_BAD_OF_SEVEN, ConvertMethod.REPEATED_MEDIAN) == pytest.approx(20.0)
    assert robust_interpolate(2.0, TWO_BAD_OF_SEVEN, ConvertMethod.THEIL_SEN) != pytest.approx(20.0, abs=1.0)


@pytest.mark.parametrize("method", ROBUST)
def test_repeated_raw_values_are_ignored_when_computing_slopes(method: ConvertMethod) -> None:
    points = [(1.0, 10.0), (1.0, 12.0), (2.0, 20.0), (3.0, 30.0)]
    assert robust_interpolate(4.0, points, method) == pytest.approx(40.0)


@pytest.mark.parametrize("method", ROBUST)
@pytest.mark.parametrize("points", [None, [], [(1.0, 1.0)], [(1.0, 1.0), (1.0, 5.0)]])
def test_too_few_distinct_points_pass_the_raw_value_through(method: ConvertMethod, points) -> None:
    assert robust_interpolate(7.3, points, method) == 7.3


@pytest.mark.parametrize("method", ["theil_sen", "repeated_median"])
def test_parser_applies_robust_methods(method: str) -> None:
    config = SystemConfig.model_validate(
        {
            "Sensors": [
                {
                    "name": "PGSO",
                    "type": "PT",
                    "unit": "psi",
                    "binding": {"source": "GCS", "hat_id": 0, "channel_id": 0},
                    "convert": {"method": method, "calibration": [list(p) for p in ONE_BAD_POINT]},
                }
            ]
        }
    )
    parsed = SensorParser(RollingAverageStore(window_size=5)).parse(
        source="GCS",
        raw_sensors=[{"hat_id": 0, "channel_id": 0, "value": 2.0, "timestamp": 1}],
        config=config,
        calibration_enabled=True,
    )
    assert parsed[0].value == pytest.approx(20.0)
    assert parsed[0].unit == "psi"
