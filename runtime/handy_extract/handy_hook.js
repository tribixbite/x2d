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
  const A = (m) => {
    try { send({type:'log', msg:'[anti] ' + m}); } catch (e) {}
    try { if (typeof _fcap === 'function') _fcap('[anti] ' + m); } catch (e) {}
  };

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

  // Listeners installed by the shield-bypass hooks. Tracked so we can
  // detach them ALL after the shield's startup checks are over —
  // every Interceptor.attach on a hot libc symbol (read, syscall,
  // prctl) crosses the JS bridge per call. Once the shield's
  // ptrace daemon and seccomp install have run (within ~500ms of
  // gadget bind), keeping these armed only burns CPU on the main
  // thread and starves UI rendering, leaving the user stuck on a
  // spinner.
  const shieldListeners = [];
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
      const l = Interceptor.attach(p, mkHandler());
      shieldListeners.push(l);
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
  //
  // CRITICAL: hooking generic read() / open() / openat() globally adds a
  // JS-bridge crossing on every libc call. Bambu's main thread does so
  // many of these per UI tap that the 5-second input-dispatch budget is
  // exhausted and the activity ANRs ("Input dispatching timed out, 5005ms
  // for MotionEvent"). Same root cause as quick_hook.js's read filter.
  //
  // Mitigation: detach the read/open/openat listeners after a guard
  // window (12s). The shield's TracerPid scan happens once at startup
  // and is over by then. ptrace/prctl/syscall hooks stay armed forever
  // because they only fire on rare specific opcodes and have no measurable
  // overhead.
  const procStatusFds = new Set();
  const statusListeners = [];
  function watchOpen(symName) {
    let p = null;
    try { p = Module.findExportByName(null, symName); } catch (e) {}
    if (!p) {
      for (const m of Process.enumerateModules()) {
        try { const a = m.findExportByName(symName); if (a) { p = a; break; } } catch (e) {}
      }
    }
    if (!p) { A(`watchOpen ${symName}: not found`); return; }
    try {
      const l = Interceptor.attach(p, {
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
      });
      statusListeners.push(l);
      A(`hooked ${symName}`);
    } catch (e) { A(`hook ${symName} failed: ${e}`); }
  }
  watchOpen('open');
  watchOpen('openat');

  const readSym = (function() {
    let p = null;
    try { p = Module.findExportByName(null, 'read'); } catch (e) {}
    if (!p) {
      for (const m of Process.enumerateModules()) {
        try { const a = m.findExportByName('read'); if (a) { p = a; break; } } catch (e) {}
      }
    }
    return p;
  })();
  if (readSym) {
    try {
      const l = Interceptor.attach(readSym, {
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
      });
      statusListeners.push(l);
      A('hooked read (TracerPid filter)');
    } catch (e) { A(`hook read failed: ${e}`); }
  }

  // Auto-detach ALL shield-bypass hooks after the startup window.
  // The shield's tamper-die / seccomp / ptrace installation all
  // complete within the first ~500ms; keeping ptrace/prctl/syscall
  // hooks armed long-term costs a JS-bridge crossing per libc call
  // and starves Bambu's UI thread (user observed: app "freezes with
  // a spinner after splash" — main thread was busy serializing
  // every prctl/syscall via the JS VM). The exception handler stays
  // installed because `Process.setExceptionHandler` is process-wide
  // and not a per-call hook.
  // 5-minute detach window to keep shield-bypass active during the full
  // OAuth login flow (user opens app → "Me" tab → Chrome custom tab →
  // credentials → deep-link callback → cert mint). Earlier 12 s window
  // stranded Bambu after the deep-link return because the shield's
  // tamper-die saw our gadget once the read filter detached, infinite-
  // looped through PC+=4 across unmapped pages, and starved the Dart
  // UI thread. 5 min covers a comfortable login walkthrough; raise to
  // `Number.MAX_SAFE_INTEGER` for indefinite filtering if the user
  // takes longer.
  const SHIELD_DETACH_MS = 300000;
  setTimeout(() => {
    let n = 0;
    for (const l of statusListeners) { try { l.detach(); n++; } catch (e) {} }
    statusListeners.length = 0;
    procStatusFds.clear();
    for (const l of shieldListeners) { try { l.detach(); n++; } catch (e) {} }
    shieldListeners.length = 0;
    if (typeof _fcap === 'function')
      _fcap('[anti] all shield-bypass hooks detached (' + n + ' listeners) after '
            + (SHIELD_DETACH_MS / 1000) + 's');
  }, SHIELD_DETACH_MS);

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

  // Catch signal-based aborts as a backup. The shield's tamper-die
  // primitive does an indirect call into a magic-PC unmapped page —
  // typically a `BLR Xn` where Xn ∈ {0xdead3000, 0xdead5019}. The CPU
  // faults at the first byte of the magic page; advancing PC by 4
  // each time would just walk further into the unmapped page and
  // generate another SIGSEGV → infinite skip loop, megabytes of log
  // spam, and no actual recovery.
  //
  // Correct fix: when the fault address is in 0xdead* magic range,
  // simulate "return from the function that was never there" by
  // restoring PC from LR (x30 on ARM64). This is exactly what `RET`
  // would do at the end of a real function. The caller's stack frame
  // is intact so execution resumes at the instruction-after-BLR.
  //
  // For non-magic faults we do still advance PC by 4 — those are
  // single-instruction tamper traps embedded inline in code (e.g.
  // `BRK` or junk bytes the shield jumped to). Skipping them keeps
  // us on the original execution path.
  // Return-to-LR exception handler. The shield's tamper-die does
  // `BLR Xn` / `BR Xn` where Xn ∈ {0xdead*, 0, junk-stack-region}; the
  // CPU faults on the unmapped target. Original strategy of advancing
  // PC by 4 walked the unmapped page byte-by-byte (256 MB capture log
  // observed in one run). Return-to-LR works for the BLR variant where
  // lr was just-set to "instruction after BLR" — a valid address.
  //
  // CRITICAL CORRECTNESS BUG FIXED: `ctx.lr` is a NativePointer object,
  // so `if (lr)` is ALWAYS truthy — even when the pointer value is 0
  // (caller used BR not BLR, lr never set). The previous code happily
  // assigned `ctx.pc = NativePointer(0)`, causing an immediate SIGSEGV
  // at PC=0, which re-entered THIS HANDLER, which set PC=0 again, ...
  // infinite recursion inside the gadget's signal-handling thread,
  // holding gum-js-loop. The Dart UI thread's next call into hooked
  // crypto then futex-waited on that lock indefinitely, freezing
  // Bambu Handy on the home spinner.
  //
  // New strategy:
  //   1. Validate lr — must be non-null AND >= 0x10000 (any address
  //      below the first user-space page is invalid).
  //   2. If lr is invalid, fall back to PC+=4 (will likely also fault,
  //      but won't recurse on the same address).
  //   3. Hard cap at HARD_LIMIT total exceptions; once exceeded,
  //      uninstall the handler and let the next signal propagate to
  //      the OS for a clean crash + tombstone (better than a hang).
  // Locate pthread_exit at install time so the exception handler can
  // redirect Thread-2's PC to it. This is the killer feature: the
  // Promon shield's tamper-die clears all registers + lr=0 then BR x0
  // (where x0=0xdead5019), so there's NO valid return to recover to.
  // Memory-dump analysis (path #4, runtime/handy_extract/dump_unpacker.sh)
  // confirmed: 141 BR x0 sites in the unpacked shield region; the
  // 0xdead5019 magic is constructed at runtime via XOR-swap arithmetic
  // so static patching is infeasible.
  //
  // Fix: redirect the dying thread's PC to pthread_exit so ONLY that
  // thread dies, not the whole process. pthread_exit(NULL) terminates
  // the calling thread via direct syscall; safe to call even with
  // corrupted GP registers because we set x0=NULL explicitly.
  let _pthread_exit_addr = null;
  for (const m of Process.enumerateModules()) {
    try {
      const a = m.findExportByName('pthread_exit');
      if (a) { _pthread_exit_addr = a; break; }
    } catch (e) {}
  }
  A('pthread_exit @ ' + _pthread_exit_addr);

  let _excCount = 0;
  const _excLogMax = 16;
  const _excHardLimit = 256;
  let _excListener = null;
  try {
    _excListener = Process.setExceptionHandler(function (details) {
      _excCount++;
      const t = details.type;
      const addr = details.address;
      const ctx = details.context;
      const addrStr = addr ? addr.toString() : '';

      // Hard limit hit — uninstall ourselves, let signal propagate.
      // The process will SIGSEGV and produce a clean tombstone.
      if (_excCount > _excHardLimit) {
        try {
          if (typeof _fcap === 'function')
            _fcap(`[anti] !! exception #${_excCount} — HARD LIMIT, uninstalling exception handler`);
        } catch (e) {}
        // There is no Frida API to uninstall the exception handler in
        // place; returning false lets the signal propagate for THIS
        // event, but on the next fault the handler runs again. The
        // hard-limit log line + return false combination is a clean
        // diagnostic that we've hit a recursion case.
        return false;
      }

      if (!ctx) return false;

      // Frida ARM64 CpuContext exposes the link register as `lr`.
      // Some older builds also accept `x30` as an alias; try both.
      // CRITICAL: NativePointer(0) is truthy (it's an object), so we
      // must use isNull() to detect a 0 value.
      const lr = ctx.lr || ctx.x30;
      const lrValid = lr && !lr.isNull() && (parseInt(lr.toString(), 16) >= 0x10000);

      if (lrValid) {
        if (_excCount <= _excLogMax) {
          A(`!! exception #${_excCount} ${t} @ ${addrStr} — RET via lr=${lr}`);
          if (_excCount === _excLogMax)
            A(`(further exceptions silenced to avoid log amplification)`);
        }
        ctx.pc = lr;
        return true;
      }
      // LR is null/zero/clearly invalid. The shield's tamper-die path
      // clears all registers + zeroes lr before BR x0.
      //
      // Tested approach: redirect PC to pthread_exit so the dying
      // thread terminates cleanly without killing the process.
      // OUTCOME: Thread-2 died fine, but Bambu's main thread then
      // futex-waited forever on a signal that Thread-2 was supposed
      // to broadcast (the shield expects Thread-2 to publish a
      // "no-tamper" status). With Thread-2 gone, main thread blocks
      // indefinitely (verified via /proc/PID/syscall = 98 (futex_wait)
      // on address 0x70c8215cc0). Verdict: pthread_exit redirect
      // FREEZES Bambu instead of killing it — strictly worse than
      // letting SIGBUS propagate.
      //
      // Reverting to bail-fast: return false on first invalid-lr
      // exception, kernel delivers SIGSEGV, process dies cleanly,
      // user sees a tombstone, can restart. Mitm-proxy path captures
      // crypto in subsequent fresh runs without needing the gadget
      // armed at all.
      A(`!! exception #${_excCount} ${t} @ ${addrStr} — invalid lr=${lr}, propagating signal (process will be killed by kernel)`);
      return false;
    });
    A('installed exception handler (return-to-LR with NativePointer-isNull fix)');
  } catch (e) { A(`exception handler install failed: ${e}`); }

  A('anti-anti-debug installed');
}

