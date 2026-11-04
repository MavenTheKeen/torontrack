"""Official TTC endpoints. Only the collector contacts them; browsers never do."""

REALTIME_HOST = "gtfsrt.ttc.ca"
REALTIME_FEEDS = {
    "trips_subway": f"https://{REALTIME_HOST}/trips/subway?format=binary",
    "alerts_subway": f"https://{REALTIME_HOST}/alerts/subway?format=binary",
    "alerts_accessibility": f"https://{REALTIME_HOST}/alerts/accessibility?format=binary",
    # Every alert; only those naming Lines 5 and 6 (light rail, absent from the subway
    # feed) are kept.
    "alerts_light_rail": f"https://{REALTIME_HOST}/alerts/all?format=binary",
    # Every bus and streetcar in service.
    "vehicles": f"https://{REALTIME_HOST}/vehicles/position?format=binary",
    # Bus and streetcar trip updates: TTC's own arrival predictions (not listed in the
    # site's docs until 2026; about 2,400 trips and 31,000 stop predictions, ~700 KB).
    "trips_surface": f"https://{REALTIME_HOST}/trips/update?format=binary",
}

# Toronto Open Data "Merged GTFS: TTC Routes and Schedules" (static GTFS). The older "TTC
# Routes and Schedules" file has been an empty ZIP since late October 2026.
STATIC_HOST = "ckan0.cf.opendata.inter.prod-toronto.ca"
STATIC_GTFS_URL = (
    f"https://{STATIC_HOST}/dataset/b811ead4-6eaf-4adb-8408-d389fb5a069c/resource/"
    "c920e221-7a1c-488b-8c5b-6d8cd4e85eaf/download/completegtfs.zip"
)

# GTFS route_type 1 = subway/metro. Lines 5 and 6 are light rail (type 0, as streetcars
# are) but run as rapid transit lines with stations, so they are loaded with the subway.
# TTC publishes no realtime data for them (no predictions, no vehicle positions), so their
# trains are placed by timetable.
SUBWAY_ROUTE_TYPE = "1"
LIGHT_RAIL_LINES = frozenset({"5", "6"})
