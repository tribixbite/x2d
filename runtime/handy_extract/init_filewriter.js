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
