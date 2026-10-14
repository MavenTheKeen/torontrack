"""Run with: streamlit run dashboard/app.py."""

from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from transit.bikeshare.dashboard_data import (
    AnalyticsNotBuilt,
    annotate_current,
    load_analytics,
    load_history,
    load_overview,
    local_time,
    prepare_history,
    ranked_stations,
)
from transit.config import STALE_SECONDS, Settings

st.set_page_config(page_title="Toronto Bike Share Reliability", page_icon="🚲", layout="wide")
st.title("Toronto Bike Share Reliability Monitor")
st.caption(
    "Observed bike and dock availability · Toronto local time · target collection interval: 15 min"
)
st.markdown(
    "Data: [Bike Share Toronto](https://bikesharetoronto.com/), Toronto Parking Authority. "
    "Contains information licensed under the "
    "[Open Government Licence – Toronto]"
    "(https://www.toronto.ca/city-government/data-research-maps/open-data/open-data-licence/)."
)
st.button("Refresh data", help="Reads the database; does not make a new live API collection.")

try:
    with st.spinner("Reading the latest collection…"):
        settings = Settings.from_env()
        overview = load_overview(settings.database_url)
except Exception:
    st.error(
        "The dashboard could not read the database. Check DATABASE_URL, start PostgreSQL, "
        "and run the database initialization command. No live data is available here yet."
    )
    st.stop()

latest = overview["latest"]
latest_label = local_time(latest["collected_at"]) if latest else "None yet"
st.caption(
    f"Latest successful collection: {latest_label}. "
    "Open Pipeline health for collection failures and quality results."
)
with st.expander("Pipeline health", expanded=False):
    a, b, c = st.columns(3)
    a.metric("Last successful collection", local_time(latest["collected_at"]) if latest else "None")
    b.metric("Source published", local_time(latest["source_published_at"]) if latest else "Unknown")
    c.metric("Failed attempts · past 24h", overview["failed_attempts"])
    if latest:
        st.caption(
            f"HTTP fetch completed: {local_time(latest['fetched_at'])} · "
            f"Database completion: {local_time(latest['completed_at'])} · "
            f"Collection ID: {latest['collection_id']}"
        )
    if overview["checks"].empty:
        st.info("No quality-check results recorded for the latest successful collection.")
    else:
        checks = overview["checks"].copy()
        checks["result"] = checks["passed"].map({True: "Pass", False: "Fail"})
        st.dataframe(checks[["check_name", "result", "details"]], hide_index=True, width="stretch")
        if not checks["passed"].all():
            st.warning("One or more quality checks failed. Inspect the recorded details.")
    if not overview["runs"].empty:
        st.caption("Recent collection outcomes; failed retries also count in the attempt metric.")
        runs = overview["runs"].copy()
        for column in ("collected_at", "completed_at"):
            runs[column] = runs[column].map(local_time)
        st.dataframe(runs, hide_index=True, width="stretch")
    if not overview.get("attempts", pd.DataFrame()).empty:
        st.caption(
            "Recent attempts, including replay failures that preserved previously valid data."
        )
        attempts = overview["attempts"].copy()
        for column in ("started_at", "finished_at"):
            attempts[column] = attempts[column].map(local_time)
        st.dataframe(attempts, hide_index=True, width="stretch")
    transformation = overview.get("transformation")
    if transformation:
        st.write(f"**Latest dbt build:** {transformation['status']}")
        st.caption(
            f"Started: {local_time(transformation['started_at'])} · "
            f"Finished: {local_time(transformation['finished_at'])} · "
            f"Build ID: {transformation['transformation_id']}"
        )
        if transformation["status"] == "failed":
            st.warning(
                "The latest dbt build failed. Inspect its test results and CLI or Airflow logs."
            )
        elif transformation["status"] == "running":
            st.info("A dbt build is running, or was interrupted before its outcome was recorded.")
        if transformation.get("dbt_results"):
            st.dataframe(
                pd.DataFrame(transformation["dbt_results"]), hide_index=True, width="stretch"
            )
        if (
            latest
            and transformation["finished_at"]
            and transformation["finished_at"] < latest["collected_at"]
        ):
            st.info("The latest collection is newer than the recorded dbt checks. Run a new build.")
    else:
        st.info(
            "No dbt build outcome recorded yet. Run the transform command to record model checks."
        )

if not latest or overview["stations"].empty:
    st.info(
        "No station observations yet. Run a collection to begin recording real availability. "
        "Historical observations start with your first collection; no sample data is displayed."
    )
    st.code("docker compose run --rm collector collect", language="shell")
    st.stop()

