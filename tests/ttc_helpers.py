"""Small constructed TTC inputs. Stop IDs and names are real TTC values; the schedule
and predictions are made up to exercise specific cases."""

import io
import zipfile
from datetime import UTC, datetime

from google.transit import gtfs_realtime_pb2

T0 = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)

# Line 1 southbound and northbound around Bloor; Line 2 at Yonge (same station) plus a bus.
LINE_1_SOUTH = ["13803", "13864", "13806", "13807"]  # Rosedale, Bloor, Wellesley, College
LINE_1_NORTH = ["13808", "13805", "13863", "13804"]
LINE_2_EAST = ["13757", "13756", "13753"]  # Bay, Yonge, Sherbourne
STOP_NAMES = {
    "13803": "Rosedale Station - Southbound Platform",
    "13864": "Bloor Station - Southbound Platform",
    "13806": "Wellesley Station - Southbound Platform",
    "13807": "College Station - Southbound Platform",
    "13808": "College Station - Northbound Platform",
    "13805": "Wellesley Station - Northbound Platform",
    "13863": "Bloor Station - Northbound Platform",
    "13804": "Rosedale Station - Northbound Platform",
    "13757": "Bay Station - Eastbound Platform",
    "13756": "Yonge Station - Eastbound Platform",
    "13753": "Sherbourne Station - Eastbound Platform",
    "662": "Danforth Rd at Kennedy Rd",
}
# A bus route east and west along one street, ~800 m between stops, and a streetcar.
# (stop_id, name, lat, lon); the schedule takes 3 minutes between stops.
BUS_EAST = [
    ("662", "Danforth Rd at Kennedy Rd", 43.7, -79.27),
    ("663", "Danforth Rd at Midland Ave", 43.7, -79.26),
    ("664", "Danforth Rd at Brimley Rd", 43.7, -79.25),
]
BUS_WEST = [
    ("665", "Danforth Rd at Brimley Rd", 43.7003, -79.25),
    ("666", "Danforth Rd at Midland Ave", 43.7003, -79.26),
    ("667", "Danforth Rd at Kennedy Rd", 43.7003, -79.27),
]
STREETCAR = [
    ("701", "Queen St at Yonge St", 43.6525, -79.3793),
    ("702", "Queen St at Bay St", 43.6522, -79.3818),
]
SURFACE_STOPS = {s: (n, lat, lon) for s, n, lat, lon in BUS_EAST + BUS_WEST + STREETCAR}
# Line 6 Finch West, light rail (route_type 0 like streetcars), at its western end. Each
# trip takes 3 minutes between stops: eastbound trips leave Humber College at 08:56, 09:06
# and 09:16 Toronto time (T0 is 09:00), westbound ones leave Martin Grove at 08:58.
LINE_6_EAST = [
    ("16289", "Humber College Station LRT Platform", 43.7286, -79.6067),
    ("16291", "Westmore Station Eastbound Platform", 43.7372, -79.6000),
    ("16293", "Martin Grove Station Eastbound Platform", 43.7428, -79.5878),
]
LINE_6_WEST = [
    ("16294", "Martin Grove Station Westbound Platform", 43.7429, -79.5879),
    ("16292", "Westmore Station Westbound Platform", 43.7373, -79.6001),
    ("16290", "Humber College Station LRT Platform", 43.7287, -79.6068),
]


def _csv(header, rows):
    return "\n".join([",".join(header), *(",".join(map(str, r)) for r in rows)]) + "\n"


def static_zip(extra_stop_times=(), light_rail=False) -> bytes:
    trips, stop_times = [], []
    patterns = [
        ("1", 0, "Line 1 (Yonge-University) towards Vaughan Metropolitan Centre", LINE_1_SOUTH),
        ("1", 1, "Line 1 (Yonge-University) towards Finch Station", LINE_1_NORTH),
        ("2", 0, "Line 2 (Bloor-Danforth) towards Kennedy Station", LINE_2_EAST),
    ]
    for route_id, direction, headsign, stops in patterns:
        for n in range(3):
            trip_id = f"{route_id}{direction}{n}"
            trips.append((route_id, "1", trip_id, headsign, direction))
            for seq, stop_id in enumerate(stops, start=1):
                t = f"{6 + n}:{seq * 2:02d}:00"
                stop_times.append((trip_id, t, t, stop_id, seq))
    surface = [
        ("10", 0, "East - 10 Van Horne towards Brimley", BUS_EAST, 2),
        ("10", 1, "West - 10 Van Horne towards Kennedy Station", BUS_WEST, 2),
        ("501", 0, "West - 501 Queen towards Long Branch", STREETCAR, 1),
    ]
    for route_id, direction, headsign, stops, count in surface:
        for n in range(count):
            trip_id = f"s{route_id}{direction}{n}"
            trips.append((route_id, "1", trip_id, headsign, direction))
            for seq, (stop_id, *_rest) in enumerate(stops, start=1):
                t = f"{7 + n}:{(seq - 1) * 3:02d}:00"
                stop_times.append((trip_id, t, t, stop_id, seq))
    routes = [
        ("1", "1", "1", "Line 1 (Yonge-University)", 1),
        ("2", "1", "2", "Line 2 (Bloor - Danforth)", 1),
        ("10", "1", "10", "Van Horne", 3),
        ("501", "1", "501", "Queen", 0),
    ]
    stops = [(s, n, 43.66, -79.38, 1) for s, n in STOP_NAMES.items() if s not in SURFACE_STOPS]
    stops += [(s, n, lat, lon, 1) for s, (n, lat, lon) in SURFACE_STOPS.items()]
    if light_rail:
        routes.append(("6", "1", "6", "Line 6 Finch West", 0))
        for direction, pattern, first, count in ((0, LINE_6_EAST, 56, 3), (1, LINE_6_WEST, 58, 1)):
            headsign = "Martin Grove" if direction == 0 else "Humber College"
            for n in range(count):
                trip_id = f"6{direction}{n}"
                trips.append(
                    ("6", "1", trip_id, f"Line 6 Finch West towards {headsign}", direction)
                )
                for seq, (stop_id, *_rest) in enumerate(pattern, start=1):
                    minute = first + n * 10 + (seq - 1) * 3
                    t = f"{8 + minute // 60:02d}:{minute % 60:02d}:00"
                    stop_times.append((trip_id, t, t, stop_id, seq))
            stops += [(s, n, lat, lon, 1) for s, n, lat, lon in pattern]
    stop_times.extend(extra_stop_times)
    files = {
        "routes.txt": _csv(
            ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
            routes,
        ),
        "trips.txt": _csv(
            ["route_id", "service_id", "trip_id", "trip_headsign", "direction_id"], trips
        ),
        "stops.txt": _csv(
            ["stop_id", "stop_name", "stop_lat", "stop_lon", "wheelchair_boarding"], stops
        ),
        "stop_times.txt": _csv(
            ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
            stop_times,
        ),
        "calendar.txt": _csv(
            [
                "service_id",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
                "start_date",
                "end_date",
            ],
            [("1", 1, 1, 1, 1, 1, 0, 0, "20260930", "20261031")],
        ),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), text)
    return buffer.getvalue()


