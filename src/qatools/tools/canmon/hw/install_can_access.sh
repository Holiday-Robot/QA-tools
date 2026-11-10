#!/bin/bash
# One-time setup on the measurement laptop so the CAN monitor can set bit rates without a password.
#
#   sudo ./install_can_access.sh [user ...]
#
# What it does:
#   1. copies canmon-setup to /usr/local/sbin (owned by root, not writable by others)
#   2. creates the group "canmon" and adds the given users (default: the user who ran sudo)
#   3. adds /etc/sudoers.d/canmon: members of "canmon" may run only that script as root without a password
# Undo: sudo rm /etc/sudoers.d/canmon /usr/local/sbin/canmon-setup
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
users=("$@")
[ ${#users[@]} -gt 0 ] || users=("${SUDO_USER:?give a user name}")

install -o root -g root -m 0755 "$here/canmon-setup" /usr/local/sbin/canmon-setup
getent group canmon >/dev/null || groupadd canmon
for u in "${users[@]}"; do
  id "$u" >/dev/null
  usermod -aG canmon "$u"
done

tmp=$(mktemp)
echo '%canmon ALL=(root) NOPASSWD: /usr/local/sbin/canmon-setup' > "$tmp"
visudo -cf "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/canmon
rm -f "$tmp"

echo "Installed. Users in group canmon: ${users[*]}"
echo "Log out and back in once so the new group takes effect, then test:"
echo "  sudo -n /usr/local/sbin/canmon-setup show can0"
