#!/usr/bin/env bash
# Bring up a PCAN-USB Pro FD channel in listen-only CAN FD mode for FM26C capture.
#
# Usage: sudo ./can_setup.sh <iface> <local|dcan>
#   local : local CAN line (HETC ch0/ch1)  1 Mbps / 5 Mbps, sample point 87.5 % / 62.5 %
#   dcan  : shared D-CAN                   125 kbps / 1 Mbps (sample point not confirmed)
#
# Listen-only keeps the analyzer from sending ACKs or error frames onto the robot bus.
# The PCAN-USB driver (peak_usb) is in the mainline kernel; check with: ip -details link show <iface>
set -euo pipefail

iface=${1:?iface, e.g. can0}
mode=${2:?local or dcan}

case "$mode" in
  local) args=(bitrate 1000000 sample-point 0.875 dbitrate 5000000 dsample-point 0.625) ;;
  dcan)  args=(bitrate 125000 dbitrate 1000000) ;;
  *) echo "mode must be local or dcan" >&2; exit 2 ;;
esac

ip link set "$iface" down 2>/dev/null || true
ip link set "$iface" type can "${args[@]}" fd on listen-only on berr-reporting on
ip link set "$iface" txqueuelen 1000
ip link set "$iface" up
ip -details -statistics link show "$iface"
