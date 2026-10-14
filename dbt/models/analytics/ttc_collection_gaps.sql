{{
    config(
        materialized='incremental',
        unique_key='gap_start',
        incremental_strategy='delete+insert',
        on_schema_change='fail',
    )
}}
-- Every collection gap, kept after poll runs expire (30 days) so the report can state
-- its full coverage. Each run re-reads the last two days in case a gap was still open.
select gap_start, gap_end, gap_seconds
from {{ ref('stg_ttc_collection_gaps') }}
{% if is_incremental() %}
    where gap_end >= (select coalesce(max(gap_start), timestamptz '1900-01-01') - interval '2 days'
                      from {{ this }})
{% endif %}
