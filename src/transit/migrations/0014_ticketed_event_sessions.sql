-- Timed-entry attractions list every slot as an event; the loader keeps one row per
-- name, venue and day with the number of sessions (transit.ticketmaster).
ALTER TABLE normalized.ticketed_events ADD COLUMN sessions integer NOT NULL DEFAULT 1;
