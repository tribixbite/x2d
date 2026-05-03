// Sign-helper for X2D LAN-direct printing.
// Runs under sharedUserId="bbl.shared" — same UID as the re-patched
// Bambu Handy. AndroidKeyStore is per-UID, so we see + can use Bambu's
// hardware-backed RSA key WITHOUT extracting it (impossible per TEE
// boundary). Sign payloads on demand.
package com.x2d.sign;
import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.util.Base64;
import android.util.Log;
import java.io.File;
import java.io.FileOutputStream;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.cert.X509Certificate;
import java.util.Enumeration;

public class SignActivity extends Activity {
    private static final String TAG = "x2d-sign";

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        Intent i = getIntent();
        String act = (i.getAction() == null) ? "" : i.getAction();
        StringBuilder out = new StringBuilder();
        out.append("uid=").append(android.os.Process.myUid()).append('\n');
        out.append("pkg=").append(getPackageName()).append('\n');
        out.append("action=").append(act).append('\n');
        try {
            if (act.equals("com.x2d.sign.LIST_ALIASES")) listAliases(out);
            else if (act.equals("com.x2d.sign.SIGN_PAYLOAD")) sign(out, i.getStringExtra("alias"), i.getStringExtra("payload"));
            else out.append("ERR: unknown action; supported: LIST_ALIASES, SIGN_PAYLOAD\n");
        } catch (Throwable t) {
            out.append("EXCEPTION: ").append(t).append('\n');
            for (StackTraceElement e : t.getStackTrace()) out.append("  at ").append(e).append('\n');
        }
        // write to internal storage (no perms needed) — user pulls via run-as
        File outFile = new File(getFilesDir(), "x2d_sign_out.txt");
        try (FileOutputStream f = new FileOutputStream(outFile)) {
            f.write(out.toString().getBytes("UTF-8"));
        } catch (Throwable t) { Log.e(TAG, "write " + outFile, t); }
        Log.i(TAG, out.toString());
        finish();
    }

    private void listAliases(StringBuilder out) throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        int n = 0;
        for (Enumeration<String> al = ks.aliases(); al.hasMoreElements();) {
            String a = al.nextElement(); n++;
            try {
                java.security.Key k = ks.getKey(a, null);
                String algo = (k != null) ? k.getAlgorithm() : "?";
                java.security.cert.Certificate cert = ks.getCertificate(a);
                out.append("  alias[").append(n).append("] = ").append(a).append("  key.algo=").append(algo).append('\n');
                if (cert instanceof X509Certificate) {
                    X509Certificate x = (X509Certificate) cert;
                    out.append("    subject=").append(x.getSubjectDN()).append('\n');
                    out.append("    issuer =").append(x.getIssuerDN()).append('\n');
                    out.append("    serial =").append(x.getSerialNumber()).append('\n');
                    out.append("    notAfter=").append(x.getNotAfter()).append('\n');
                    out.append("-----BEGIN CERTIFICATE-----\n")
                       .append(Base64.encodeToString(x.getEncoded(), Base64.NO_WRAP))
                       .append("\n-----END CERTIFICATE-----\n");
                }
            } catch (Throwable t) {
                out.append("    <error: ").append(t).append(">\n");
            }
        }
        out.append("total aliases: ").append(n).append('\n');
    }

    private void sign(StringBuilder out, String alias, String payloadB64) throws Exception {
        if (alias == null || payloadB64 == null) {
            out.append("ERR: SIGN_PAYLOAD requires alias= and payload= extras\n");
            return;
        }
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        PrivateKey pk = (PrivateKey) ks.getKey(alias, null);
        if (pk == null) { out.append("ERR: no key for alias=").append(alias).append('\n'); return; }
        byte[] payload = Base64.decode(payloadB64, Base64.DEFAULT);
        Signature s = Signature.getInstance("SHA256withRSA");
        s.initSign(pk); s.update(payload);
        byte[] sig = s.sign();
        out.append("SIGNATURE: ").append(Base64.encodeToString(sig, Base64.NO_WRAP)).append('\n');
        out.append("payload_len=").append(payload.length).append(" sig_len=").append(sig.length).append('\n');
    }
}
