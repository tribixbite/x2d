// handy_hook.js — Frida script for Bambu Handy v3.19.0 (bbl.intl.bambulab.com).
//
// Goal: capture the per-installation X.509 cert + RSA private key the app uses
// to sign LAN MQTT publishes against an X-series Bambu printer.
//
// Strategy: hook every plausible signing primitive AND every plausible AES
// decrypt that could carry a PEM-wrapped cert/key. On hit, walk the in-memory
// RSA / AES context and emit raw bytes via send() so the host runner can
// reassemble PKCS#8 PEMs.
//
// Coverage matrix (one of these almost certainly fires on a print/control op):
//
//   OpenSSL/BoringSSL   (libcrypto.so / linked into libflutter.so)
//     EVP_PKEY_sign           — high-level sign API (post-1.1)
//     EVP_PKEY_get1_RSA       — pulls RSA* out of EVP_PKEY*
//     EVP_DigestSignFinal     — used by JWT-style signing libs
//     RSA_sign                — direct RSA* sign API
//     RSA_private_encrypt     — bare RSA priv-encrypt (PKCS1v15 padding)
//     EVP_PKEY_decrypt        — RSA decrypt (cert-fetch payload?)
//     EVP_DecryptUpdate       — symmetric AES (cert-mint endpoint payload)
//     AES_cbc_encrypt         — legacy AES API (still in BoringSSL)
//
//   mbedTLS (libmbedcrypto.so / static-linked into libbambu_*.so analogues)
//     mbedtls_pk_sign
//     mbedtls_rsa_pkcs1_sign
//     mbedtls_aes_crypt_cbc
//
// We DO NOT call back into the app from on-enter; we only memcpy the in-memory
// context bytes out. RASP layers (Promon, Tinker hot-patch) react to behaviour
// changes more than to passive reads, so this minimises detection.

'use strict';

const PRINT = (m) => send({ type: 'log', msg: '' + m });
const EMIT  = (kind, payload) => send({ type: kind, ...payload });

// ---------- helpers ---------------------------------------------------------

// Walk a BoringSSL/OpenSSL BIGNUM* into a hex string. Layout (BoringSSL,
// stable since 2018):
//   struct bignum_st { BN_ULONG *d; int top; int dmax; int neg; int flags; };
// We trust top + neg only.
function bn2hex(p) {
  if (p.isNull()) return '';
  const d_ptr = p.readPointer();
  const top   = p.add(Process.pointerSize).readS32();
  // const dmax  = p.add(Process.pointerSize + 4).readS32();
  const neg   = p.add(Process.pointerSize + 8).readS32();
  if (top <= 0 || top > 1024 || d_ptr.isNull()) return '';
  const word = (Process.pointerSize === 8) ? 8 : 4;
  // BIGNUMs are little-endian word arrays; flip to big-endian hex.
  const bytes = [];
  for (let i = top - 1; i >= 0; i--) {
    const w = (word === 8) ? d_ptr.add(i * 8).readU64() : d_ptr.add(i * 4).readU32();
    const hex = w.toString(16).padStart(word * 2, '0');
    bytes.push(hex);
  }
  let out = bytes.join('').replace(/^0+/, '');
  if (out.length % 2) out = '0' + out;
  if (neg) out = '-' + out;
  return out;
}

// Walk an mbedtls_mpi struct into a hex string.
//   struct { int s; size_t n; mbedtls_mpi_uint *p; }   on stock mbedTLS 3.x
function mpi2hex(p) {
  if (p.isNull()) return '';
  const s     = p.readS32();
  const n     = p.add(Process.pointerSize).readU32();   // size_t — assume 4 high bytes are zero
  const limbs = p.add(Process.pointerSize * 2).readPointer();
  if (n <= 0 || n > 1024 || limbs.isNull()) return '';
  const word = Process.pointerSize;
  const arr = [];
  for (let i = n - 1; i >= 0; i--) {
    const w = (word === 8) ? limbs.add(i * 8).readU64() : limbs.add(i * 4).readU32();
    arr.push(w.toString(16).padStart(word * 2, '0'));
  }
  let out = arr.join('').replace(/^0+/, '');
  if (out.length % 2) out = '0' + out;
  if (s < 0) out = '-' + out;
  return out;
}

