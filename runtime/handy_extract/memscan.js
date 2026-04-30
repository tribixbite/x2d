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

  // DER patterns we're looking for in app memory:
  //   PKCS#8 RSA-2048 priv:  30 82 04 BD..C4   02 01 00 30 0D 06 09 2A 86 48 86 F7 0D 01 01 01
  //   PKCS#1 RSA-2048 priv:  30 82 04 A0..A9   02 01 00 02 82 01 01 00
  //   PKCS#8 RSA-4096 priv:  30 82 09 3D..43   02 01 00 30 0D 06 09 2A 86 48 86 F7 0D 01 01 01
  //   X.509 cert (DER):      30 82 LL LL       30 82 LL LL A0 03 02 01 02 02
  // We scan for the static prefix "30 82 ?? ?? 02 01 00 30 0D 06 09 2A 86 48 86 F7 0D 01 01 01" (PKCS#8)
  // and "30 82 ?? ?? 02 01 00 02 82 01 01 00" (PKCS#1 RSA-2048).
  const PATTERNS = [
    {label:'PEM_BEGIN',     bytes:'2D 2D 2D 2D 2D 42 45 47 49 4E 20', dump_text: true},
    {label:'PKCS8_DER',     bytes:'30 82 ?? ?? 02 01 00 30 0D 06 09 2A 86 48 86 F7 0D 01 01 01', dump_text: false, dump_len: 1216},
    {label:'PKCS1_RSA_DER', bytes:'30 82 ?? ?? 02 01 00 02 82 01 01 00', dump_text: false, dump_len: 1192},
  ];

  function scanOnce() {
    let total_hits = 0;
    Process.enumerateRanges('rw-').forEach(range => {
      for (const pat of PATTERNS) {
        try {
          const matches = Memory.scanSync(range.base, range.size, pat.bytes);
          for (const m of matches) {
            if (pat.dump_text) {
              // PEM marker — read string
              let header;
              try { header = Memory.readUtf8String(m.address, 64); } catch (e) { continue; }
              if (!header) continue;
              let label;
              if (header.startsWith(PEM_PRIV)) label = 'PKCS8_PRIV';
              else if (header.startsWith(PEM_RSA)) label = 'PKCS1_RSA';
              else if (header.startsWith(PEM_CERT)) label = 'X509_CERT';
              else continue;
              let body;
              try { body = Memory.readUtf8String(m.address, 8192); } catch (e) { continue; }
              if (!body) continue;
              const endIdx = body.indexOf('-----END ');
              if (endIdx > 0) {
                const tailIdx = body.indexOf('-----', endIdx + 9);
                if (tailIdx > 0) body = body.substring(0, tailIdx + 5);
              }
              dump(label, m.address, body.length);
              total_hits++;
            } else {
              // DER blob — read raw bytes, hex-encode
              let bytes;
              try { bytes = Memory.readByteArray(m.address, pat.dump_len); }
              catch (e) { continue; }
              const hex = Array.from(new Uint8Array(bytes))
                .map(b => b.toString(16).padStart(2,'0')).join('');
              if (typeof _fcap === 'function') {
                const sig = m.address.toString();
                if (!dumped.has(sig + ':' + pat.label)) {
                  dumped.add(sig + ':' + pat.label);
                  _fcap('DER_FOUND ' + pat.label + ' @ ' + m.address +
                        ' (' + pat.dump_len + 'B): ' + hex);
                  total_hits++;
                }
              }
            }
          }
        } catch (e) {}
      }
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
