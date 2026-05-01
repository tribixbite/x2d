#!/system/bin/sh
# Apply iptables NAT to redirect Bambu's (uid 10217) outbound 443/80 -> localhost:18080.
# Run as root.

set -u
UID_BAMBU=10217
MITM_PORT=18080

# Use Termux's iptables since Android's may be missing -m owner
IPT=/data/data/com.termux/files/usr/bin/iptables
[ -x "$IPT" ] || IPT=/system/bin/iptables

echo "Using iptables: $IPT"
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib $IPT --version 2>&1 | head

# Wipe any prior rules from earlier runs
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib $IPT -t nat -F OUTPUT 2>&1 || true

# Insert: skip mitmdump's own traffic (uid 10212) so it can connect upstream
# Bambu (uid 10217) outbound 443 -> redirect to local 18080
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib $IPT -t nat -A OUTPUT -p tcp -m owner --uid-owner $UID_BAMBU --dport 443 -j REDIRECT --to-port $MITM_PORT
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib $IPT -t nat -A OUTPUT -p tcp -m owner --uid-owner $UID_BAMBU --dport 80 -j REDIRECT --to-port $MITM_PORT
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib $IPT -t nat -A OUTPUT -p tcp -m owner --uid-owner $UID_BAMBU --dport 8883 -j REDIRECT --to-port $MITM_PORT

# Same for IPv6
IPT6=$(echo "$IPT" | sed 's/iptables/ip6tables/')
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib $IPT6 -t nat -F OUTPUT 2>&1 || true
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib $IPT6 -t nat -A OUTPUT -p tcp -m owner --uid-owner $UID_BAMBU --dport 443 -j REDIRECT --to-port $MITM_PORT 2>&1 || true
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib $IPT6 -t nat -A OUTPUT -p tcp -m owner --uid-owner $UID_BAMBU --dport 80  -j REDIRECT --to-port $MITM_PORT 2>&1 || true
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib $IPT6 -t nat -A OUTPUT -p tcp -m owner --uid-owner $UID_BAMBU --dport 8883 -j REDIRECT --to-port $MITM_PORT 2>&1 || true

echo "=== nat OUTPUT v4 ==="
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib $IPT -t nat -L OUTPUT -n -v
echo "=== nat OUTPUT v6 ==="
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib $IPT6 -t nat -L OUTPUT -n -v 2>&1 || true
