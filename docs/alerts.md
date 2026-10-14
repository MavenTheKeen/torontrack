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
# In .env (ignored by Git):
NTFY_URL=https://ntfy.sh/transit-alerts-<random text>
docker compose up -d alerts
```

Subscribe to the same topic in the ntfy app. Without `NTFY_URL`, alerts are recorded but
not sent.

## When the whole machine stops

A service cannot report its own absence: if the computer sleeps, Docker stops or the
power goes out, nothing inside the stack runs. For that, set `HEARTBEAT_URL` to a dead
man's switch such as a free [healthchecks.io](https://healthchecks.io) check with a
1-minute period. The service pings it after every successful check, and healthchecks.io
notifies you (including through ntfy) when the pings stop. When monitoring resumes after
more than 5 minutes, the service also sends "Monitoring resumed" with the length of the
gap, since nothing was collected during it.

## Records

`ops.alert_state` holds each component's current status, when it began and what the last
delivered notification said. `ops.alerts` records every notification attempted, with
`delivered` and the error if sending failed; undelivered alerts are retried at the next
check.

```sql
SELECT created_at, title, delivered FROM ops.alerts ORDER BY created_at DESC LIMIT 20;
```
