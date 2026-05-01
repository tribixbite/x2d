#!/system/bin/sh
# Run as root via Magisk su.
# Sets up a hosts-file bind mount that's visible from Termux's mount namespace.
# Strategy: nsenter into init's mount NS (PID 1, the global one), bind-mount there
# so every later-fork (including Termux app process when it next launches) inherits.
# If init NS unreachable, fall back to per-app: nsenter Termux app proc and bind there.

set -u

HOSTS_DIR=/data/local/tmp/hosts_overlay
HOSTS_FILE=$HOSTS_DIR/hosts

mkdir -p "$HOSTS_DIR"
cat > "$HOSTS_FILE" <<EOF
127.0.0.1       localhost
::1             ip6-localhost
# Termux mirrors
104.21.20.145   packages-cf.termux.org
172.67.193.33   packages-cf.termux.dev
# pypi + crates (Fastly CDN, all share IP 151.101.x)
151.101.0.223   pypi.org
151.101.0.223   files.pythonhosted.org
151.101.0.223   pythonhosted.org
151.101.66.137  crates.io
151.101.66.137  index.crates.io
151.101.66.137  static.crates.io
151.101.66.137  static.rust-lang.org
151.101.66.137  dualstack.k.sni.global.fastly.net
# github (in case any pip dep pulls from VCS)
140.82.112.4    github.com
185.199.108.133 raw.githubusercontent.com
185.199.108.133 objects.githubusercontent.com
140.82.112.10   codeload.github.com
# Bambu (we'll need these resolvable for mitm tests later)
54.230.10.5     bbl.intl.bambulab.com
54.230.10.5     api.bambulab.com
54.230.10.5     us.api.bambulab.com
EOF
chmod 644 "$HOSTS_FILE"

# Find Termux app PID (may be 0 if not running). For each running termux/bambu PID,
# bind-mount over /system/etc/hosts inside that NS using nsenter.
for proc in /proc/[0-9]*/cmdline; do
  pid=${proc#/proc/}; pid=${pid%/cmdline}
  cmd=$(tr '\0' ' ' < "$proc" 2>/dev/null)
  case "$cmd" in
    com.termux*|*com.termux*|*bbl.intl.bambulab.com*|com.android.systemui*|system_server*|init*)
      ns_inode=$(readlink "/proc/$pid/ns/mnt" 2>/dev/null)
      echo "PID $pid ($cmd) -> $ns_inode"
      ;;
  esac
done

# Use nsenter from toybox into PID 1 mount NS and bind there.
# On Magisk-rooted devices, PID 1 is init in the global root NS that survives boot.
echo "=== Bind in PID 1 mount NS ==="
nsenter -t 1 -m -- mount --bind "$HOSTS_FILE" /system/etc/hosts 2>&1
nsenter -t 1 -m -- cat /system/etc/hosts 2>&1 | head -3
echo "=== Re-mount on init NS done ==="

# Bind in our own NS too (so this shell can also see it)
mount --bind "$HOSTS_FILE" /system/etc/hosts 2>&1 || true

# Bind into every existing Termux/bambu process NS
for proc in /proc/[0-9]*/cmdline; do
  pid=${proc#/proc/}; pid=${pid%/cmdline}
  cmd=$(tr '\0' ' ' < "$proc" 2>/dev/null)
  case "$cmd" in
    *com.termux*|*bbl.intl.bambulab.com*)
      echo "Bind into PID $pid ($cmd)"
      nsenter -t "$pid" -m -- mount --bind "$HOSTS_FILE" /system/etc/hosts 2>&1
      ;;
  esac
done

echo "=== Verify from default NS ==="
cat /system/etc/hosts | head -8
