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

  // Compute UTF-8 byte length without allocating an intermediate Buffer.
  // JS String.length counts UTF-16 code units, not bytes; using it as the
  // fwrite count truncates the trailing '\n' whenever the message contains
  // any non-ASCII character (e.g. em-dash), leading to log lines that
  // concatenate without separator.
  function utf8ByteLength(s) {
    let n = 0;
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      if (c < 0x80) n += 1;
      else if (c < 0x800) n += 2;
      else if (c >= 0xD800 && c <= 0xDBFF) { n += 4; i++; } // surrogate pair
      else n += 3;
    }
    return n;
  }
  globalThis._fcap = function(s) {
    try {
      const t = String(s) + '\n';
      const buf = Memory.allocUtf8String(t);
      fwriteF(buf, 1, utf8ByteLength(t), fp);
      if (fflushF) fflushF(fp);
    } catch (e) {}
  };
  _fcap('=== gadget started: ' + new Date().toISOString() + ' ===');
})();
