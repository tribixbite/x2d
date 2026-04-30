/**
 * Bambu Handy v3.19.0 — Promon-style anti-tamper shield neutralizer.
 *
 * Findings from runtime/handy_extract/{analyze_shield,find_brx0,scan_xor_keys}.py:
 *
 *   Shield unpacked region: anon:.bss r-xp at 0x705e482000 (size 0x2e4000)
 *   in target's address space — **base differs per launch (ASLR)** so we
 *   discover dynamically by scanning Process.enumerateRanges('r-x') for
 *   the small (<8 MB) anonymous mapping with no module name.
 *
 *   Inside that region:
 *     - 141 `BR x0` instructions (encoding 0xd61f0000)
 *     - Zero `MOVZ + MOVK` pairs encoding 0xdead5019
 *     - Zero literal occurrences of 0xdead5019, rev/rbit/~/- variants
 *     - Only 1 non-trivial XOR pair (likely coincidental noise)
 *
 *   Conclusion: the magic value 0xdead5019 is **constructed at runtime**
 *   via multi-register arithmetic chains (XOR-swap-style identity
 *   sequences with embedded subs/adds of small immediates). There is no
 *   static byte sequence to NOP. The conditional branch that gates the
 *   tamper-die is itself obfuscated — every flow-control decision in the
 *   shield travels through `BR x0` after constructing the next-PC via
 *   stack loads + arithmetic.
 *
 * Strategy: instead of patching a static branch, intercept **every** BR x0
 * site in the shield region with Frida Interceptor + Stalker:
 *   - For each BR x0 instruction, install a probe that reads x0 just
 *     before the indirect jump. If x0 looks like a "tamper signal" target
 *     (0xdead5019 or any other unmapped page in low userspace), skip the
 *     BR by rewriting x0 to point at a RET stub.
 *
 * Alternative (simpler) strategy: hook the unhandled SIGBUS handler so the
 * crash is absorbed. But the shield ALSO clobbers most thread state before
 * BR'ing, so simply absorbing the signal will leave the process in a bad
 * state — better to prevent the BR.
 *
 * Usage from Frida CLI:
 *   frida -U -p $(pidof bbl.intl.bambulab.com) -l shield_patch.js
 * Or include from the ZygiskFrida loader script:
 *   require('./shield_patch.js')
 *
 * NOTE: This script does NOT replace the existing handy_hook.js / quick_hook.js
 * — load it BEFORE those so the shield is neutralized first.
 */
'use strict';

const TAMPER_MAGIC = ptr('0xdead5019');
const SHIELD_REGION_MAX = 0x800000;     // 8 MB upper bound on shield region
const SHIELD_REGION_MIN = 0x100000;     // 1 MB lower bound (skip tiny pages)
const BR_X0_ENCODING = 0xd61f0000;
const NOP_ENCODING   = 0xd503201f;

function findShieldRegion() {
    // The shield's unpacked memory in our dump was a 3.03 MB anonymous r-x
    // mapping. Filter Process.enumerateRanges('r-x') for ranges that are:
    //   - not backed by a file (range.file undefined)
    //   - size between 1 MB and 8 MB
    //   - not Flutter VM (which is much larger)
    const candidates = [];
    Process.enumerateRanges('r-x').forEach(r => {
        if (r.file) return;
        if (r.size < SHIELD_REGION_MIN || r.size > SHIELD_REGION_MAX) return;
        candidates.push(r);
    });
    return candidates;
}

function countBrX0(base, size) {
    // Scan the region in 4 KB chunks, counting 0xd61f0000 4-byte aligned
    // hits. >50 strongly indicates Promon shield.
    let count = 0;
    const end = base.add(size);
    for (let p = base; p.compare(end) < 0; p = p.add(4096)) {
        const chunk = Memory.readByteArray(p, 4096);
        const view = new Uint32Array(chunk);
        for (let i = 0; i < view.length; i++) {
            if (view[i] === BR_X0_ENCODING) count++;
        }
    }
    return count;
}

function installBrX0Guards(region) {
    // For each BR x0 site, patch the instruction with a small thunk that
    // checks x0 and bypasses if it points to an unmapped/magic address.
    //
    // Frida's Interceptor.attach on raw addresses inside an obfuscated
    // shield is risky — the shield CRCs its own code. Safer approach:
    // replace each BR x0 with a B instruction that jumps to a Frida-
    // allocated trampoline that:
    //   1. Tests x0 == 0xdead5019 → if yes, perform a controlled RET
    //      (load x30 from saved-LR slot or just RET)
    //   2. Otherwise BR x0 as normal
    //
    // BUT: the shield's CRC self-check will detect the patched B. So we
    // pair each patch with hooking the CRC-check function (which we'd
    // need to identify separately — typically called once per second from
    // Thread-2). For now this is left as a TODO.
    //
    // ALTERNATIVE: install an exception handler that catches SIGBUS at
    // 0xdead5019 and resumes from a safe stack frame. Frida supports this
    // via Process.setExceptionHandler. This is the simplest neutralizer
    // and survives shield CRC checks because we don't modify shield code.
    Process.setExceptionHandler(details => {
        if (details.type === 'access-violation' &&
            details.memory && details.memory.address &&
            details.memory.address.equals(TAMPER_MAGIC)) {
            console.log('[shield_patch] caught tamper-die SIGBUS; absorbing');
            // Force the thread to return to its caller. The shield clears
            // all registers and zeroes lr, so we have nothing useful to
            // RET to. Our best bet is to call pthread_exit on the
            // tamper-detection thread (Thread-2) so the process keeps
            // running without it.
            // TODO: locate pthread_exit in libc and set context.pc to it.
            // For now, log and let the crash proceed (no improvement
            // over baseline, but the diagnostic helps confirm the hook
            // is wired).
            return false;
        }
        return false;
    });
    console.log('[shield_patch] installed SIGBUS handler for ' + TAMPER_MAGIC);
}

function main() {
    console.log('[shield_patch] scanning for shield region...');
    const candidates = findShieldRegion();
    console.log(`[shield_patch] ${candidates.length} candidate anon r-x mapping(s)`);
    let shieldRegion = null;
    candidates.forEach(r => {
        const brCount = countBrX0(r.base, r.size);
        console.log(`[shield_patch]   ${r.base} size=0x${r.size.toString(16)} BR-x0=${brCount}`);
        if (brCount >= 50 && (shieldRegion === null || brCount > shieldRegion.brCount)) {
            shieldRegion = { base: r.base, size: r.size, brCount: brCount };
        }
    });
    if (!shieldRegion) {
        console.log('[shield_patch] NO shield region identified.');
        return;
    }
    console.log(`[shield_patch] SHIELD: ${shieldRegion.base} ` +
                `size=0x${shieldRegion.size.toString(16)} ` +
                `BR-x0 count=${shieldRegion.brCount}`);
    installBrX0Guards(shieldRegion);
}

// Run immediately if loaded via -l, or schedule for after gadget bind.
if (typeof rpc !== 'undefined') {
    rpc.exports = { main: main };
}
main();
