-- analytics.ttc_longest_gaps was replaced by ttc_long_gaps, which the web app groups into
-- delay incidents. dbt does not drop models it no longer has, so the old table stays.
DROP TABLE IF EXISTS analytics.ttc_longest_gaps;
