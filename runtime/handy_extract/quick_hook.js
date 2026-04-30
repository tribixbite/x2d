// quick_hook.js — minimal hook to load INSTANTLY when gadget comes up.
// Only the bare minimum to defeat the shield's Thread-2 check that fires
// ~500ms after process spawn. Goal: hook /proc/self/maps reads to filter
// out our injected libs.
//
// CRITICAL: Bambu Handy ANRs (Input dispatching timed out, 5005ms) when
// our read() hook stays armed long-term — every read() call triggers a
// JS-bridge crossing, and a Flutter app does millions of read()s on the
// UI/IO threads. Bambu's main thread blocks on the JS-VM lock and the
// 5-second input-dispatch budget is exhausted.
//
// Mitigation: detach BOTH openat and read interceptors after a guard
// window (default 12s). The shield's Thread-2 path-fingerprint check
// fires ~455ms after gadget bind and is one-shot — once it has cleared
// successfully there is no need to keep filtering. Anti-anti-debug
// hooks (in handy_hook.js) survive this detach.

'use strict';
const A = (m) => send({type:'log', msg:'[q] ' + m});

// Earlier 12 s default was too short — the shield's tamper-die fires on
// every /proc/self/maps re-read, including during user-driven flows like
// "tap Me → OAuth deep-link comes back → cert fetch". 5 min covers a full
// realistic login + first-printer-bind walkthrough; can be raised to
// `Infinity` for indefinite filtering at the cost of ~5 µs JS-bridge
// overhead per libc read() across the entire process lifetime.
const FILTER_WINDOW_MS = 300000;  // detach interceptors after this many ms

// 1. Find symbols
function findSym(name) {
  for (const m of Process.enumerateModules()) {
    try { const a = m.findExportByName(name); if (a) return a; } catch (e) {}
  }
  return null;
}

const armedListeners = [];

// 2. Hook openat to track which fds are reading /proc/self/maps
const procMapsFds = new Set();
const openat = findSym('openat');
if (openat) {
  armedListeners.push(Interceptor.attach(openat, {
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
  }));
  A('hooked openat');
}

// 3. Hook read to filter out gadget/sysrt/frida/zygisk lines from /proc/self/maps
const readFn = findSym('read');
if (readFn) {
  armedListeners.push(Interceptor.attach(readFn, {
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
          this.buf.writeUtf8String(filtered);
          retval.replace(filtered.length);
          A(`filtered /proc/.../maps read: ${text.length}B → ${filtered.length}B`);
        }
      } catch (e) {}
    }
  }));
  A('hooked read');
}

// 4. Also hook fopen for older code paths (cheap, only fires on file opens)
const fopen = findSym('fopen');
if (fopen) {
  armedListeners.push(Interceptor.attach(fopen, {
    onEnter(args) {
      try {
        const path = args[0].readCString() || '';
        if (path.includes('/proc/') && path.includes('/maps')) {
          A(`fopen on maps: ${path} (passed through)`);
        }
      } catch (e) {}
    }
  }));
  A('hooked fopen');
}

// 5. Auto-detach after the guard window — keeps Bambu's main thread out
// of read()-hook contention once the shield's path check is past us.
// Frida's setTimeout is implemented on top of the agent message pump and
// fires reliably even in script-mode gadgets.
setTimeout(() => {
  for (const l of armedListeners) {
    try { l.detach(); } catch (e) {}
  }
  armedListeners.length = 0;
  procMapsFds.clear();
  if (typeof _fcap === 'function')
    _fcap('quick_hook detached after ' + FILTER_WINDOW_MS + 'ms — read filter retired');
}, FILTER_WINDOW_MS);

A(`quick_hook armed (mode: filter /proc/self/maps reads, ${FILTER_WINDOW_MS}ms window)`);