// Hexdump a small region for log messages.
function hexdump(p, n) {
  if (p.isNull()) return '<null>';
  const buf = p.readByteArray(Math.min(n, 256));
  return Array.from(new Uint8Array(buf))
    .map(b => b.toString(16).padStart(2, '0')).join('');
}

// Heuristic: does a byte buffer look like a PKCS#1/PKCS#8 RSA private key
// or an X.509 certificate (DER or PEM)?
function classifyBlob(buf, len) {
  if (len < 200) return null;
  const head = new Uint8Array(buf.readByteArray(Math.min(64, len)));
  // PEM headers
  const text = String.fromCharCode.apply(null, head);
  if (text.includes('BEGIN PRIVATE KEY'))     return 'pkcs8_pem';
  if (text.includes('BEGIN RSA PRIVATE KEY')) return 'pkcs1_pem';
  if (text.includes('BEGIN CERTIFICATE'))     return 'cert_pem';
  // DER: 30 82 LL LL 02 01 00 ... (PKCS#8 RSA-2048 ≈ 0x4be, RSA-4096 ≈ 0x93d)
  if (head[0] === 0x30 && head[1] === 0x82 && head[4] === 0x02 && head[5] === 0x01) {
    return 'pkcs8_der';   // best guess
  }
  // X.509 cert DER: 30 82 LL LL 30 82 ...
  if (head[0] === 0x30 && head[1] === 0x82 && head[4] === 0x30 && head[5] === 0x82) {
    return 'cert_der';
  }
  return null;
}

function sniffBuffer(label, buf, len) {
  if (!buf || buf.isNull() || len <= 0) return;
  const kind = classifyBlob(buf, len);
  if (!kind) return;
  PRINT(`[sniff] ${label} → ${kind}, ${len} bytes`);
  EMIT('blob', {
    label, kind, len,
    bytes_hex: hexdump(buf, len)
  });
}

// ---------- crypto hooks ----------------------------------------------------

