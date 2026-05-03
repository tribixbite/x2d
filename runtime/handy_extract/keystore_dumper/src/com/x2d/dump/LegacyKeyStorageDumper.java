package com.x2d.dump;

import java.io.File;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.util.Base64;
import javax.crypto.Cipher;
import javax.xml.parsers.DocumentBuilderFactory;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

/**
 * Dump the legacy flutter_secure_storage RSA-wrapped AES key from
 *   shared_prefs/FlutterSecureKeyStorage.xml
 * by RSA-unwrapping with the AndroidKeyStore alias
 *   bbl.intl.bambulab.com.FlutterSecureStoragePluginKey
 *
 * The plugin tries multiple paddings across versions; we walk both the
 * v3 (RSA-OAEP-SHA1) and v2 (RSA-PKCS1) flavours and print whichever
 * succeeds. Once we have the AES key, we hex-print it so an offline
 * step can search the rest of the data dir for AES/CBC-encrypted
 * blobs that decrypt cleanly with this key.
 */
public class LegacyKeyStorageDumper {

    private static final String XML_PATH =
        "/data/data/bbl.intl.bambulab.com/shared_prefs/FlutterSecureKeyStorage.xml";
    private static final String RSA_ALIAS =
        "bbl.intl.bambulab.com.FlutterSecureStoragePluginKey";
    /** Plugin's well-known wrap-key entry name (base64 of "This is the key..."). */
    private static final String WRAP_KEY_ENTRY =
        "VGhpcyBpcyB0aGUga2V5IGZvciBhIHNlY3VyZSBzdG9yYWdlIEFFUyBLZXkK";

    public static void main(String[] args) throws Exception {
        installKeystoreProvider();
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        PrivateKey rsa = (PrivateKey) ks.getKey(RSA_ALIAS, null);
        if (rsa == null) throw new IllegalStateException("RSA key absent — wrong UID?");
        System.out.println("[+] RSA key acquired: alias=" + RSA_ALIAS + " algo=" + rsa.getAlgorithm());

        org.w3c.dom.Document doc = DocumentBuilderFactory.newInstance()
            .newDocumentBuilder().parse(new File(XML_PATH));
        NodeList strings = doc.getElementsByTagName("string");
        String wrappedB64 = null;
        for (int i = 0; i < strings.getLength(); i++) {
            Element e = (Element) strings.item(i);
            String name = e.getAttribute("name");
            if (WRAP_KEY_ENTRY.equals(name)) {
                wrappedB64 = e.getTextContent().trim().replaceAll("\\s+", "");
                break;
            }
        }
        if (wrappedB64 == null) throw new IllegalStateException("wrap-key entry not found");
        byte[] wrapped = Base64.getDecoder().decode(wrappedB64);
        System.out.println("[+] wrapped blob: " + wrapped.length + " bytes");

        // Try paddings flutter_secure_storage uses across versions.
        String[] transforms = {
            "RSA/ECB/OAEPwithSHA-256andMGF1Padding",
            "RSA/ECB/OAEPWithSHA-1AndMGF1Padding",
            "RSA/ECB/PKCS1Padding"
        };
        byte[] aesKey = null;
        String winner = null;
        for (String tr : transforms) {
            try {
                Cipher c = Cipher.getInstance(tr);
                c.init(Cipher.DECRYPT_MODE, rsa);
                byte[] out = c.doFinal(wrapped);
                aesKey = out;
                winner = tr;
                break;
            } catch (Exception ex) {
                System.out.println("[!] " + tr + " -> " + ex.getMessage());
            }
        }
        if (aesKey == null) throw new IllegalStateException("no padding worked");

        System.out.println("[+] unwrapped with " + winner + " -> " + aesKey.length + " bytes");
        StringBuilder hex = new StringBuilder();
        for (byte b : aesKey) hex.append(String.format("%02x", b));
        System.out.println("AES_KEY_HEX=" + hex);
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
