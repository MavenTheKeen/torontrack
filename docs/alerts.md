# Phone alerts

The `alerts` service checks pipeline health every minute with the same rules as the
[status page](site.md): each TTC feed, Bike Share collection and the dbt build is `ok`,
`delayed` or `failing` by time since its last success. A component that stays unhealthy
for 3 minutes sends one notification; moving between delayed and failing is the same
incident, and one more notification says when it recovers. A single slow poll never
alerts.

Notifications go to [ntfy](https://ntfy.sh), a free push service with Android and iOS
apps and no account. Anyone who knows a topic name can read it, so use a long random
name:

```powershell
# In secrets/ (ignored by Git; see deploy.md#secrets):
Set-Content -NoNewline secrets/ntfy_url 'https://ntfy.sh/transit-alerts-<random text>'
docker compose up -d alerts
```

Subscribe to the same topic in the ntfy app. With `secrets/ntfy_url` empty, alerts are
recorded but not sent.

## When the whole machine stops

A service cannot report its own absence: if the computer sleeps, Docker stops or the
power goes out, nothing inside the stack runs. For that, put in `secrets/heartbeat_url` a dead
man's switch such as a free [healthchecks.io](https://healthchecks.io) check with a
1-minute period. The service pings it after every successful check, and healthchecks.io
notifies you (including through ntfy) when the pings stop. When monitoring resumes after
more than 5 minutes, the service also sends "Monitoring resumed" with the length of the
gap, since nothing was collected during it.

## Site availability

With `SITE_URL` set in `.env` (the public address, for example
`https://torontrack.example`), each check also requests `/api/lines` through that address,
so the proxy, the web process and the database are all on the path, and records the result
in `ops.site_checks` (kept 120 days). The pipeline page shows the last 90 days, one square
per day, from `/api/uptime`.

Time counts as available when a check succeeded and the one before it was at most 3
minutes earlier. A failed check counts as unavailable, and so does a longer silence: the
server or this monitor was down, and from the record the two cannot be told apart. Each
update restarts the site for about a minute, which shows as that much unavailability.

## Notifications for visitors

A line's page has a **Notify me** button where the browser supports push notifications
(on an iPhone, after the site is added to the Home Screen). The browser asks permission,
then hands the site a subscription: an address at its maker's push service and two keys.
The site stores that and the lines chosen (`push.subscriptions`), with no account, address
or name. Turning the last line off deletes the row.

Each check compares every line's reported status (the most severe active TTC service
alert) with the last one seen (`push.line_state`) and sends one message per change to the
subscriptions for that line, including the return to normal service. Nothing is sent while
TTC's alert feed is stale, and the first check after a fresh install only records. A new
subscription gets one confirmation message within a minute. A subscription the push service
reports as ended is deleted; one that fails five times in a row is deleted too.

Messages are encrypted for the one browser and signed with the site's key
(`secrets/vapid_private_key`, a P-256 key in PEM form). `deploy/make_secrets.sh` creates
the key once with `openssl`; with an empty file the feature is off and the button hidden.
Replacing the key ends every existing subscription. The implementation
(`transit.push`) is checked against the worked example in RFC 8291.

## Records

`ops.alert_state` holds each component's current status, when it began and what the last
delivered notification said. `ops.alerts` records every notification attempted, with
`delivered` and the error if sending failed; undelivered alerts are retried at the next
check.

```sql
SELECT created_at, title, delivered FROM ops.alerts ORDER BY created_at DESC LIMIT 20;
```
