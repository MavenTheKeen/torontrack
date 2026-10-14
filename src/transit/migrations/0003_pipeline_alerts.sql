-- Phone alerts for pipeline health (transit.alerts): the current state of each checked
-- component, and every notification attempted.

CREATE TABLE ops.alert_state (
    component text PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('ok', 'delayed', 'failing')),
    -- When the component entered its current status.
    since timestamptz NOT NULL,
    -- The status the last delivered notification reported.
    notified_status text NOT NULL DEFAULT 'ok'
        CHECK (notified_status IN ('ok', 'delayed', 'failing')),
    checked_at timestamptz NOT NULL
);

CREATE TABLE ops.alerts (
    alert_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    component text NOT NULL,
    status text NOT NULL,
    title text NOT NULL,
    message text NOT NULL,
    delivered boolean NOT NULL,
    error text
);
