// stalker_syscalls.js — ARM64 raw-svc tracer + suppressor.
//
// The shield's `.init_array` constructors call destructive syscalls
// (exit_group, kill, tgkill) via raw `svc 0` instructions, bypassing
// libc wrappers and our Interceptor.attach('ptrace'/'prctl'/...) hooks.
// To stop them we have to rewrite each `svc 0` instruction in the
// instruction stream — that's what Stalker.transform does.
//
// On every basic block compiled by Stalker:
//   - For each svc instruction, replace it with a putCallout that
//     handles the syscall in JavaScript context. We DO NOT keep() the
//     original svc, so it is dropped from the trampolined block.
//   - The callout reads x8 (syscall number) and x0..x5 (args).
//   - If the syscall is in our SUPPRESSED set, set x0 = 0 (success)
//     and return — the dropped svc means nothing happened.
//   - Otherwise call libc's syscall() to actually perform the request,
//     write the result back into x0.
//
// Threads: we follow every existing thread and hook pthread_create so
// new threads (the shield's watchdog, in particular) are also followed.
//
// Lifetime: Stalker overhead on every basic block is measurable (~10×
// slowdown per instruction). After the app's startup tamper-checks
// have all run (typically <5 s), call `unfollowAll()` from the Frida
// REPL or via a timer to drop back to normal speed.

'use strict';

const A = (m) => send({type:'log', msg: '[stalker] ' + m});

// ARM64 syscall numbers. Source: linux/include/uapi/asm-generic/unistd.h
const SUPPRESSED_NR = new Set([
   93,  // exit
   94,  // exit_group   ← shield's primary kill path
  117,  // ptrace        ← would let shield re-establish self-ptrace
  129,  // kill          ← signal-based abort
  131,  // tgkill        ← thread-targeted abort
  277,  // seccomp       ← prevent re-installing a filter
]);

const NR_NAME = {
   56:'openat', 62:'lseek', 63:'read', 93:'exit', 94:'exit_group',
  117:'ptrace', 129:'kill', 131:'tgkill', 167:'prctl', 203:'connect',
  206:'sendto', 277:'seccomp',
};

function findSym(name) {
  for (const m of Process.enumerateModules()) {
    try { const a = m.findExportByName(name); if (a) return a; } catch (e) {}
  }
  return null;
}

// Mark system / runtime libs as excluded so Stalker doesn't rewrite
// every libart / libc / libssl basic block. Drastically reduces overhead
// — we only care about app-bundled code (libapp.so, the loader stub,
// the unpacked dex/payload). Without this, a Flutter app's ~14 threads
// each running art/libc/libssl will trip Android's ANR watchdog.
function excludeSystemModules() {
  const SYSTEM_PREFIXES = [
    '/system/', '/apex/', '/vendor/', '/product/', '/system_ext/',
  ];
  let excluded = 0;
  for (const m of Process.enumerateModules()) {
    if (SYSTEM_PREFIXES.some(p => (m.path || '').startsWith(p))) {
      try {
        Stalker.exclude({ base: m.base, size: m.size });
        excluded++;
      } catch (e) { /* range may overlap with one already excluded */ }
    }
  }
  send({type:'log', msg:`[stalker] excluded ${excluded} system module ranges`});
}

// Stats — printed by `stats()` callable from the REPL or after a
// time-based unfollow.
const stats = {
  total_svc: 0,
  suppressed: 0,
  by_nr: {},
};

let logged_suppressed = 0;
const LOG_LIMIT = 80;

