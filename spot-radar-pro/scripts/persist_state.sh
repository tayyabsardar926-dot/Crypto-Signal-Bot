#!/usr/bin/env bash
set -euo pipefail
test -f state/state.json
cp state/state.json .state-store/state.json
git -C .state-store add state.json
if ! git -C .state-store diff --cached --quiet; then
  git -C .state-store commit -m "Persist scanner state [skip ci]"
  git -C .state-store push origin HEAD:signal-bot-state
fi
