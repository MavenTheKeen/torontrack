"""Official TTC endpoints. Only the collector contacts them; browsers never do."""

REALTIME_HOST = "gtfsrt.ttc.ca"
REALTIME_FEEDS = {
    "trips_subway": f"https://{REALTIME_HOST}/trips/subway?format=binary",
    "alerts_subway": f"https://{REALTIME_HOST}/alerts/subway?format=binary",
    "alerts_accessibility": f"https://{REALTIME_HOST}/alerts/accessibility?format=binary",
    # Every bus and streetcar in service (TTC publishes no surface arrival predictions).
    "vehicles": f"https://{REALTIME_HOST}/vehicles/position?format=binary",
}

# Toronto Open Data "TTC Routes and Schedules" (static GTFS, refreshed roughly monthly).
STATIC_HOST = "ckan0.cf.opendata.inter.prod-toronto.ca"
STATIC_GTFS_URL = (
    f"https://{STATIC_HOST}/dataset/7795b45e-e65a-4465-81fc-c36b9dfff169/resource/"
    "cfb6b2b8-6191-41e3-bda1-b175c51148cb/download/opendata_ttc_schedules.zip"
)

# GTFS route_type 1 = subway/metro. Lines 5 and 6 are light rail (type 0) and are not in
# the realtime subway feed.
SUBWAY_ROUTE_TYPE = "1"
