# Verification results

- Python 3.12.14; offline suite: **43 passed, 0 failed, 0 errors**.
- Socket connections explicitly blocked for the whole test suite.
- Python AST syntax validation: 3 files passed.
- Config, purpose metadata and GitHub workflow YAML parsed successfully.
- Workflow schedule, state write permission and concurrency assertions passed.
- Bash runtime unavailable locally; shell blocks inspected but not executed or shell-parser validated.
- ZIP CRC and required-file checks passed during packaging.
- Live Binance/Telegram requests, GitHub-hosted execution and strategy performance were not tested.

Reproduce: `python -m unittest discover -s tests -v` from project root after installing requirements.

Coverage includes EMA/RSI/ATR, trend and pivots, scoring, levels/R:R rejection, liquidity/spread, age boundaries, absent/corrupt state, UTC cap reset, cooldown, sector limits, correlation, Telegram formatting and timeout secrecy, HTTP retry/cache, stale data, dry-run non-mutation, BTC suppression, reservation persistence, failure-before-send, and test-only mode.
