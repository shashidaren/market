# gold-watcher — RETIRED, do not run

**Disabled on 2026-09-25.** The gold Telegram alerts are no longer used,
and the service never worked with the central secrets setup anyway (it
reads `TELEGRAM_TOKEN` from `gold-watcher/.env`, which does not exist, so
every alert failed — see HANDOFF.md §4.2 of that date).

`check_all.sh` no longer runs this module's preflight and expects the
systemd unit to be **inactive**.

## Keeping it disabled on the server

```bash
systemctl disable --now gold-watcher
rm -f /etc/systemd/system/gold-watcher.service   # optional, removes it entirely
systemctl daemon-reload
```

## If you ever want it back

1. Create `gold-watcher/.env` with `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID`
   — or better, extend `config.py` to call `env_loader.load_env()` and
   fall back to the central `TELEGRAM_BOT_TOKEN`.
2. Reinstall the unit:
   `cp gold-watcher.service /etc/systemd/system/ && systemctl daemon-reload`
3. `systemctl enable --now gold-watcher`
4. Re-add `run_preflight "gold-watcher" "gold-watcher" ...` to `check_all.sh`.

The code in this folder is kept for reference only.