def trip_feed(timestamp: datetime, trips: list[dict]) -> bytes:
    """trips: [{"trip_id", "route_id", "vehicle"?, "label"?, "stops": [(stop_id, seq, offset_s)]}]

    The vehicle label defaults to the trip_id; pass "vehicle" to model TTC reassigning
    trip_ids to the same train between polls."""
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    message.header.timestamp = int(timestamp.timestamp())
    for trip in trips:
        entity = message.entity.add()
        entity.id = f"subway-{trip['trip_id']}|{trip.get('label', 'South')}"
        update = entity.trip_update
        update.trip.trip_id = trip["trip_id"]
        update.trip.route_id = trip["route_id"]
        update.vehicle.label = trip.get("vehicle", trip["trip_id"])
        for stop_id, sequence, offset in trip["stops"]:
            stu = update.stop_time_update.add()
            stu.stop_id = stop_id
            stu.stop_sequence = sequence
            stu.arrival.time = int(timestamp.timestamp()) + offset
    return message.SerializeToString()


def alert_feed(timestamp: datetime, alerts: list[dict]) -> bytes:
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    message.header.timestamp = int(timestamp.timestamp())
    for a in alerts:
        entity = message.entity.add()
        entity.id = a["id"]
        alert = entity.alert
        alert.effect = gtfs_realtime_pb2.Alert.Effect.Value(a.get("effect", "UNKNOWN_EFFECT"))
        period = alert.active_period.add()
        period.start = int(a.get("start", T0).timestamp())
        for stop_id in a.get("stops", []):
            alert.informed_entity.add().stop_id = stop_id
        for route_id in a.get("routes", []):
            alert.informed_entity.add().route_id = route_id
        translation = alert.header_text.translation.add()
        translation.text = a["text"]
        translation.language = "en"
    return message.SerializeToString()


def vehicle_feed(timestamp: datetime, vehicles: list[dict]) -> bytes:
    """vehicles: [{"id", "route_id"?, "lat", "lon", "bearing"?, "age"? (s), "stop_id"?}]"""
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    message.header.timestamp = int(timestamp.timestamp())
    for v in vehicles:
        entity = message.entity.add()
        entity.id = v["id"]
        position = entity.vehicle
        position.vehicle.id = v["id"]
        if v.get("route_id"):
            position.trip.route_id = v["route_id"]
            position.trip.trip_id = f"rt-{v['id']}"
        position.position.latitude = v["lat"]
        position.position.longitude = v["lon"]
        if "bearing" in v:
            position.position.bearing = v["bearing"]
        position.timestamp = int(timestamp.timestamp()) - v.get("age", 20)
        position.stop_id = v.get("stop_id", "9999")
        position.occupancy_status = gtfs_realtime_pb2.VehiclePosition.MANY_SEATS_AVAILABLE
    return message.SerializeToString()


def surface_trip_feed(timestamp, trips):
    """trips: [{"trip_id", "route_id", "vehicle"?, "cancelled"?, "stops": [(stop_id, offset_s)]}]"""
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = int(timestamp.timestamp())
    for trip in trips:
        update = message.entity.add(id=trip["trip_id"]).trip_update
        update.trip.trip_id, update.trip.route_id = trip["trip_id"], trip["route_id"]
        if trip.get("cancelled"):
            update.trip.schedule_relationship = gtfs_realtime_pb2.TripDescriptor.CANCELED
        if trip.get("vehicle"):
            update.vehicle.id = trip["vehicle"]
        for stop_id, offset in trip["stops"]:
            stu = update.stop_time_update.add(stop_id=stop_id)
            stu.arrival.time = int(timestamp.timestamp()) + offset
    return message.SerializeToString()
