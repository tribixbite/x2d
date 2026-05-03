package com.x2d.dump;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.Base64;
import javax.crypto.Cipher;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import javax.xml.parsers.DocumentBuilderFactory;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

/**
 * Phase 2 of the cert extractor. Reads
 *   /data/data/bbl.intl.bambulab.com/shared_prefs/FlutterSecureStorage.xml
 * unwraps the AndroidX EncryptedSharedPreferences value-keyset using the
 * AndroidKeyStore master key, then decrypts every entry value and prints
 * any plaintext that looks like a PEM cert / RSA private key block.
 *
 * Key insights that let us avoid pulling in androidx.security:security-crypto
 * and com.google.crypto.tink:tink-android (~700 KB DEX):
 *  - The value keyset is a Tink EncryptedKeyset proto. Field 2 = bytes
 *    encrypted_keyset, encrypted with [12-byte IV][ciphertext][16-byte tag]
 *    AES-256-GCM, AAD = empty, key = AndroidKeyStore alias
 *    "_androidx_security_master_key_".
 *  - The decrypted value keyset is a Tink Keyset proto. We only need
 *    the inner AesGcmKey -> field 3 (bytes key_value) which is the raw
 *    32-byte AES key.
 *  - Each Tink AES-GCM ciphertext (in pref values) is base64'd and laid
 *    out as: [1-byte 0x01][4-byte big-endian key_id][12-byte IV]
 *           [ciphertext][16-byte GCM tag], AAD = empty.
 *  - Pref keys (entry names) are encrypted with deterministic AES-SIV.
 *    We don't bother decrypting them — printing the values is enough
 *    to spot the cert / key blobs by content.
 *
 * Run via:
 *   ./run.sh com.x2d.dump.SecureStorageDumper
 */
public class SecureStorageDumper {

    private static final String XML_PATH =
        "/data/data/bbl.intl.bambulab.com/shared_prefs/FlutterSecureStorage.xml";
    private static final String MASTER_ALIAS = "_androidx_security_master_key_";
    private static final String KEY_KEYSET_NAME =
        "__androidx_security_crypto_encrypted_prefs_key_keyset__";
    private static final String VALUE_KEYSET_NAME =
        "__androidx_security_crypto_encrypted_prefs_value_keyset__";

    public static void main(String[] args) throws Exception {
        installKeystoreProvider();

        // 1. Pull master key from AndroidKeyStore (HW-backed but Cipher
        //    operations work via binder to keystore2).
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        SecretKey master = (SecretKey) ks.getKey(MASTER_ALIAS, null);
        if (master == null) throw new IllegalStateException("master key absent — wrong UID?");
        System.out.println("[+] master key acquired: alias=" + MASTER_ALIAS
                         + " algo=" + master.getAlgorithm());

        // 2. Parse the prefs XML, collect the two reserved keyset entries
        //    plus every other (encrypted) name -> value mapping.
        org.w3c.dom.Document doc = DocumentBuilderFactory.newInstance()
            .newDocumentBuilder()
            .parse(new java.io.File(XML_PATH));
        NodeList strings = doc.getElementsByTagName("string");
        String valueKeysetHex = null;
        java.util.List<String[]> entries = new java.util.ArrayList<>();
        for (int i = 0; i < strings.getLength(); i++) {
            Element e = (Element) strings.item(i);
            String name = e.getAttribute("name");
            String text = e.getTextContent().trim();
            if (VALUE_KEYSET_NAME.equals(name)) {
                valueKeysetHex = text;
            } else if (!KEY_KEYSET_NAME.equals(name)) {
                entries.add(new String[] { name, text });
            }
        }
        if (valueKeysetHex == null) throw new IllegalStateException("value keyset missing");
        System.out.println("[+] " + entries.size() + " encrypted entries to walk");

        // 3. Unwrap the Tink EncryptedKeyset proto -> get the inner AES-GCM key.
        byte[] encryptedKeyset = parseEncryptedKeyset(hexDecode(valueKeysetHex));
        byte[] keysetProtoBytes = aesGcmDecrypt(master, encryptedKeyset, 0, encryptedKeyset.length);
        byte[] aesGcmKey = parseTinkAesGcmKey(keysetProtoBytes);
        System.out.println("[+] value AesGcm key recovered, " + aesGcmKey.length + " bytes");

        SecretKey valueKey = new javax.crypto.spec.SecretKeySpec(aesGcmKey, "AES");

        // 4. Walk every entry value, decrypt, dump.
        for (String[] entry : entries) {
            String name = entry[0];
            byte[] blob = Base64.getDecoder().decode(entry[1]);
            // Tink TINK output prefix = 0x01 + 4-byte key_id.
            if (blob.length < 5 + 12 + 16) {
                System.out.println("[!] entry too short: " + name); continue;
            }
            int off = 5;
            try {
                // AAD = the entry-name string itself (encrypted-key as base64),
                // bound this way by androidx.security EncryptedSharedPreferences
                // to prevent value-swap attacks across keys.
                byte[] aad = name.getBytes(StandardCharsets.UTF_8);
                byte[] plain = aesGcmDecrypt(valueKey, blob, off, blob.length - off, aad);
                // EncryptedSharedPreferences prepends a 1-byte type tag (STRING=2 etc.)
                int type = plain[0] & 0xff;
                String value;
                switch (type) {
                    case 2: // STRING
                    {
                        // bytes 1..4 = big-endian length, then UTF-8.
                        int len = ((plain[1]&0xff)<<24)|((plain[2]&0xff)<<16)
                                  |((plain[3]&0xff)<<8)|(plain[4]&0xff);
                        if (len > 0 && 5 + len <= plain.length) {
                            value = new String(plain, 5, len, StandardCharsets.UTF_8);
                        } else {
                            value = "<bad-string-len " + len + ">";
                        }
                        break;
                    }
                    default:
                        // Print raw bytes (UTF-8 best-effort) — pref types 0..6 incl
                        // INT/LONG/FLOAT/BOOL/STRING_SET; we don't expect those for a
                        // Flutter secure-storage map, but cover gracefully.
                        value = "type=" + type + " raw=" + bytesToPrintable(plain);
                }
                String display = value.length() > 200 ? value.substring(0, 200) + "...[len=" + value.length() + "]" : value;
                System.out.println("--- " + shorten(name) + " ---");
                System.out.println(display);
            } catch (Exception ex) {
                System.out.println("[!] decrypt failed for " + shorten(name) + ": " + ex);
            }
        }
    }

