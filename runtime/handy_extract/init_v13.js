// Provide a _fcap(string) global that writes to the app's cache log.
// Doesn't touch send() — calling code uses _fcap explicitly when it
// wants events captured.
(function() {
  const findSym = (n) => {
    for (const m of Process.enumerateModules()) {
      try { const a = m.findExportByName(n); if (a) return a; } catch (e) {}
    }
    return null;
  };
  const fopenA = findSym('fopen'), fwriteA = findSym('fwrite'), fflushA = findSym('fflush');
  if (!fopenA || !fwriteA) return;
  const fopenF = new NativeFunction(fopenA, 'pointer', ['pointer','pointer'], {exceptions:'propagate'});
  const fwriteF = new NativeFunction(fwriteA, 'size_t', ['pointer','size_t','size_t','pointer'], {exceptions:'propagate'});
  const fflushF = fflushA ? new NativeFunction(fflushA, 'int', ['pointer'], {exceptions:'propagate'}) : null;

  const path = Memory.allocUtf8String('/data/data/bbl.intl.bambulab.com/cache/handy_capture.log');
  const mode = Memory.allocUtf8String('a');
  const fp = fopenF(path, mode);
  if (fp.isNull()) return;

  globalThis._fcap = function(s) {
    try {
      const t = String(s);
      const buf = Memory.allocUtf8String(t + '\n');
      fwriteF(buf, 1, t.length + 1, fp);
      if (fflushF) fflushF(fp);
    } catch (e) {}
  };
  _fcap('=== gadget started: ' + new Date().toISOString() + ' ===');
})();
// quick_hook.js — minimal hook to load INSTANTLY when gadget comes up.
// Only the bare minimum to defeat the shield's Thread-2 check that
// fires ~500ms after process spawn. Goal: hook /proc/self/maps reads
// to filter out our injected libs.

'use strict';
const A = (m) => send({type:'log', msg:'[q] ' + m});

// 1. Find symbols
function findSym(name) {
  for (const m of Process.enumerateModules()) {
    try { const a = m.findExportByName(name); if (a) return a; } catch (e) {}
  }
  return null;
}

// 2. Hook openat to track which fds are reading /proc/self/maps
const procMapsFds = new Set();
const openat = findSym('openat');
if (openat) {
  Interceptor.attach(openat, {
    onEnter(args) {
      try {
        const path = args[1].readCString() || '';
        this.isMaps = path.includes('/proc/') && path.includes('/maps');
      } catch (e) { this.isMaps = false; }
    },
    onLeave(retval) {
      if (this.isMaps && retval.toInt32() >= 0) {
        procMapsFds.add(retval.toInt32());
      }
    }
  });
  A('hooked openat');
}

// 3. Hook read to filter out gadget/sysrt/frida/zygisk lines from /proc/self/maps
const readFn = findSym('read');
if (readFn) {
  Interceptor.attach(readFn, {
    onEnter(args) {
      this.fd = args[0].toInt32();
      this.buf = args[1];
      this.shouldFilter = procMapsFds.has(this.fd);
    },
    onLeave(retval) {
      if (!this.shouldFilter) return;
      const n = retval.toInt32();
      if (n <= 0) return;
      try {
        const text = this.buf.readUtf8String(n);
        if (!text) return;
        // Filter out lines that mention our injected libs
        const filtered = text.split('\n').filter(line => {
          const lower = line.toLowerCase();
          return !(lower.includes('gadget') ||
                   lower.includes('sysrt') ||
                   lower.includes('frida') ||
                   lower.includes('zygisk') ||
                   lower.includes('re.zyg.fri') ||
                   lower.includes('magisk'));
        }).join('\n');
        if (filtered.length !== text.length) {
          // Pad with nulls or shrink the result
          this.buf.writeUtf8String(filtered);
          // Update return value to new length (may break the caller's
          // assumption but most readers handle this fine)
          retval.replace(filtered.length);
          A(`filtered /proc/.../maps read: ${text.length}B → ${filtered.length}B`);
        }
      } catch (e) {}
    }
  });
  A('hooked read');
}

