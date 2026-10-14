select collection_id, station_id
from {{ ref('stg_station_observations') }}
where
    (not observed and (rental_eligible or return_eligible or is_empty or is_full))
    or (not is_fresh and (rental_eligible or return_eligible))
    or (rental_eligible and (not is_installed or not is_renting))
    or (return_eligible and (not is_installed or not is_returning))
    or (is_empty and (not rental_eligible or num_bikes_available <> 0))
    or (is_full and (not return_eligible or num_docks_available <> 0))
    or (observed and (
        num_bikes_available is null or num_docks_available is null
        or station_reported_at is null or status_fetched_at is null
        or source_published_at is null
    ))
