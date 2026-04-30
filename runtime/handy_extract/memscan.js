// memscan.js — periodic scan of the app's writable memory for PEM/DER
// markers (PKCS#8 private keys, X.509 certs). Bambu Handy uses Flutter
// + BoringSSL bundled inside libapp.so; system libcrypto hooks miss
// the actual RSA operations. Brute-force: search heap for known
// markers when the app is running.

'use strict';
(function() {
  const findSym = (n) => {
    for (const m of Process.enumerateModules()) {
      try { const a = m.findExportByName(n); if (a) return a; } catch (e) {}
    }
    return null;
  };

  const PEM_PRIV = '-----BEGIN PRIVATE KEY-----';
  const PEM_RSA  = '-----BEGIN RSA PRIVATE KEY-----';
  const PEM_CERT = '-----BEGIN CERTIFICATE-----';

  const dumped = new Set();

  function dump(label, addr, len) {
    const sig = addr.toString() + ':' + len;
    if (dumped.has(sig)) return;
    dumped.add(sig);
    try {
      const bytes = Memory.readByteArray(addr, len);
      const txt = new TextDecoder('utf-8', {fatal:false}).decode(bytes);
      if (typeof _fcap === 'function') {
        _fcap('PEM_FOUND ' + label + ' @ ' + addr + ' (' + len + 'B)\n' + txt);
      }
    } catch (e) {}
  }

  function scanOnce() {
    let total_hits = 0;
    Process.enumerateRanges('rw-').forEach(range => {
      try {
        const matches = Memory.scanSync(range.base, range.size, "2D 2D 2D 2D 2D 42 45 47 49 4E 20"); // "----- BEGIN "
        for (const m of matches) {
          // Read 64 bytes to identify the marker
          let header;
          try { header = Memory.readUtf8String(m.address, 64); } catch (e) { continue; }
          if (!header) continue;
          let label = '?';
          if (header.startsWith(PEM_PRIV)) label = 'PKCS8_PRIV';
          else if (header.startsWith(PEM_RSA)) label = 'PKCS1_RSA';
          else if (header.startsWith(PEM_CERT)) label = 'X509_CERT';
          else continue; // some other PEM marker, skip
          // Try to read up to 8KB to capture the full PEM body
          let body;
          try { body = Memory.readUtf8String(m.address, 8192); } catch (e) { continue; }
          if (!body) continue;
          // Trim at the END marker
          const endIdx = body.indexOf('-----END ');
          if (endIdx > 0) {
            // extend to include the line ending
            const tailIdx = body.indexOf('-----', endIdx + 9);
            if (tailIdx > 0) body = body.substring(0, tailIdx + 5);
          }
          dump(label, m.address, body.length);
          total_hits++;
        }
      } catch (e) {}
    });
    return total_hits;
  }

  // setInterval is unreliable in script-mode. Hook the read syscall and
  // rate-limit a memscan trigger from there. Frequent reads → frequent
  // checks. App's natural file I/O drives the schedule.
  globalThis._memscan = scanOnce;
  let read_count = 0;
  const readSym = findSym('read');
  if (readSym) {
    Interceptor.attach(readSym, {
      onLeave() {
        read_count++;
        if (read_count === 200 || read_count === 2000 ||
            (read_count >= 5000 && read_count % 5000 === 0)) {
          try {
            const hits = scanOnce();
            if (typeof _fcap === 'function')
              _fcap(`memscan(read#${read_count}): ${hits} new`);
          } catch (e) {
            if (typeof _fcap === 'function') _fcap('memscan err: '+e);
          }
        }
      }
    });
    if (typeof _fcap === 'function') _fcap('memscan: read-triggered scan armed');
  }
  // Initial scan now (immediate)
  try {
    const hits = scanOnce();
    if (typeof _fcap === 'function') _fcap(`memscan(initial): ${hits} hits`);
  } catch (e) {
    if (typeof _fcap === 'function') _fcap('memscan initial err: '+e);
  }
})();