// 4. Also hook fopen for older code paths
const fopen = findSym('fopen');
if (fopen) {
  Interceptor.attach(fopen, {
    onEnter(args) {
      try {
        const path = args[0].readCString() || '';
        if (path.includes('/proc/') && path.includes('/maps')) {
          A(`fopen on maps: ${path} (passed through)`);
        }
      } catch (e) {}
    }
  });
  A('hooked fopen');
}

A(`quick_hook armed (mode: filter /proc/self/maps reads)`);
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

const PRINT = (m) => {
  try { send({ type: 'log', msg: '' + m }); } catch (e) {}
  try { if (typeof _fcap === 'function') _fcap('LOG ' + m); } catch (e) {}
};
const EMIT  = (kind, payload) => {
  // Dual-emit: send() to gadget runtime + _fcap() to capture file when
  // running in script-mode where the runtime has no consumer.
  try { send({ type: kind, ...payload }); } catch (e) {}
  try {
    if (typeof _fcap === 'function') _fcap(kind + ' ' + JSON.stringify(payload).substring(0, 8000));
  } catch (e) {}
};
const PRINT2 = (m) => { try { send({type:'log', msg:''+m}); } catch(e){} try { if (typeof _fcap==='function') _fcap('LOG '+m); } catch(e){} };

