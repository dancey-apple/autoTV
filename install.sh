#!/usr/bin/env bash
# Schedule autotv with systemd user timers.
#
#   ./install.sh 08:00          turn on and play every day at 08:00
#   ./install.sh 08:00 22:30    ...and turn off at 22:30
#   ./install.sh --remove       remove the schedule
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now autotv-on.timer autotv-off.timer 2>/dev/null || true
    rm -f "$units"/autotv-on.{service,timer} "$units"/autotv-off.{service,timer}
    systemctl --user daemon-reload
    echo "Schedule removed."
    exit 0
fi

on_time="${1:?usage: ./install.sh ON_TIME [OFF_TIME]   (24-hour HH:MM)}"
off_time="${2:-}"
for t in "$on_time" $off_time; do
    [[ "$t" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "Not a HH:MM time: $t" >&2; exit 1; }
done

mkdir -p "$units"
[ -f "$dir/config.toml" ] || cp "$dir/config.example.toml" "$dir/config.toml"

write_units() {  # name, command, time
    cat > "$units/autotv-$1.service" <<EOF
[Unit]
Description=autotv $1
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 "$dir/autotv.py" $2
EOF
    cat > "$units/autotv-$1.timer" <<EOF
[Unit]
Description=autotv $1 daily at $3

[Timer]
OnCalendar=*-*-* $3:00
Persistent=false

[Install]
WantedBy=timers.target
EOF
}

write_units on on "$on_time"
if [ -n "$off_time" ]; then
    write_units off off "$off_time"
else
    systemctl --user disable --now autotv-off.timer 2>/dev/null || true
    rm -f "$units"/autotv-off.{service,timer}
fi

systemctl --user daemon-reload
systemctl --user enable --now autotv-on.timer
[ -z "$off_time" ] || systemctl --user enable --now autotv-off.timer

# Let the timers run when nobody is logged in to the Pi.
loginctl enable-linger "$USER" 2>/dev/null || \
    echo "Note: run 'sudo loginctl enable-linger $USER' so the schedule runs without a login."

echo "Scheduled: on at $on_time${off_time:+, off at $off_time}."
systemctl --user list-timers 'autotv-*' --no-pager
