package com.x2d.dump;

import java.security.KeyStore;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;
import java.util.Base64;

/**
 * Export the X.509 self-signed cert (and matching public key) from
 * the AndroidKeyStore alias bbl.intl.bambulab.com.FlutterSecureStoragePluginKey.
 * Tells us whether this RSA key is also Bambu's per-installation X2D
 * MQTT signing key (in which case we can use it via Signature.sign()
 * from this UID-switched process to reproduce signed-publish payloads).
 */
public class ExportCert {
    public static void main(String[] args) throws Exception {
        installKeystoreProvider();
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        String alias = args.length > 0 ? args[0]
            : "bbl.intl.bambulab.com.FlutterSecureStoragePluginKey";
        X509Certificate cert = (X509Certificate) ks.getCertificate(alias);
        if (cert == null) throw new IllegalStateException("alias not found: " + alias);
        System.out.println("alias=" + alias);
        System.out.println("subject=" + cert.getSubjectDN());
        System.out.println("issuer=" + cert.getIssuerDN());
        System.out.println("serial=" + cert.getSerialNumber());
        System.out.println("not-before=" + cert.getNotBefore());
        System.out.println("not-after=" + cert.getNotAfter());
        System.out.println("sig-algo=" + cert.getSigAlgName());
        System.out.println("key-algo=" + cert.getPublicKey().getAlgorithm());
        if (cert.getPublicKey() instanceof java.security.interfaces.RSAPublicKey) {
            java.security.interfaces.RSAPublicKey pk =
                (java.security.interfaces.RSAPublicKey) cert.getPublicKey();
            System.out.println("rsa-modulus-bits=" + pk.getModulus().bitLength());
            System.out.println("rsa-modulus-hex=" + pk.getModulus().toString(16));
            System.out.println("rsa-public-exp=" + pk.getPublicExponent());
        }
        System.out.println("-----BEGIN CERTIFICATE-----");
        String b64 = Base64.getEncoder().encodeToString(cert.getEncoded());
        for (int i = 0; i < b64.length(); i += 64) {
            System.out.println(b64.substring(i, Math.min(i + 64, b64.length())));
        }
        System.out.println("-----END CERTIFICATE-----");
    }

    private static void installKeystoreProvider() {
        for (String klass : new String[] {
                "android.security.keystore2.AndroidKeyStoreProvider",
                "android.security.keystore.AndroidKeyStoreProvider"}) {
            try { Class.forName(klass).getMethod("install").invoke(null); return; }
            catch (Throwable ignored) {}
        }
    }
}