// =====================================================================
// Anti-anti-debug — runs IMMEDIATELY at script load, before our 250ms
// setTimeout-delayed crypto hooks. Defeats the shield's three layers:
//   - self-ptrace (return success without ptracing)
//   - seccomp filter install (return success without applying)
//   - PR_SET_DUMPABLE / PR_SET_PTRACER (return success without applying)
//   - /proc/self/status reads (rewrite TracerPid → 0)
//
// Spawn-mode entry pauses the process before .init_array constructors
// run, so installing these hooks here means the shield's anti-debug
// constructors run AGAINST stubs and never actually apply.
// =====================================================================
function installAntiAntiDebug() {
  const A = (m) => send({type:'log', msg:'[anti] ' + m});

  // findSymNF: locate a libc symbol and wrap as NativeFunction. Useful
  // for the magic-page mmap below.
  function findSymNF(name, retType, argTypes) {
    let p = null;
    try { p = Module.findExportByName(null, name); } catch (e) {}
    if (!p) {
      for (const m of Process.enumerateModules()) {
        try { const a = m.findExportByName(name); if (a) { p = a; break; } } catch (e) {}
      }
    }
    if (!p) return null;
    try { return new NativeFunction(p, retType, argTypes, {exceptions:'propagate'}); }
    catch (e) { return null; }
  }

  // Re-enumerate modules so libc.so etc. are visible. At very-early
  // spawn time the loader may not have populated Frida's module list
  // even though libc is mapped — call this defensively.
  try { Module.load('/system/lib64/libc.so'); } catch (e) {}
  // List visible candidates so we can debug if libc still isn't here
  const cands = Process.enumerateModules().filter(m => /libc\.so|libssl|libcrypto/.test(m.name));
  A(`module enum: ${cands.length} candidates - ${cands.map(m=>m.name).join(',')}`);

  function hook(name, mod, sym, mkHandler) {
    let p = null;
    try { p = Module.findExportByName(mod, sym); } catch (e) {}
    if (!p) {
      // Try each loaded module by name
      for (const m of Process.enumerateModules()) {
        try {
          const a = m.findExportByName(sym);
          if (a) { p = a; break; }
        } catch (e) {}
      }
    }
    if (!p) { A(`hook ${sym}: export not found`); return; }
    try {
      Interceptor.attach(p, mkHandler());
      A(`hooked ${sym} @ ${p}`);
    } catch (e) { A(`hook ${sym} failed: ${e}`); }
  }

  // ptrace — return 0 (success) for every call, so self-ptrace fails
  // silently and TracerPid stays 0.
  hook('ptrace', null, 'ptrace', () => ({
    onEnter(args) { this.req = args[0].toInt32(); this.pid = args[1].toInt32(); },
    onLeave(retval) {
      retval.replace(0);
      A(`ptrace(req=${this.req},pid=${this.pid}) faked 0`);
    }
  }));

  // prctl — block PR_SET_SECCOMP / PR_SET_DUMPABLE(0) / PR_SET_PTRACER
  hook('prctl', null, 'prctl', () => ({
    onEnter(args) { this.opt = args[0].toInt32(); this.arg2 = args[1]; },
    onLeave(retval) {
      if (this.opt === 22) { retval.replace(0); A('prctl(PR_SET_SECCOMP) blocked'); }
      else if (this.opt === 4 && this.arg2.toInt32() === 0) {
        retval.replace(0); A('prctl(PR_SET_DUMPABLE,0) blocked');
      }
      else if (this.opt === 0x59616D61) { retval.replace(0); A('prctl(PR_SET_PTRACER) blocked'); }
    }
  }));

  // raw syscall() — block SYS_seccomp(277) / SYS_ptrace(117) on arm64
  hook('syscall', null, 'syscall', () => ({
    onEnter(args) { this.nr = args[0].toInt32(); },
    onLeave(retval) {
      if (this.nr === 277 || this.nr === 117) {
        retval.replace(0); A(`syscall(${this.nr}) blocked`);
      }
    }
  }));

  // /proc/self/status read patch — defensive belt-and-suspenders so
  // any reader of TracerPid sees 0.
  const procStatusFds = new Set();
  function watchOpen(symName) {
    hook(symName, null, symName, () => ({
      onEnter(args) {
        const pathArg = (symName === 'openat') ? args[1] : args[0];
        try {
          const path = pathArg.readCString() || '';
          this.statusFd = path.includes('status');
        } catch (e) { this.statusFd = false; }
      },
      onLeave(retval) {
        if (this.statusFd && retval.toInt32() >= 0)
          procStatusFds.add(retval.toInt32());
      }
    }));
  }
  watchOpen('open');
  watchOpen('openat');
  hook('read', null, 'read', () => ({
    onEnter(args) {
      this.fd = args[0].toInt32(); this.buf = args[1];
      this.patch = procStatusFds.has(this.fd);
    },
    onLeave(retval) {
      if (!this.patch) return;
      const n = retval.toInt32();
      if (n <= 0) return;
      try {
        const text = this.buf.readUtf8String(n);
        if (text && /TracerPid:\s*[1-9]/.test(text)) {
          const patched = text.replace(/TracerPid:\s*\d+/g, 'TracerPid:\t0');
          this.buf.writeUtf8String(patched);
          A('TracerPid in /proc/self/status patched to 0');
        }
      } catch (e) {}
    }
  }));

  // Refuse-to-die: replace every plausible self-kill with a no-op stub.
  // Use ONLY Interceptor.replace (not attach) — they conflict and the
  // attach version doesn't actually skip the original call. raise/kill/
  // tgkill take args but we replace with a stub that returns 0 (success)
  // without actually doing anything. abort/_exit/exit/pthread_exit are
  // [[noreturn]] but our stub just returns, which the caller's prolog
  // didn't expect — they assume the call doesn't return — but in
  // practice the caller's stack is intact and it just continues past
  // the call site.
  function replaceNoop(sym, retType, argTypes) {
    let p = null;
    try { p = Module.findExportByName(null, sym); } catch (e) {}
    if (!p) {
      // Same per-module fallback as hook(): early in process, the global
      // export table may not be populated yet, so iterate modules manually.
      for (const m of Process.enumerateModules()) {
        try { const a = m.findExportByName(sym); if (a) { p = a; break; } } catch (e) {}
      }
    }
    if (!p) { A(`no-op replace ${sym}: not found`); return; }
    try {
      const stub = new NativeCallback(function() {
        send({type:'log', msg:`[anti] !! ${sym}() suppressed`});
        // Return 0 (or void). For abort etc., callers don't read retval.
      }, retType, argTypes);
      Interceptor.replace(p, stub);
      A(`replaced ${sym} with no-op stub @ ${p}`);
    } catch (e) { A(`replace ${sym} failed: ${e}`); }
  }
  // ONLY replace abort/_exit/exit — these are clearly death-paths and
  // not used legitimately during normal app lifecycle.
  // pthread_exit / raise / kill / tgkill are USED by ART internally
  // (thread cleanup, GC signaling, etc.) — replacing them with no-op
  // stubs breaks ART with `Check failed: self->tlsPtr_.jpeer != nullptr`
  // and `existing_entry_point != nullptr` aborts. Don't touch them.
  replaceNoop('abort', 'void', []);
  replaceNoop('_exit', 'void', ['int']);
  replaceNoop('exit',  'void', ['int']);

  // (Magic-page mmap removed — crashes gum-js-loop. The shield's
  // 0xdead-magic crashes are countered at the Shamiko level instead:
  // when Shamiko hides Magisk from the app, the shield's tamper-detect
  // doesn't fire in the first place, so no 0xdead* deref happens.)

  // Catch signal-based aborts as a backup (skip offending instruction,
  // advance PC by 4 — ARM64 fixed-width).
  try {
    Process.setExceptionHandler(function (details) {
      const t = details.type;
      const addr = details.address;
      A(`!! exception ${t} @ ${addr} — skipping instruction`);
      // ARM64 instructions are 4 bytes wide. Advance PC.
      if (details.context && details.context.pc) {
        details.context.pc = details.context.pc.add(4);
      }
      return true; // we handled it
    });
    A('installed exception handler (signal interceptor)');
  } catch (e) { A(`exception handler install failed: ${e}`); }

  A('anti-anti-debug installed');
}

