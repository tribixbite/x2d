// After handy_hook.js installs, this runs and patches its hooks to also
// call _fcap. Done by overriding script-locals via globalThis if exposed,
// or by re-installing duplicate hooks. Simplest: monkey-patch the Frida
// `send` to ALSO call _fcap *only* on incoming messages from the
// Interceptor, NOT on outgoing replies — but since we don't know how to
// distinguish, just shadow send to dual-emit:
(function() {
  if (typeof _fcap !== 'function') return;
  // Use Object.defineProperty so we can intercept reads of 'send'.
  // But that won't change the existing function references in the
  // already-loaded hook script's closures. Instead, we hook into
  // Frida's Interceptor.attach via wrapping it — every future hook
  // installed will dual-emit.
  const _oldAttach = Interceptor.attach;
  Interceptor.attach = function(target, callbacks) {
    const wrapped = {};
    for (const k of Object.keys(callbacks || {})) {
      const orig = callbacks[k];
      if (typeof orig !== 'function') { wrapped[k] = orig; continue; }
      wrapped[k] = function() {
        try { return orig.apply(this, arguments); }
        catch (e) { _fcap('[err in '+k+'] '+e); throw e; }
      };
    }
    return _oldAttach.call(this, target, wrapped);
  };
  _fcap('Interceptor.attach wrapper installed');
})();
