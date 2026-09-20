# State examples and recovery

Initial state (created automatically; do not upload credentials):

```json
{"version": 1, "alerts": [], "ages": {}}
```

An alert record contains `symbol`, `sector`, Unix UTC `time`, and `status: reserved`. Reservations are intentionally retained after ambiguous/failed Telegram sends. Last 30 days are retained when a reservation is made; age cache remains. Five/day is a reservation limit, so successful deliveries may be fewer.

In Actions, the first normal scan creates `signal-bot-state`. An absent branch initializes cleanly. Existing branch with missing/corrupt state fails; restore `state.json` from that branch's last valid commit. Never reset state just to force signals. Deliberately deleting state loses caps/cooldowns; if recovering without a backup, disable scans for at least 24 hours first. Local state uses the same schema.

The branch is an independent orphan history and contains only state.json. Normal source uploads do not touch it. `scripts/persist_state.sh` runs only when `PERSIST_STATE_COMMAND=github`; locally, leave that variable unset. Workflow push permission failures block alerts. Do not allow other bots to write this state branch, and do not run the old scheduled workflow concurrently.
