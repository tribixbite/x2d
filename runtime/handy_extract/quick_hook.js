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
