-- The site's own availability (transit.uptime): one row per check of the public address,
-- made once a minute by the alerts process.
CREATE TABLE ops.site_checks (
    checked_at timestamptz PRIMARY KEY,
    ok boolean NOT NULL,
    ms integer
);

-- Browser notifications (transit.push). The schema is separate from those the public
-- site's login can read: a subscription's address and keys are for the sender only. The
-- site reaches it through the functions below and nothing else.
CREATE SCHEMA push;

CREATE TABLE push.subscriptions (
    endpoint text PRIMARY KEY,
    p256dh text NOT NULL,
    auth text NOT NULL,
    lines text[] NOT NULL,
    confirm_due boolean NOT NULL DEFAULT true,
    failures integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX subscriptions_lines ON push.subscriptions USING gin (lines);

CREATE TABLE push.line_state (
    line text PRIMARY KEY,
    status text NOT NULL,
    changed_at timestamptz NOT NULL
);

CREATE TABLE push.config (
    name text PRIMARY KEY,
    value text NOT NULL
);

-- The key browsers subscribe with; null until the sender has started with a key.
CREATE FUNCTION push.public_key() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = push, pg_temp AS $$
    SELECT value FROM push.config WHERE name = 'public_key'
$$;

-- The lines a subscription has chosen. Knowing the address is the only credential, and
-- only the browser and its push service know it.
CREATE FUNCTION push.lines(p_endpoint text) RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = push, pg_temp AS $$
    SELECT coalesce((SELECT lines FROM push.subscriptions WHERE endpoint = p_endpoint), '{}')
$$;

-- Set a subscription's lines; none removes it. A cap on the table bounds what a flood
-- of made-up subscriptions could store. Returns the lines now kept.
CREATE FUNCTION push.subscribe(p_endpoint text, p_p256dh text, p_auth text, p_lines text[])
RETURNS text[]
LANGUAGE plpgsql SECURITY DEFINER SET search_path = push, pg_temp AS $$
BEGIN
    IF coalesce(cardinality(p_lines), 0) = 0 THEN
        DELETE FROM push.subscriptions WHERE endpoint = p_endpoint;
        RETURN '{}';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM push.subscriptions WHERE endpoint = p_endpoint)
        AND (SELECT count(*) FROM push.subscriptions) >= 5000 THEN
        RAISE EXCEPTION 'subscription limit reached' USING ERRCODE = '53400';
    END IF;
    INSERT INTO push.subscriptions (endpoint, p256dh, auth, lines)
    VALUES (p_endpoint, p_p256dh, p_auth, p_lines)
    ON CONFLICT (endpoint) DO UPDATE
    SET p256dh = excluded.p256dh, auth = excluded.auth, lines = excluded.lines,
        confirm_due = subscriptions.confirm_due
            OR NOT (excluded.lines <@ subscriptions.lines),
        failures = 0;
    RETURN p_lines;
END
$$;

REVOKE ALL ON SCHEMA push FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA push FROM PUBLIC;
