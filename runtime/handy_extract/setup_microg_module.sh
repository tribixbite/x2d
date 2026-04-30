#!/system/bin/sh
# Set up a Magisk module that overlays /product/priv-app/GmsCore + Phonesky
# with newer microG APKs. Requires reboot to take effect.
set -e

MOD=/data/adb/modules/microg_update
echo "creating Magisk module at $MOD"
rm -rf $MOD
mkdir -p $MOD/system/product/priv-app/GmsCore
mkdir -p $MOD/system/product/priv-app/Phonesky

cp /data/local/tmp/gms.apk $MOD/system/product/priv-app/GmsCore/GmsCore.apk
cp /data/local/tmp/vending.apk $MOD/system/product/priv-app/Phonesky/Phonesky.apk
chmod 644 $MOD/system/product/priv-app/GmsCore/GmsCore.apk
chmod 644 $MOD/system/product/priv-app/Phonesky/Phonesky.apk

cat > $MOD/module.prop <<EOF
id=microg_update
name=microG Update
version=v25.09.32
versionCode=250932030
author=user
description=Overlay newer microG GmsCore (25.09.32) + Phonesky (84022630) over the stock-installed older versions
EOF

cat > $MOD/post-fs-data.sh <<EOF
# Ensure proper ownership at boot
chown root:root \$MODPATH/system/product/priv-app/GmsCore/GmsCore.apk 2>/dev/null
chown root:root \$MODPATH/system/product/priv-app/Phonesky/Phonesky.apk 2>/dev/null
EOF
chmod +x $MOD/post-fs-data.sh

# Don't add skip_mount — we WANT it mounted
touch $MOD/update

echo "module installed:"
ls -la $MOD/
echo
echo "system/product/priv-app/GmsCore/:"
ls -la $MOD/system/product/priv-app/GmsCore/
echo "system/product/priv-app/Phonesky/:"
ls -la $MOD/system/product/priv-app/Phonesky/
echo
echo "DONE — reboot required to activate"