// Delay so the loader has time to map libc/etc. into the process. At
// pure spawn-mode entry the dynamic linker has only just started; by
// the time setTimeout fires libc is fully linked and findExportByName
// can resolve symbols.
setTimeout(installAntiAntiDebug, 50);

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
function hexstr(p, n) {
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
    bytes_hex: hexstr(buf, len)
  });
}

// ---------- crypto hooks ----------------------------------------------------

function hookOpenSSL() {
  const candidateNames = ['libcrypto.so', 'libssl.so', 'libflutter.so', 'libapp.so'];
  // Multiple libcrypto.so instances are loaded into Bambu Handy (system,
  // bundled, possibly Conscrypt's). Enumerate all modules whose name matches
  // any candidate, not just the first via findExportByName.
  const allModules = Process.enumerateModules();
  const candidateMods = allModules.filter(m =>
    candidateNames.some(n => m.name === n || m.name.startsWith(n + '.')));
  let hooks = 0;

  function tryHook(mod, sym, mkHandler) {
    let addr = null;
    try { addr = mod.findExportByName(sym); } catch (e) {}
    if (!addr) return false;
    try {
      Interceptor.attach(addr, mkHandler(mod.name, sym));
      PRINT(`hooked ${mod.name}@${mod.base}!${sym} @ ${addr}`);
      hooks++;
      return true;
    } catch (e) {
      PRINT(`failed to hook ${mod.name}!${sym}: ${e}`);
      return false;
    }
  }

  candidateMods.forEach(mod => {
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
  // Java runtime is only available when Frida runs the V8 runtime AND the
  // app has loaded ART. QuickJS doesn't expose Java at all. Treat as
  // best-effort.
  if (typeof Java === 'undefined' || !Java.available) {
    PRINT('  Java bridge unavailable (likely QuickJS runtime); skipping HTTPS hooks');
    return;
  }
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