// Callout that runs RIGHT BEFORE the original svc instruction. For
// suppressed syscall numbers we set x0 = 0 (success) and advance PC
// past the svc so Stalker's continuation skips it. For everything
// else we leave context untouched — the svc executes normally.
function handleSvc(context) {
  const nr = context.x8.toInt32();
  stats.total_svc++;
  stats.by_nr[nr] = (stats.by_nr[nr] || 0) + 1;
  if (!SUPPRESSED_NR.has(nr)) return;
  stats.suppressed++;
  if (logged_suppressed < LOG_LIMIT) {
    logged_suppressed++;
    A(`!! suppressed svc nr=${nr} (${NR_NAME[nr]||'?'}) pc=${context.pc}`);
  }
  context.x0 = ptr(0);
  // ARM64 instructions are fixed 4 bytes. Bump PC past the svc.
  context.pc = context.pc.add(4);
}

const followed = new Set();

function followThread(tid, label) {
  if (followed.has(tid)) return;
  followed.add(tid);
  try {
    Stalker.follow(tid, {
      events: { call: false, ret: false, exec: false, block: false },
      transform: function (iterator) {
        let inst;
        while ((inst = iterator.next()) !== null) {
          if (inst.mnemonic === 'svc') {
            // Insert a callout immediately before the svc. The callout
            // either lets the svc execute (returns without changing pc)
            // or skips it by advancing pc — Stalker's relocator honours
            // a modified pc value and continues at that address.
            iterator.putCallout(handleSvc);
          }
          iterator.keep();
        }
      }
    });
    A(`following tid=${tid} (${label})`);
  } catch (e) {
    A(`follow tid=${tid} failed: ${e}`);
    followed.delete(tid);
  }
}

function followAll(label) {
  Process.enumerateThreads().forEach(t => followThread(t.id, label || 'enum'));
}

// Restricted follow: only the MAIN thread (tid == process.id). The
// shield's tamper-detect runs on the main thread (during loadLibrary
// of assets/l6a18f19c_a64.so — its .init_array fires in the loading
// thread which is the caller). New threads spawned by the shield's
// watchdog are caught by the pthread_create hook below.
function followMainOnly() {
  const mainTid = Process.id;
  let found = false;
  Process.enumerateThreads().forEach(t => {
    if (t.id === mainTid) {
      followThread(t.id, 'main');
      found = true;
    }
  });
  if (!found) {
    // Process.id may not equal mainTid on some Android builds; fall
    // back to the lowest-numbered thread (typically the main one).
    const threads = Process.enumerateThreads().sort((a,b) => a.id - b.id);
    if (threads.length) followThread(threads[0].id, 'main-fallback');
  }
}

function unfollowAll() {
  for (const tid of followed) {
    try { Stalker.unfollow(tid); } catch (e) {}
  }
  followed.clear();
  A(`unfollowed all threads. stats: total=${stats.total_svc}, ` +
    `suppressed=${stats.suppressed}, by_nr=${JSON.stringify(stats.by_nr)}`);
}

// 1. Exclude system modules from Stalker first (so future follow()
//    calls don't trace into them).
excludeSystemModules();

// 2. Follow only the main thread initially.
followMainOnly();

// 2. Hook pthread_create — re-enumerate threads on each call so we
// catch the new one. Bionic's pthread_t is opaque so we can't read
// the new tid from the args easily; re-enumerate is reliable.
const pthreadCreate = findSym('pthread_create');
if (pthreadCreate) {
  Interceptor.attach(pthreadCreate, {
    onLeave(retval) {
      if (retval.toInt32() === 0) followAll('post-pthread_create');
    }
  });
  A('hooked pthread_create');
}

// 4. After 5 s drop Stalker — the shield's startup tamper-checks have
// all completed by then and Stalker overhead would otherwise stall the
// app's cert-fetch / RSA-sign work.
setTimeout(() => {
  A('5s elapsed — stopping Stalker for runtime efficiency');
  unfollowAll();
}, 5000);

// Expose a manual-control RPC so the host can adjust if needed.
rpc.exports = {
  unfollow: () => { unfollowAll(); return 'ok'; },
  followAll: () => { followAll('rpc'); return 'ok'; },
  stats: () => stats,
};

A(`stalker syscall guard armed (suppressing nr=[${[...SUPPRESSED_NR].join(',')}])`);
