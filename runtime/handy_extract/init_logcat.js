// Push captured events through logcat (visible via adb logcat -s FCAP)
(function() {
  const findSym = (n) => {
    for (const m of Process.enumerateModules()) {
      try { const a = m.findExportByName(n); if (a) return a; } catch (e) {}
    }
    return null;
  };
  const alpAddr = findSym('__android_log_print');
  if (!alpAddr) return;
  const ALP = new NativeFunction(alpAddr, 'int',
                                 ['int','pointer','pointer','pointer'],
                                 {exceptions:'propagate'});
  const TAG = Memory.allocUtf8String('FCAP');
  const FMT = Memory.allocUtf8String('%s');
  globalThis._fcap = function(s) {
    try { ALP(3, TAG, FMT, Memory.allocUtf8String(String(s))); } catch (e) {}
  };
  globalThis.send = function(payload, data) {
    try { _fcap('SEND ' + JSON.stringify(payload).substring(0, 8000)); } catch (e) {}
  };
  _fcap('init_logcat.js loaded');
})();
