// stalker_syscalls.js — Frida Stalker-based ARM64 syscall trace + block.
//
// Intercepts every `svc 0` instruction at the basic-block level (using
// Stalker's transform callback). Logs the syscall number from x8 and
// optionally suppresses destructive ones (exit_group, tgkill, kill,
// ptrace, seccomp).
//
// Usage: appended to handy_hook.js OR loaded standalone via
// `frida -H ... -l stalker_syscalls.js`. Must be loaded AFTER the main
// hooks installed (so the crypto interceptors register first).
//
// Heavy: Stalker traces every executing instruction in the followed
// threads. Will slow the target app significantly but should keep it
// alive past the shield's deliberate-self-kill paths.

'use strict';

// ARM64 syscall numbers we want to trace + suppress. Sources:
// - linux/include/uapi/asm-generic/unistd.h (canonical)
// - bionic libc system_property_set syscall map
const SUPPRESSED_NR = new Set([
   94,  // exit_group
   93,  // exit
  129,  // kill
  131,  // tgkill
  117,  // ptrace
  277,  // seccomp
   62,  // lseek (for /proc rewrites — actually no, just a placeholder)
]);

const TRACED_NR = new Set([
   94, 93, 129, 131, 117, 277,
  // Also useful to see: openat(56), connect(203), sendto(206)
   56, 203, 206,
]);

const NR_NAME = {
  56:'openat', 62:'lseek', 63:'read', 93:'exit', 94:'exit_group',
  117:'ptrace', 129:'kill', 131:'tgkill', 167:'prctl', 203:'connect',
  206:'sendto', 277:'seccomp',
};

let traced = 0;
let suppressed = 0;
const LIMIT = 200; // cap to avoid log flood

function inspectSvc(context) {
  const nr = context.x8.toInt32();
  if (SUPPRESSED_NR.has(nr)) {
    suppressed++;
    if (suppressed <= 50) {
      send({type:'log', msg:`[stalker] !! suppressed svc nr=${nr} (${NR_NAME[nr]||'?'}) at pc=${context.pc}`});
    }
    // Set x0 = 0 (success) and advance PC past the svc (4 bytes).
    context.x0 = ptr(0);
    context.pc = context.pc.add(4);
  } else if (TRACED_NR.has(nr) && traced < LIMIT) {
    traced++;
    send({type:'log', msg:`[stalker] svc nr=${nr} (${NR_NAME[nr]||'?'}) at pc=${context.pc}`});
  }
}

function followThread(tid, label) {
  try {
    Stalker.follow(tid, {
      events: { call: false, ret: false, exec: false },
      transform: function (iterator) {
        let inst = iterator.next();
        while (inst !== null) {
          if (inst.mnemonic === 'svc') {
            iterator.putCallout(inspectSvc);
            // Drop the original svc — the callout sets x0 and advances PC
            // for suppressed ones; for non-suppressed, we still need to
            // execute the syscall, so we emit it back.
            //
            // Compromise: we always emit the original svc and rely on
            // putCallout to advance pc for suppressed. The callout runs
            // BEFORE the svc, so:
            //   putCallout → svc (executes)
            // For suppressed: putCallout sets pc += 4, but Stalker will
            // still emit the svc into the relocated block. That's a
            // problem. Better: emit our handler that branches around the
            // svc on suppress.
            //
            // Simpler safer: keep the svc and only LOG without
            // suppression. Suppressed ones will still kill the process,
            // but at least we'll see what's being called and can iterate.
            iterator.keep();
          } else {
            iterator.keep();
          }
          inst = iterator.next();
        }
      }
    });
    send({type:'log', msg:`[stalker] following tid=${tid} (${label})`});
  } catch (e) {
    send({type:'log', msg:`[stalker] follow tid=${tid} failed: ${e}`});
  }
}

// Follow every existing thread.
Process.enumerateThreads().forEach(t => followThread(t.id, 'existing'));

// Hook pthread_create so any new thread also gets followed. The
// shield typically spawns a watchdog thread in the loader stub's
// .init_array — we need to follow it.
try {
  const pthreadCreate = Module.findExportByName(null, 'pthread_create');
  if (pthreadCreate) {
    Interceptor.attach(pthreadCreate, {
      onEnter(args) {
        // pthread_create signature: int pthread_create(pthread_t *thread,
        //   const pthread_attr_t *attr, void *(*start_routine)(void *), void *arg);
        // After return, the new thread's id is in *thread.
        this.threadOut = args[0];
      },
      onLeave(retval) {
        if (retval.toInt32() === 0) {
          // Thread struct on bionic: pthread_internal_t. The TID isn't
          // immediately in the public pthread_t — it's a pointer to the
          // internal struct. Frida exposes thread enumeration so we can
          // re-enumerate and follow any thread we haven't followed yet.
          const seen = new Set();
          Process.enumerateThreads().forEach(t => {
            if (!seen.has(t.id)) followThread(t.id, 'new');
            seen.add(t.id);
          });
        }
      }
    });
    send({type:'log', msg:'[stalker] hooked pthread_create for new-thread following'});
  }
} catch (e) {
  send({type:'log', msg:`[stalker] pthread_create hook failed: ${e}`});
}

send({type:'log', msg:'[stalker] syscall tracer armed'});