now = datetime.now(UTC)
stations = annotate_current(overview["stations"], now)
ages = [
    (now - pd.Timestamp(latest[column]).to_pydatetime()).total_seconds()
    for column in ("collected_at", "fetched_at", "source_published_at")
    if latest.get(column) is not None
]
if len(ages) < 3 or any(age > STALE_SECONDS or age < -300 for age in ages):
    st.warning(
        "The latest collection or source publication is stale or has an unknown/invalid time. "
        "Counts below are the last recorded values and are not a current availability guarantee."
    )

a, b, c, d = st.columns(4)
a.metric("Stations in latest collection", len(stations))
b.metric("Stations with bikes", int(stations["rental_state"].eq("Available").sum()))
c.metric("Stations with return docks", int(stations["return_state"].eq("Available").sum()))
d.metric("Stale / missing / invalid reports", int(stations["freshness"].ne("Fresh").sum()))
st.caption(
    "Availability requires an installed station, its rental/return service enabled, and fetch, "
    "source, and station timestamps no more than 30 minutes old. Future times over 5 minutes "
    "are invalid. Bike and dock counts do not necessarily add up to capacity."
)

st.subheader("Current station map")
search = st.text_input("Search stations", placeholder="Station name or ID")
view = st.selectbox(
    "Show on map", ["All stations", "Bikes available", "Return docks available", "Needs attention"]
)
filtered = stations[
    stations["name"].str.contains(search, case=False, regex=False)
    | stations["station_id"].str.contains(search, case=False, regex=False)
]
if view == "Bikes available":
    filtered = filtered[filtered["rental_state"].eq("Available")]
elif view == "Return docks available":
    filtered = filtered[filtered["return_state"].eq("Available")]
elif view == "Needs attention":
    filtered = filtered[
        filtered["rental_state"].ne("Available") | filtered["return_state"].ne("Available")
    ]
if filtered.empty:
    st.info("No stations match the current search and map filter.")
else:
    st.map(filtered[["lat", "lon"]], latitude="lat", longitude="lon", size=45)
    st.caption("Each point is a station in the filtered latest collection. Select a station below.")
    st.dataframe(
        filtered[
            [
                "name",
                "station_id",
                "num_bikes_available",
                "num_docks_available",
                "rental_state",
                "return_state",
                "freshness",
            ]
        ].rename(
            columns={
                "name": "Station",
                "station_id": "ID",
                "num_bikes_available": "Recorded bikes",
                "num_docks_available": "Recorded docks",
                "rental_state": "Rentals",
                "return_state": "Returns",
                "freshness": "Report freshness",
            }
        ),
        hide_index=True,
        width="stretch",
    )

st.subheader("Station availability history")
choices = filtered if not filtered.empty else stations
names = dict(zip(choices["station_id"], choices["name"], strict=True))
station_id = st.selectbox(
    "Select station", list(names), format_func=lambda value: f"{names[value]} ({value})"
)
station = stations[stations["station_id"] == station_id].iloc[0]
st.write(f"**Rentals:** {station['rental_state']} · **Returns:** {station['return_state']}")
st.caption(
    f"Station reported: {local_time(station['station_reported_at'])} · "
    f"Feed published: {local_time(station['source_published_at'])} · "
    f"HTTP fetch: {local_time(station['status_fetched_at'])}"
)
days = st.selectbox(
    "Analysis period", [1, 7, 30], index=1, format_func=lambda value: f"Past {value} days"
)
try:
    with st.spinner("Loading observed station history…"):
        history = prepare_history(load_history(settings.database_url, station_id, days, now=now))
except Exception:
    st.error("Station history could not be loaded. Check the database connection and retry.")
    st.stop()
if history.empty:
    st.info("No observed snapshots for this station in the selected period.")
else:
    st.scatter_chart(
        history.set_index("Toronto time")[["Bikes available", "Docks available"]],
        x_label="Collection time (America/Toronto)",
        y_label="Available count",
    )
    st.caption(
        "Each point is an observed snapshot, with freshness evaluated at collection time. "
        "Missing, inactive, stale, and unavailable service observations are excluded per series. "
        "Gaps are not filled and points are not connected. Snapshots do not measure trips or "
        "exact minutes empty/full."
    )
    with st.expander("Inspect source trace and observed snapshots"):
        trace = history.copy()
        for column in (
            "collected_at",
            "status_fetched_at",
            "source_published_at",
            "station_reported_at",
        ):
            trace[column] = trace[column].map(local_time)
        st.dataframe(
            trace.drop(columns=["Toronto time", "Bikes available", "Docks available"]),
            hide_index=True,
            width="stretch",
        )

st.subheader("Observed reliability and coverage")
try:
    with st.spinner("Reading analytical models…"):
        analytics = load_analytics(settings.database_url, station_id, days, now)
