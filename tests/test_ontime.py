"""On-time estimates for planned trips."""

from transit.web import ontime

# A weekday at 08:00 Toronto: the morning part of the day.
MORNING = "2026-11-03T08:00:00-05:00"
BUFFERS = {
    "weekday|10|0|am": {"wait": 600, "ride_ratio": 1.5},
    "weekday|1|1|am": {"wait": 480, "ride_ratio": None},
    "weekday|29|0|am": {"wait": 300, "ride_ratio": None},
    "weekend|10|0|am": {"wait": 1500, "ride_ratio": 1.2},
    "weekday|10|0|off": {"wait": 1200, "ride_ratio": 1.1},
}


def ride(route="10", direction=0, mode="bus", network="ttc", start=MORNING, seconds=1200):
    return {
        "mode": mode,
        "network": network,
        "route": route,
        "direction": direction,
        "start": start,
        "seconds": seconds,
    }


def trip(*legs):
    return {"start": MORNING, "end": "2026-11-03T08:30:00-05:00", "legs": list(legs)}


def test_a_bus_ride_adds_its_wait_and_its_ride():
    # Wait 600 s, ride 600 s: halfway between 1200 and 849 is 1024.3 s, so 18 minutes.
    assert ontime.buffer_minutes(trip(ride()), BUFFERS) == 18


def test_a_subway_ride_adds_only_its_wait():
    assert ontime.buffer_minutes(trip(ride("1", 1, "subway")), BUFFERS) == 8


def test_rides_add_up_by_less_than_their_sum():
    both = ontime.buffer_minutes(trip(ride(), ride("1", 1, "subway")), BUFFERS)
    assert 18 < both < 18 + 8


def test_the_part_of_the_day_and_kind_of_day_come_from_boarding():
    saturday = ride(start="2026-11-07T08:00:00-05:00")
    assert ontime.buffer_minutes(trip(saturday), BUFFERS) != 18
    night = ride(start="2026-11-03T23:30:00-05:00")
    assert ontime.buffer_minutes(trip(night), BUFFERS) == ontime.math.ceil(
        (1200 + 120 + (1200**2 + 120**2) ** 0.5) / 2 / 60
    )
    # 02:00 on Saturday is still Friday's service day, and off-peak.
    friday_night = ride(start="2026-11-07T02:00:00-05:00")
    assert ontime.buffer_minutes(trip(friday_night), BUFFERS) == ontime.buffer_minutes(
        trip(night), BUFFERS
    )


def test_any_ride_without_measurements_means_no_estimate():
    for unmeasured in (
        ride("999"),  # No data for the route.
        ride(network="br"),  # Another agency.
        ride(direction=None),  # The planner gave no direction.
        ride("29"),  # Waits but no ride times, on a bus.
    ):
        assert ontime.buffer_minutes(trip(ride(), unmeasured), BUFFERS) is None
    walk = {"mode": "walk", "network": None, "seconds": 300}
    assert ontime.buffer_minutes(trip(walk), BUFFERS) is None
    assert ontime.buffer_minutes(trip(walk, ride()), BUFFERS) == 18


def test_annotate_gives_times_to_leave_and_arrive():
    covered, uncovered = trip(ride()), trip(ride("999"))
    ontime.annotate([covered, uncovered], BUFFERS)
    assert covered["on_time"] == {
        "buffer_minutes": 18,
        "leave_by": "2026-11-03T07:42:00-05:00",
        "arrive_by": "2026-11-03T08:48:00-05:00",
    }
    assert "on_time" not in uncovered