    // ---- helpers --------------------------------------------------------

    /** Parse Tink EncryptedKeyset proto and return field 2 (encrypted_keyset bytes). */
    private static byte[] parseEncryptedKeyset(byte[] proto) {
        // Walk top-level fields, return the bytes payload of field 2.
        int pos = 0;
        while (pos < proto.length) {
            int[] tag = readVarint(proto, pos);
            int fieldNum = tag[0] >>> 3;
            int wireType = tag[0] & 0x7;
            pos = tag[1];
            if (wireType == 2) {
                int[] len = readVarint(proto, pos);
                int payloadLen = len[0];
                pos = len[1];
                if (fieldNum == 2) {
                    byte[] out = new byte[payloadLen];
                    System.arraycopy(proto, pos, out, 0, payloadLen);
                    return out;
                }
                pos += payloadLen;
            } else {
                // varint: skip
                int[] skip = readVarint(proto, pos);
                pos = skip[1];
            }
        }
        throw new IllegalStateException("field 2 not found in EncryptedKeyset");
    }

    /**
     * Parse Tink Keyset (plaintext) proto. Path:
     *   Keyset.key      (field 2, repeated Key)
     *     -> Key.key_data (field 1, KeyData)
     *       -> KeyData.value (field 2, bytes = serialised AesGcmKey)
     *         -> AesGcmKey.key_value (field 3, bytes = raw 32-byte AES key)
     */
    private static byte[] parseTinkAesGcmKey(byte[] keysetProto) {
        int pos = 0;
        while (pos < keysetProto.length) {
            int[] tag = readVarint(keysetProto, pos);
            int fieldNum = tag[0] >>> 3;
            int wireType = tag[0] & 0x7;
            pos = tag[1];
            if (wireType == 2) {
                int[] len = readVarint(keysetProto, pos);
                int payloadLen = len[0];
                pos = len[1];
                if (fieldNum == 2) {
                    // Keyset.Key — find its KeyData (field 1).
                    byte[] keyEntry = java.util.Arrays.copyOfRange(keysetProto, pos, pos + payloadLen);
                    int kp = 0;
                    while (kp < keyEntry.length) {
                        int[] kt = readVarint(keyEntry, kp);
                        int kf = kt[0] >>> 3;
                        int kw = kt[0] & 0x7;
                        kp = kt[1];
                        if (kw == 2) {
                            int[] kl = readVarint(keyEntry, kp);
                            int kpl = kl[0];
                            kp = kl[1];
                            if (kf == 1) {
                                byte[] keyData = java.util.Arrays.copyOfRange(keyEntry, kp, kp + kpl);
                                return parseKeyDataValue(keyData);
                            }
                            kp += kpl;
                        } else {
                            int[] sk = readVarint(keyEntry, kp);
                            kp = sk[1];
                        }
                    }
                }
                pos += payloadLen;
            } else {
                int[] skip = readVarint(keysetProto, pos);
                pos = skip[1];
            }
        }
        throw new IllegalStateException("Key.key_data not found in Keyset");
    }