// Synchronous install at gadget dlopen time blocks Bambu's dynamic
// linker from advancing past dlopen() — the loader stalls before
// .init_array, so the app never starts. Defer to setTimeout(50): the
// linker resumes immediately, and our hooks land before the shield's
// ptrace-daemon thread is up.
//
// Even at 50ms there is a window in which the shield could ptrace
// successfully — but the return-to-LR exception handler (installed
// inside installAntiAntiDebug) covers the tamper-die path that fires
// when we later interfere. So an early ptrace race is non-fatal.
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

// ---------- Flutter bundled BoringSSL (libflutter.so) ---------------------
//
// libflutter.so ships its own BoringSSL with all symbols stripped (only
// 50 dynamic exports, all `InternalFlutterGpu_*`). System libcrypto hooks
// don't fire because Bambu's Dart HTTP client (dart:io / dio) goes
// through this bundled BoringSSL, not Conscrypt.
//
// Static analysis (runtime/handy_extract/find_boringssl.py against the
// libflutter.so we extracted from split_config.arm64_v8a.apk) located
// the function entry offsets below by xref'ing rodata source-path strings
// (e.g. "boringssl/src/ssl/ssl_privkey.cc") and walking back to the
// nearest STP X29,X30,[SP,#-N]! prologue. Offsets are RELATIVE to the
// libflutter.so loaded base — add them to module.base at hook time.
//
// Drift-resistance: Flutter Engine pinned versions correlate strongly
// with these offsets. If a future Flutter version changes them, re-run
// find_boringssl.py against the new libflutter.so and replace this map.
//
// Map produced 2026-04-30 against the Bambu Handy v3.19.0 split-config
// libflutter.so (BuildID 0a7fde9baaf490ad50a8480ebc422ea4ee862a2e).
const LIBFLUTTER_BORINGSSL_OFFSETS = {
  // Shared-key primitive — fires on every TLS / signed-MQTT publish.
  // RSA pkey ctx contains the BIGNUM* fields (n, e, d, p, q, ...).
  pkey_rsa_sign:        0x6ed024,
  // Cert-chain validation — fires when Bambu validates the cloud's
  // server cert during the TLS handshake. Safe place to disable
  // pinning by always returning 1.
  ASN1_item_verify:     0x6f4794,
  // SSL-level signing path — Bambu calls this when its TLS stack
  // signs handshake transcripts (or when it signs MQTT publishes
  // through libflutter's BoringSSL). Buffer is the raw to-be-signed
  // bytes; key context contains RSA*.
  ssl_private_key_sign: 0x70f378,
  // Generic SSL_lib entry — could be SSL_read / SSL_write / SSL_get_error.
  // Hook with a header-byte sniff to disambiguate at runtime.
  ssl_lib_unknown:      0x84b55c,
  ssl_private_key_sign_2: 0x8537a0,
};

