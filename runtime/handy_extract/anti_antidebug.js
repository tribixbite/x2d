// anti_antidebug.js — defeat the shield's three anti-debug layers
// before the app's .init_array constructors run.
//
// Loaded as the FIRST hook in spawn mode. Order of operations:
//   1. Block every `ptrace(...)` syscall — return 0 success without
//      actually ptracing. Defeats the self-ptrace watchdog (the shield
//      can't establish TracerPid).
//   2. Block `prctl(PR_SET_SECCOMP, ...)` and `prctl(PR_SET_DUMPABLE, 0)`
//      — return 0 without applying. App keeps full /proc visibility and
//      no seccomp filter ever installs, so the libgum agent's pipe
//      isn't blocked.
//   3. Block `syscall(SYS_seccomp, ...)` and `syscall(SYS_ptrace, ...)`
//      — same as above but for code that bypasses libc wrappers.
//   4. Patch `read()` of `/proc/self/status` so any code that reads
//      TracerPid sees 0 (defensive — should never trigger because
//      step 1 already keeps TracerPid=0).
//
// Per ARM64 syscall numbering (asm-generic + arm64 specifics):
//   ptrace = 117
//   seccomp = 277
//   prctl  = 167
//
// PR_SET_SECCOMP = 22, PR_SET_DUMPABLE = 4, PR_SET_PTRACER = 0x59616D61

'use strict';

(function() {
  const PRINT = (m) => send({type:'log', msg: '[anti] ' + m});

  // 1. ptrace
  const ptracePtr = Module.findExportByName(null, 'ptrace');
  if (ptracePtr) {
    Interceptor.attach(ptracePtr, {
      onEnter(args) {
        this.req = args[0].toInt32();
        this.pid = args[1].toInt32();
      },
      onLeave(retval) {
        // PTRACE_TRACEME=0, PTRACE_ATTACH=16, PTRACE_DETACH=17, etc.
        // Always succeed (0) for any ptrace call from inside the app.
        const before = retval.toInt32();
        retval.replace(0);
        PRINT(`ptrace(req=${this.req}, pid=${this.pid}) -> faked 0 (was ${before})`);
      }
    });
    PRINT(`hooked ptrace @ ${ptracePtr}`);
  } else {
    PRINT('ptrace export not found — falling back to syscall hook');
  }

  // 2. prctl
  const prctlPtr = Module.findExportByName(null, 'prctl');
  if (prctlPtr) {
    Interceptor.attach(prctlPtr, {
      onEnter(args) {
        this.opt = args[0].toInt32();
        this.arg2 = args[1];
      },
      onLeave(retval) {
        // PR_SET_SECCOMP = 22  → block (don't install seccomp filter)
        // PR_SET_DUMPABLE = 4 → block when arg2 == 0 (anti-dump)
        // PR_SET_PTRACER = 0x59616D61 → block (some apps narrow ptracer)
        if (this.opt === 22) {
          retval.replace(0);
          PRINT(`prctl(PR_SET_SECCOMP) blocked`);
        } else if (this.opt === 4 && this.arg2.toInt32() === 0) {
          retval.replace(0);
          PRINT(`prctl(PR_SET_DUMPABLE, 0) blocked`);
        } else if (this.opt === 0x59616D61) {
          retval.replace(0);
          PRINT(`prctl(PR_SET_PTRACER) blocked`);
        }
      }
    });
    PRINT(`hooked prctl @ ${prctlPtr}`);
  }

  // 3. raw syscall() — for code that bypasses libc wrappers
  const syscallPtr = Module.findExportByName(null, 'syscall');
  if (syscallPtr) {
    Interceptor.attach(syscallPtr, {
      onEnter(args) { this.nr = args[0].toInt32(); },
      onLeave(retval) {
        // SYS_seccomp=277, SYS_ptrace=117 on arm64
        if (this.nr === 277 || this.nr === 117) {
          retval.replace(0);
          PRINT(`syscall nr=${this.nr} blocked`);
        }
      }
    });
    PRINT(`hooked syscall @ ${syscallPtr}`);
  }

  // 4. Patch reads from /proc/self/status that contain TracerPid.
  // The shield's watchdog typically reads /proc/self/status periodically
  // and SIGKILLs the parent if TracerPid != 0 (when expecting its
  // sibling) or if TracerPid != expected. By rewriting the buffer
  // post-read, we lie consistently.
  const openatPtr = Module.findExportByName(null, 'openat');
  const openPtr = Module.findExportByName(null, 'open');
  const readPtr = Module.findExportByName(null, 'read');
  const procStatusFds = new Set();
  function watchOpen(name, ptr) {
    if (!ptr) return;
    Interceptor.attach(ptr, {
      onEnter(args) {
        // openat(dirfd, path, flags) ; open(path, flags)
        const pathArg = (name === 'openat') ? args[1] : args[0];
        try {
          const path = pathArg.readCString() || '';
          this.isStatus = path.includes('/status') || path.includes('/proc/self/status');
        } catch (e) { this.isStatus = false; }
      },
      onLeave(retval) {
        if (this.isStatus && retval.toInt32() >= 0) {
          procStatusFds.add(retval.toInt32());
        }
      }
    });
    PRINT(`hooked ${name} @ ${ptr}`);
  }
  watchOpen('open', openPtr);
  watchOpen('openat', openatPtr);
  if (readPtr) {
    Interceptor.attach(readPtr, {
      onEnter(args) {
        this.fd = args[0].toInt32();
        this.buf = args[1];
        this.shouldPatch = procStatusFds.has(this.fd);
      },
      onLeave(retval) {
        if (!this.shouldPatch) return;
        const n = retval.toInt32();
        if (n <= 0) return;
        try {
          const text = this.buf.readUtf8String(n);
          if (text && text.includes('TracerPid:')) {
            const patched = text.replace(/TracerPid:\s*\d+/g, 'TracerPid:\t0');
            this.buf.writeUtf8String(patched);
            PRINT(`/proc/self/status read patched (TracerPid → 0)`);
          }
        } catch (e) {}
      }
    });
    PRINT(`hooked read @ ${readPtr}`);
  }

  PRINT('anti-anti-debug installed.');
})();
