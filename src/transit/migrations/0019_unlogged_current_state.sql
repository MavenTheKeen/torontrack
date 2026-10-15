-- Tables holding only the current state of a live feed, rewritten every 10 to 30 seconds,
-- skip the write-ahead log: less disk writing. After a crash PostgreSQL empties them, and
-- the collectors refill them within a poll.

ALTER TABLE normalized.ttc_vehicles SET UNLOGGED;
ALTER TABLE normalized.ttc_current_predictions SET UNLOGGED;
ALTER TABLE normalized.ttc_surface_predictions SET UNLOGGED;
ALTER TABLE normalized.agency_vehicles SET UNLOGGED;
ALTER TABLE normalized.agency_predictions SET UNLOGGED;
