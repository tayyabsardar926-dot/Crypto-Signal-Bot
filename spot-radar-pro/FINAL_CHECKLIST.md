# Final replacement checklist

- [ ] Download a backup of existing repository.
- [ ] Disable old scheduled bot workflow and remove its obsolete .yml file.
- [ ] Extract final ZIP; upload extracted contents, not ZIP, to default-branch root.
- [ ] Confirm `signal_bot.py`, `config.yaml`, `assets.yaml`, `requirements.txt`, `tests/`, `scripts/` and `.github/workflows/signal-bot.yml` are present.
- [ ] Preserve unrelated repository files and any existing `signal-bot-state` branch.
- [ ] Preserve EXACT existing secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- [ ] Allow Actions write permission for its dedicated state branch.
- [ ] Run Actions > Crypto Spot Signal Bot > Run workflow > test-telegram.
- [ ] Run dry-run and inspect diagnostics, then scan. NO TRADE is acceptable.
- [ ] Confirm only one scheduled bot workflow remains active.
- [ ] Keep repo public for intended free standard hosted-runner setup; review account quota if private.

Validation evidence is recorded in `TEST_RESULTS.md`. Tests do not establish investment performance. Live Telegram/GitHub/exchange connectivity must be checked through the above manual runs.
