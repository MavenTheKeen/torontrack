# Curbside collection

`transit waste-refresh` loads two City of Toronto datasets, both under the Open Government
Licence – Toronto, and replaces the previous load:

- **Daytime curbside collection areas** (`solid-waste-daytime-curbside-collection-areas`):
  34 polygons, each naming a calendar such as Tuesday 1 or Friday 2, or No Collection for
  the downtown night-collection area. The city publishes them only as a shapefile in
  latitude and longitude; `waste.read_areas` reads the `.shp` and `.dbf` directly, so no
  GIS library is needed. The polygons were last redrawn in 2012 and their names still match
  the current calendar.
- **Pickup schedule** (`solid-waste-pickup-schedule`), one table per year: for each
  calendar, every collection date and what is collected (garbage, Green Bin, recycling,
  yard waste, Christmas trees). Dates are already moved for holidays, so a Friday
  collection in Christmas week appears on the Saturday. Tables for the current and later
  years are loaded.

Areas go to `normalized.waste_areas` with their bounding box and rings, collections to
`normalized.waste_collections`, and each attempt to `ops.waste_refreshes`. The scheduler
runs the load daily at 10:40 UTC (`waste_collection`).

`GET /api/waste?at=lat,lon` finds the area containing the point (bounding box in SQL, then
the even-odd rule over its rings in Python) and returns its calendar and next four
collections. On the map, an address or place card shows them. The calendar applies to
houses on curbside collection; apartments and businesses may be collected differently.