function hookOpenSSL() {
  const candidates = ['libcrypto.so', 'libssl.so', 'libflutter.so', 'libapp.so'];
  let hooks = 0;

  function tryHook(modName, sym, mkHandler) {
    let addr = null;
    try { addr = Module.findExportByName(modName, sym); } catch (e) {}
    if (!addr) return false;
    try {
      Interceptor.attach(addr, mkHandler(modName, sym));
      PRINT(`hooked ${modName}!${sym} @ ${addr}`);
      hooks++;
      return true;
    } catch (e) {
      PRINT(`failed to hook ${modName}!${sym}: ${e}`);
      return false;
    }
  }

  candidates.forEach(mod => {
    // EVP_PKEY_sign(ctx, sig, *siglen, tbs, tbslen)
    tryHook(mod, 'EVP_PKEY_sign', (m, s) => ({
      onEnter(args) {
        this.tbs = args[3]; this.tbslen = args[4].toUInt32();
        EMIT('sign_call', { fn: s, mod: m, tbslen: this.tbslen,
                             tbs_hex: this.tbs.readByteArray(Math.min(this.tbslen, 512)) });
      }
    }));

    // EVP_PKEY_get1_RSA(EVP_PKEY*) → RSA*  (we then walk RSA)
    tryHook(mod, 'EVP_PKEY_get1_RSA', (m, s) => ({
      onLeave(retval) {
        if (!retval.isNull()) {
          // BoringSSL RSA layout:
          //   RSA { CRYPTO_refcount_t refs; ENGINE *engine; CRYPTO_EX_DATA;
          //         BIGNUM *n,*e,*d,*p,*q,*dmp1,*dmq1,*iqmp; ... }
          // Skip refs+engine+ex_data; first BIGNUM* is at offset varies — try
          // 24, 32, 40 and pick the one that looks like a valid pointer chain.
          const offs = [16, 24, 32, 40, 48, 56];
          for (const off of offs) {
            try {
              const n_pp = retval.add(off).readPointer();
              if (n_pp.isNull()) continue;
              const n = bn2hex(n_pp);
              if (!n || n.length < 32 || n.length > 4096) continue;
              const e = bn2hex(retval.add(off + Process.pointerSize).readPointer());
              const d = bn2hex(retval.add(off + Process.pointerSize * 2).readPointer());
              const p = bn2hex(retval.add(off + Process.pointerSize * 3).readPointer());
              const q = bn2hex(retval.add(off + Process.pointerSize * 4).readPointer());
              if (n && d && p && q && d.length > 16) {
                EMIT('rsa_key', {
                  fn: s, mod: m, off,
                  n, e, d, p, q,
                  dmp1: bn2hex(retval.add(off + Process.pointerSize * 5).readPointer()),
                  dmq1: bn2hex(retval.add(off + Process.pointerSize * 6).readPointer()),
                  iqmp: bn2hex(retval.add(off + Process.pointerSize * 7).readPointer())
                });
                PRINT(`[!] RSA key extracted via ${m}!${s} (off=${off})`);
                return;
              }
            } catch (e) { /* keep trying offsets */ }
          }
        }
      }
    }));

    // RSA_sign(type, m, m_len, sigret, *siglen, RSA*)
    tryHook(mod, 'RSA_sign', (modName, s) => ({
      onEnter(args) {
        const rsa = args[5];
        EMIT('sign_call', { fn: s, mod: modName, type: args[0].toInt32(), tbslen: args[2].toUInt32() });
        // RSA_sign exposes RSA* directly; walk it via the offset probe.
        const offs = [16, 24, 32, 40, 48];
        for (const off of offs) {
          try {
            const n = bn2hex(rsa.add(off).readPointer());
            if (n.length >= 32 && n.length <= 4096) {
              EMIT('rsa_key', {
                fn: s, mod: modName, off,
                n,
                e: bn2hex(rsa.add(off + Process.pointerSize).readPointer()),
                d: bn2hex(rsa.add(off + Process.pointerSize * 2).readPointer()),
                p: bn2hex(rsa.add(off + Process.pointerSize * 3).readPointer()),
                q: bn2hex(rsa.add(off + Process.pointerSize * 4).readPointer()),
                dmp1: bn2hex(rsa.add(off + Process.pointerSize * 5).readPointer()),
                dmq1: bn2hex(rsa.add(off + Process.pointerSize * 6).readPointer()),
                iqmp: bn2hex(rsa.add(off + Process.pointerSize * 7).readPointer())
              });
              PRINT(`[!] RSA key via ${modName}!${s}`);
              return;
            }
          } catch (e) {}
        }
      }
    }));

    // EVP_DecryptUpdate(ctx, out, *outlen, in, inlen)  AND  EVP_DecryptFinal_ex
    // — sniff the cleartext post-decrypt for PEM/DER markers.
    tryHook(mod, 'EVP_DecryptUpdate', (modName, s) => ({
      onEnter(args) { this.outp = args[1]; this.outlenp = args[2]; },
      onLeave(retval) {
        if (retval.toInt32() !== 1) return;
        const olen = this.outlenp.readU32();
        if (olen >= 200) sniffBuffer(`${modName}!${s}`, this.outp, olen);
      }
    }));
    tryHook(mod, 'EVP_DecryptFinal_ex', (modName, s) => ({
      onEnter(args) { this.outp = args[1]; this.outlenp = args[2]; },
      onLeave(retval) {
        if (retval.toInt32() !== 1) return;
        const olen = this.outlenp.readU32();
        if (olen >= 200) sniffBuffer(`${modName}!${s}`, this.outp, olen);
      }
    }));
  });

  return hooks;
}