function hookFlutterBoringSSL() {
  let count = 0;
  const mod = Process.findModuleByName('libflutter.so');
  if (!mod) {
    PRINT('libflutter.so not loaded — skipping bundled BoringSSL hooks');
    return 0;
  }
  PRINT(`libflutter.so @ ${mod.base} (size ${mod.size}) — installing bundled-BoringSSL hooks`);
  for (const [name, offset] of Object.entries(LIBFLUTTER_BORINGSSL_OFFSETS)) {
    const addr = mod.base.add(offset);
    try {
      if (name.startsWith('ssl_private_key_sign')) {
        // ssl_private_key_sign(SSL *ssl, uint8_t *out, size_t *out_len,
        //                       size_t max_out, uint16_t signature_algorithm,
        //                       const uint8_t *in, size_t in_len)
        // Capture both the input (pre-sign data) AND the output buffer
        // (signature). The signed data is what we want — that's the
        // exact bytes the printer will verify.
        Interceptor.attach(addr, {
          onEnter(args) {
            this.out = args[1];
            this.outlenp = args[2];
            this.sigalg = args[4].toInt32();
            this.in = args[5];
            this.inlen = args[6].toUInt32();
            EMIT('flutter_sign_call', {
              fn: name, mod: 'libflutter.so',
              sig_alg: this.sigalg, in_len: this.inlen,
              in_hex: this.in && this.inlen > 0
                ? this.in.readByteArray(Math.min(this.inlen, 512))
                : null
            });
          },
          onLeave(retval) {
            // Return value: ssl_private_key_success(=0) | ssl_private_key_failure(=1) | ssl_private_key_retry(=2)
            // We only emit on success — failed signs aren't useful.
            if (retval.toInt32() !== 0) return;
            try {
              const olen = this.outlenp.readU32();
              EMIT('flutter_sign_result', {
                fn: name, mod: 'libflutter.so',
                out_len: olen,
                out_hex: this.out.readByteArray(Math.min(olen, 1024))
              });
              PRINT(`[!] ssl_private_key_sign produced ${olen}-byte signature (alg=${this.sigalg})`);
            } catch (e) {}
          }
        });
        PRINT(`hooked libflutter.so!${name} @ ${addr}`);
        count++;
      } else if (name === 'ASN1_item_verify') {
        // ASN1_item_verify(it, alg, signature, pkey, asn) — used in cert
        // chain validation. We DON'T patch the return value here because
        // Bambu's printer cert may legitimately need to fail before the
        // user-installed cert is trusted. We just log so we can see what
        // certificates are being validated.
        Interceptor.attach(addr, {
          onEnter(args) {
            this.alg = args[1];
            this.pkey = args[3];
            this.sig = args[2];
          },
          onLeave(retval) {
            EMIT('flutter_cert_verify', {
              fn: name, mod: 'libflutter.so',
              result: retval.toInt32(),
            });
          }
        });
        PRINT(`hooked libflutter.so!${name} @ ${addr}`);
        count++;
      } else if (name === 'pkey_rsa_sign') {
        // pkey_rsa_sign(EVP_PKEY_CTX *ctx, uint8_t *sig, size_t *siglen,
        //               const uint8_t *tbs, size_t tbslen)
        Interceptor.attach(addr, {
          onEnter(args) {
            this.tbs = args[3];
            this.tbslen = args[4].toUInt32();
            EMIT('flutter_pkey_rsa_sign', {
              fn: name, mod: 'libflutter.so', tbslen: this.tbslen,
              tbs_hex: this.tbs && this.tbslen > 0
                ? this.tbs.readByteArray(Math.min(this.tbslen, 512))
                : null
            });
          }
        });
        PRINT(`hooked libflutter.so!${name} @ ${addr}`);
        count++;
      } else if (name === 'ssl_lib_unknown') {
        // 0x84b55c is ambiguous — could be SSL_read / SSL_write / SSL_get_error.
        // SSL_read(SSL*, void* buf, int num) and SSL_write(SSL*, const void* buf, int num)
        // both have buf as args[1] and num as args[2]. SSL_get_error takes only
        // SSL* + int. Hook conservatively: if return value > 0 and args[1] looks
        // like a valid pointer with > 32 bytes, treat as SSL_read/write and
        // dump the buffer.
        Interceptor.attach(addr, {
          onEnter(args) {
            this.arg1 = args[1];
            this.arg2 = args[2].toInt32();
          },
          onLeave(retval) {
            const rv = retval.toInt32();
            if (rv <= 32) return;  // too small to be useful payload
            if (!this.arg1 || this.arg1.isNull()) return;
            try {
              const head = this.arg1.readByteArray(Math.min(rv, 256));
              EMIT('flutter_ssl_io', {
                fn: name + '_(read_or_write)', mod: 'libflutter.so',
                len: rv, head_hex: head
              });
            } catch (e) {}
          }
        });
        PRINT(`hooked libflutter.so!${name} (SSL_read/write probe) @ ${addr}`);
        count++;
      }
    } catch (e) {
      PRINT(`hook libflutter.so!${name} failed: ${e}`);
    }
  }
  return count;
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
    total += hookFlutterBoringSSL();
    hookHTTPS();
    PRINT(`installed ${total} crypto hooks. Now interact with the app — every`);
    PRINT('signed MQTT publish or AES decrypt will be reported.');
  } catch (e) {
    PRINT('init error: ' + e.stack);
  }
}, 250);   // brief delay so the packer's .init_array finishes unpacking before we hook
