package com.x2d.dump;

import java.security.KeyStore;
import java.security.cert.Certificate;
import java.util.Enumeration;

/**
 * Phase 1 of the Magisk-side cert extractor: list every AndroidKeyStore
 * alias visible to the calling UID, plus minimal metadata. Run via:
 *
 *   adb shell su -c 'su 10232 -c "/system/bin/app_process \
 *     -Djava.class.path=/data/local/tmp/x2d_dumper.dex / \
 *     com.x2d.dump.ListAliases"'
 *
 * Validates the Magisk -> setuid(10232) -> app_process pipeline and
 * confirms keystore2 is happy with the binder caller UID before we
 * layer on AndroidX EncryptedSharedPreferences in phase 2.
 */
public class ListAliases {
    public static void main(String[] args) throws Exception {
        // app_process runs outside zygote -> the AndroidKeyStore JCE
        // provider isn't installed. Pull it in by reflection (the
        // class moved between API levels and we don't want to bind
        // to either flavour at compile time).
        for (String klass : new String[] {
                "android.security.keystore2.AndroidKeyStoreProvider",
                "android.security.keystore.AndroidKeyStoreProvider"}) {
            try {
                Class.forName(klass).getMethod("install").invoke(null);
                System.out.println("installed=" + klass);
                break;
            } catch (Throwable ignored) { /* try next */ }
        }
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        System.out.println("uid=" + android.os.Process.myUid()
                         + " pid=" + android.os.Process.myPid());
        Enumeration<String> aliases = ks.aliases();
        while (aliases.hasMoreElements()) {
            String alias = aliases.nextElement();
            System.out.println("alias=" + alias);
            try {
                Certificate cert = ks.getCertificate(alias);
                if (cert != null) {
                    System.out.println("  type=" + cert.getType()
                                     + " key-algo=" + cert.getPublicKey().getAlgorithm()
                                     + " cert-len=" + cert.getEncoded().length);
                }
                KeyStore.Entry e = ks.getEntry(alias, null);
                System.out.println("  entry-class=" + (e == null ? "<null>" : e.getClass().getSimpleName()));
            } catch (Exception ex) {
                System.out.println("  err=" + ex.getClass().getSimpleName() + ": " + ex.getMessage());
            }
        }
    }
}