function hookMbedTLS() {
  const candidates = ['libmbedcrypto.so', 'libmbedtls.so', 'libflutter.so', 'libapp.so'];
  let hooks = 0;

  candidates.forEach(mod => {
    // mbedtls_pk_sign(ctx, md_alg, hash, hashlen, sig, sigsz, *siglen, rng, rng_ctx)
    let addr = null;
    try { addr = Module.findExportByName(mod, 'mbedtls_pk_sign'); } catch (e) {}
    if (addr) {
      try {
        Interceptor.attach(addr, {
          onEnter(args) {
            const ctx = args[0];
            // mbedtls_pk_context: { const mbedtls_pk_info_t *info; void *pk_ctx; }
            // pk_ctx for RSA is mbedtls_rsa_context*: { ver, len, N, E, D, P, Q, DP, DQ, QP, ... }
            const pk_ctx = ctx.add(Process.pointerSize).readPointer();
            if (pk_ctx.isNull()) return;
            // first 8 bytes are int ver, int len; then N at 16
            const N_off = 16;
            const N  = mpi2hex(pk_ctx.add(N_off));
            if (!N || N.length < 32 || N.length > 4096) return;
            const STEP = Process.pointerSize * 2 + 4;  // mpi struct size approx
            // safer: walk known field offsets for mbedTLS 3.x:
            //   ver:0 len:4 N:16 E:40 D:64 P:88 Q:112 DP:136 DQ:160 QP:184  (assumes 24-byte mpi)
            const FIELDS = [
              ['N', 16], ['E', 40], ['D', 64], ['P', 88], ['Q', 112],
              ['DP', 136], ['DQ', 160], ['QP', 184]
            ];
            const out = { fn: 'mbedtls_pk_sign', mod };
            for (const [name, off] of FIELDS) {
              const v = mpi2hex(pk_ctx.add(off));
              if (v) out[name.toLowerCase()] = v;
            }
            if (out.n && out.d) {
              EMIT('rsa_key', out);
              PRINT(`[!] RSA key via ${mod}!mbedtls_pk_sign`);
            }
          }
        });
        PRINT(`hooked ${mod}!mbedtls_pk_sign @ ${addr}`);
        hooks++;
      } catch (e) {
        PRINT(`mbedtls_pk_sign hook failed: ${e}`);
      }
    }

    // mbedtls_aes_crypt_cbc(ctx, mode, length, iv, in, out)  — sniff cleartext
    try {
      const aes_addr = Module.findExportByName(mod, 'mbedtls_aes_crypt_cbc');
      if (aes_addr) {
        Interceptor.attach(aes_addr, {
          onEnter(args) {
            this.mode = args[1].toInt32();
            this.len  = args[2].toUInt32();
            this.out  = args[5];
          },
          onLeave(retval) {
            if (this.mode !== 0 /* DECRYPT */ || retval.toInt32() !== 0) return;
            sniffBuffer(`${mod}!mbedtls_aes_crypt_cbc`, this.out, this.len);
          }
        });
        PRINT(`hooked ${mod}!mbedtls_aes_crypt_cbc`);
        hooks++;
      }
    } catch (e) {}
  });

  return hooks;
}

function hookHTTPS() {
  // Java side: log every Bambu cloud HTTPS request via okhttp3 Interceptor.
  Java.perform(() => {
    try {
      const Url = Java.use('java.net.URL');
      Url.openConnection.overload().implementation = function () {
        const u = this.toString();
        if (u && u.includes('bambulab')) PRINT(`[http] URL.openConnection: ${u}`);
        return this.openConnection.apply(this, arguments);
      };
    } catch (e) { /* not all java targets are loaded yet */ }

    // dio (Dart HTTP) routes through native sockets so okhttp3 hooks may miss
    // Bambu Handy. We capture the URL inside Conscrypt's TLS handshake
    // SNI string instead.
    try {
      const ConscryptEngine = Java.use('com.android.org.conscrypt.ConscryptEngine');
      ConscryptEngine.setHostname.implementation = function (h) {
        if (h && h.includes('bambulab')) PRINT(`[tls] Conscrypt SNI: ${h}`);
        return this.setHostname(h);
      };
    } catch (e) {}
  });
}

// ---------- entry -----------------------------------------------------------

setTimeout(() => {
  try {
    PRINT(`Bambu Handy hook starting at ${new Date().toISOString()}`);
    PRINT(`pid=${Process.id}  arch=${Process.arch}  ptrSize=${Process.pointerSize}`);
    PRINT('loaded modules:');
    Process.enumerateModules().forEach(m => {
      if (/(crypto|ssl|mbed|flutter|app)/i.test(m.name) || m.name === 'libapp.so')
        PRINT(`   ${m.name.padEnd(28)} base=${m.base}  size=${m.size}`);
    });

    let total = 0;
    total += hookOpenSSL();
    total += hookMbedTLS();
    hookHTTPS();
    PRINT(`installed ${total} crypto hooks. Now interact with the app — every`);
    PRINT('signed MQTT publish or AES decrypt will be reported.');
  } catch (e) {
    PRINT('init error: ' + e.stack);
  }
}, 250);   // brief delay so the packer's .init_array finishes unpacking before we hook