except AnalyticsNotBuilt:
    st.info(
        "Analytics are not built yet. Run dbt build to create the staging and analytical views. "
        "The current map and recorded station history remain available above."
    )
    st.code("docker compose run --rm dbt", language="shell")
    st.stop()
except Exception:
    st.error(
        "Analytics could not be read. Check the database connection and dbt build results, "
        "then refresh. This is a query failure, not an empty observation period."
    )
    st.stop()

coverage = 100 * analytics["observed_slots"] / analytics["expected_slots"]
a, b = st.columns(2)
a.metric("System collection coverage", f"{coverage:.1f}%")
b.metric(
    "Observed / expected completed 15-minute slots",
    f"{analytics['observed_slots']} / {analytics['expected_slots']}",
)
st.caption(
    f"Coverage window: {local_time(analytics['slot_start'])} to "
    f"{local_time(analytics['slot_end'])} (end exclusive). Counts complete UTC 15-minute slots "
    "inside the selected period with at least one successful collection containing observations. "
    "Repeated collections within a slot count once. Partial boundary slots are excluded. "
    "Time before collection began stays uncovered; a covered slot does not mean every station "
    "reported or passed freshness checks."
)

st.write(f"**Availability by Toronto hour: {names[station_id]}**")
if analytics["hourly"].empty:
    st.info("No hourly station observations in the selected period.")
else:
    hourly = analytics["hourly"].set_index("toronto_hour").reindex(range(24))
    means = (
        hourly[["mean_bikes", "mean_docks"]]
        .apply(pd.to_numeric)
        .rename(
            columns={"mean_bikes": "Mean available bikes", "mean_docks": "Mean available docks"}
        )
    )
    means.index.name = "Toronto hour (0–23)"
    if means.notna().any().any():
        st.bar_chart(means, stack=False, x_label="Toronto hour (0–23)", y_label="Mean count")
    else:
        st.info("No eligible bike or dock observations for an hourly average in this period.")
    st.caption(
        "Snapshot-weighted means use separate rental and return eligibility. Hours without "
        "eligible observations remain blank. Both occurrences of a repeated daylight-saving "
        "hour are grouped together. Early results may contain very few snapshots."
    )
    with st.expander("Hourly observation counts"):
        st.dataframe(analytics["hourly"], hide_index=True, width="stretch")

st.write("**Stations frequently observed empty or full**")
st.caption(
    f"Period: {local_time(analytics['period_start'])} to "
    f"{local_time(analytics['period_end'])} (end exclusive). Empty % = empty rental-eligible "
    "snapshots / all rental-eligible snapshots. Full % uses a separate return-eligible "
    "denominator. Eligibility requires a reported, installed station, its relevant service "
    "enabled, and fresh source/station times at collection. Missing or stale observations "
    "are excluded. These are percentages of observed snapshots, not percentages of time."
)
minimum = st.number_input("Minimum eligible snapshots per ranking", min_value=1, value=1, step=1)
rankings = analytics["rankings"]
if rankings.empty:
    st.info("No station snapshots in the selected period.")
else:
    empty_tab, full_tab = st.tabs(["Frequently empty", "Frequently full"])
    for metric, tab in (("empty", empty_tab), ("full", full_tab)):
        with tab:
            ranked = ranked_stations(rankings, metric, int(minimum))
            eligible = (
                "rental_eligible_snapshots" if metric == "empty" else "return_eligible_snapshots"
            )
            if ranked.empty:
                st.info(
                    f"No stations meet the minimum eligible snapshot count for {metric} ranking."
                )
            else:
                st.dataframe(
                    ranked[
                        [
                            "name",
                            "station_id",
                            f"{metric}_pct",
                            f"{metric}_snapshots",
                            eligible,
                            "observed_snapshots",
                            "metadata_snapshots",
                        ]
                    ].rename(
                        columns={
                            "name": "Station",
                            "station_id": "ID",
                            f"{metric}_pct": f"{metric.title()} %",
                            f"{metric}_snapshots": f"{metric.title()} snapshots",
                            eligible: "Eligible snapshots",
                            "observed_snapshots": "Observed snapshots",
                            "metadata_snapshots": "Metadata snapshots",
                        }
                    ),
                    column_config={
                        f"{metric.title()} %": st.column_config.NumberColumn(format="%.1f")
                    },
                    hide_index=True,
                    width="stretch",
                )
            st.caption(
                f"{len(ranked)} of {len(rankings)} stations meet this denominator threshold. "
                "Observed snapshots count status records; metadata snapshots count collections "
                "listing that station, including a missing status. Neither count is scheduled "
                "time coverage. Stations with zero eligible snapshots have no defined percentage."
            )
