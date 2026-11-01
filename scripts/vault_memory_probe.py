#!/usr/bin/env python3
"""Memory of the vault's key derivation in an ISOLATED process: a throwaway workspace and a made-up passphrase, nothing of the real
service or its data is touched (MCA-VAULT PR 6, docs/MCAttach_Vault.md, "Memory and swap").

Prints the resident (RSS), proportional (PSS) and anonymous memory, the peak and MemAvailable after each step: the header
creation (what an enable does once), repeated unlocks on the main thread and on new threads (what request threads do), and with
`trim` the effect of gc + malloc_trim. A derivation needs the whole memory limit for about a second and frees it; resident
memory should be back at the baseline after every step.

Usage (from a checkout, with the project's Python):  python scripts/vault_memory_probe.py [memlimit_mib] [ops] [rounds] [trim]
Defaults: 64 MiB, 3 passes, 5 rounds."""
import ctypes
import gc
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meshsrv.attachments.vault import header, kdf, passphrase  # noqa: E402
from meshsrv.attachments.vault.runtime import VaultRuntime  # noqa: E402

if not os.path.exists('/proc/self/status'):
    sys.exit('This probe reads /proc: run it on Linux (on the node).')

MEM = int(sys.argv[1]) if len(sys.argv) > 1 else 64
OPS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 5
TRIM = (sys.argv[4] == 'trim') if len(sys.argv) > 4 else False
PASS = 'correct horse battery staple'


def kb(field, path='/proc/self/status'):
    with open(path) as handle:
        for line in handle:
            if line.startswith(field + ':'):
                return int(line.split()[1])
    return 0


def snap(label):
    rss, hwm = kb('VmRSS'), kb('VmHWM')
    pss = kb('Pss', '/proc/self/smaps_rollup')
    anon = kb('Anonymous', '/proc/self/smaps_rollup')
    avail = kb('MemAvailable', '/proc/meminfo')
    print('%-34s RSS %6.1f MB  PSS %6.1f  anon %6.1f  peak %6.1f | MemAvailable %6.1f MB' % (label, rss / 1024, pss / 1024, anon / 1024, hwm / 1024, avail / 1024))


def trim():
    try:
        return ctypes.CDLL('libc.so.6').malloc_trim(0)
    except OSError:
        return -1


snap('baseline (imports)')
work = tempfile.mkdtemp(prefix='vaultmem')
params = kdf.KdfParams(OPS, MEM * 1024 * 1024)
t0 = time.time()
created = header.create(passphrase.normalize(PASS), params)
with open(os.path.join(work, 'vault.json'), 'w', encoding='utf-8') as handle:
    handle.write(created.header.to_json())
print('   (create: %.2f s with %d MiB, %d ops)' % (time.time() - t0, MEM, OPS))
snap('after header.create (enable)')
if TRIM:
    trim(); snap('  + malloc_trim')

runtime = VaultRuntime(work, free_ram=lambda: 4 * 1024 ** 3)
for i in range(ROUNDS):
    t0 = time.time()
    runtime.unlock(passphrase=PASS)
    dt = time.time() - t0
    snap('unlock %d (%.2f s) main thread' % (i + 1, dt))
    runtime.lock()
gc.collect()
snap('after lock + gc.collect')
if TRIM:
    print('   malloc_trim ->', trim())
    snap('after malloc_trim(0)')


def in_thread():
    for i in range(ROUNDS):
        runtime.unlock(passphrase=PASS)
        runtime.lock()


for n in range(3):
    th = threading.Thread(target=in_thread)
    th.start()
    th.join()
    snap('unlock x%d in a new thread #%d' % (ROUNDS, n + 1))
gc.collect()
snap('after threads + gc')
if TRIM:
    print('   malloc_trim ->', trim())
    snap('after malloc_trim(0)')