    /** KeyData proto: field 1 = string type_url, field 2 = bytes value, field 3 = OutputPrefixType. */
    private static byte[] parseKeyDataValue(byte[] keyData) {
        int pos = 0;
        while (pos < keyData.length) {
            int[] tag = readVarint(keyData, pos);
            int fieldNum = tag[0] >>> 3;
            int wireType = tag[0] & 0x7;
            pos = tag[1];
            if (wireType == 2) {
                int[] len = readVarint(keyData, pos);
                int payloadLen = len[0];
                pos = len[1];
                if (fieldNum == 2) {
                    // The value field — itself a serialised AesGcmKey proto.
                    // AesGcmKey: field 3 = bytes key_value (the actual AES key).
                    byte[] inner = new byte[payloadLen];
                    System.arraycopy(keyData, pos, inner, 0, payloadLen);
                    return parseAesGcmKeyValue(inner);
                }
                pos += payloadLen;
            } else {
                int[] skip = readVarint(keyData, pos);
                pos = skip[1];
            }
        }
        throw new IllegalStateException("KeyData.value not found");
    }

    private static byte[] parseAesGcmKeyValue(byte[] aesGcmKey) {
        int pos = 0;
        while (pos < aesGcmKey.length) {
            int[] tag = readVarint(aesGcmKey, pos);
            int fieldNum = tag[0] >>> 3;
            int wireType = tag[0] & 0x7;
            pos = tag[1];
            if (wireType == 2) {
                int[] len = readVarint(aesGcmKey, pos);
                int payloadLen = len[0];
                pos = len[1];
                if (fieldNum == 3) {
                    byte[] out = new byte[payloadLen];
                    System.arraycopy(aesGcmKey, pos, out, 0, payloadLen);
                    return out;
                }
                pos += payloadLen;
            } else {
                int[] skip = readVarint(aesGcmKey, pos);
                pos = skip[1];
            }
        }
        throw new IllegalStateException("AesGcmKey.key_value not found");
    }

    /** Decode a varint at `proto[pos]`. Returns {value, newPos}. */
    private static int[] readVarint(byte[] buf, int pos) {
        int result = 0, shift = 0;
        while (true) {
            byte b = buf[pos++];
            result |= (b & 0x7f) << shift;
            if ((b & 0x80) == 0) break;
            shift += 7;
        }
        return new int[] { result, pos };
    }

    /**
     * AES-GCM decrypt where {@code blob[off..off+len)} is laid out as
     * [12-byte IV][ciphertext][16-byte GCM tag]. {@code aad} may be null.
     */
    private static byte[] aesGcmDecrypt(SecretKey key, byte[] blob, int off, int len, byte[] aad) throws Exception {
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, key, new GCMParameterSpec(128, blob, off, 12));
        if (aad != null) c.updateAAD(aad);
        return c.doFinal(blob, off + 12, len - 12);
    }
    private static byte[] aesGcmDecrypt(SecretKey key, byte[] blob, int off, int len) throws Exception {
        return aesGcmDecrypt(key, blob, off, len, null);
    }

    private static void installKeystoreProvider() {
        for (String klass : new String[] {
                "android.security.keystore2.AndroidKeyStoreProvider",
                "android.security.keystore.AndroidKeyStoreProvider"}) {
            try {
                Class.forName(klass).getMethod("install").invoke(null);
                return;
            } catch (Throwable ignored) { /* try next */ }
        }
    }

    private static byte[] hexDecode(String hex) {
        hex = hex.replaceAll("\\s+", "");
        byte[] out = new byte[hex.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(hex.substring(i*2, i*2+2), 16);
        }
        return out;
    }

    private static String shorten(String name) {
        return name.length() > 24 ? name.substring(0, 24) + "..." : name;
    }

    private static String bytesToPrintable(byte[] b) {
        StringBuilder sb = new StringBuilder();
        for (byte by : b) {
            int v = by & 0xff;
            if (v >= 0x20 && v < 0x7f) sb.append((char) v);
            else sb.append(String.format("\\x%02x", v));
        }
        return sb.toString();
    }
}
