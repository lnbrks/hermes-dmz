# Systemd setup (user-level)

These are user units; no root needed.

```bash
mkdir -p ~/.config/systemd/user
ln -sf ~/src/ai-inbox-cleanup/agent/systemd/cleanup-agent.service \
       ~/.config/systemd/user/cleanup-agent.service
ln -sf ~/src/ai-inbox-cleanup/agent/systemd/cleanup-agent.timer \
       ~/.config/systemd/user/cleanup-agent.timer

systemctl --user daemon-reload
systemctl --user enable --now cleanup-agent.timer
systemctl --user list-timers cleanup-agent.timer
```

Notification config goes in `~/src/ai-inbox-cleanup/.env`:

```
NTFY_TOPIC=your-secret-topic-name
# Optional:
# NTFY_SERVER=https://ntfy.sh
# NTFY_TOKEN=tk_...
```

To run a one-off without waiting for the timer:

```bash
systemctl --user start cleanup-agent.service
journalctl --user -u cleanup-agent.service -f
```

To dry-run just the apply step:

```bash
~/src/ai-inbox-cleanup/agent/bin/apply-labels --run-dir <latest-run-dir> --dry-run
```

Cadence is daily (07:00 local) by default. Change `OnCalendar=` in the timer
file. Common values: `daily`, `weekly`, `Mon *-*-* 09:00:00`.

Enabling lingering (so timers fire even when not logged in):

```bash
sudo loginctl enable-linger $USER
```
