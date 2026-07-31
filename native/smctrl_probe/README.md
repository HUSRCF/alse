# Native SM-ID probe

This is the fail-closed native probe for Gate A. It launches a compute-heavy
CUDA kernel, reads `%smid` once per block, and writes one JSON record to
standard output. Generated files are kept under `build/smctrl_probe`.

Build from the repository root:

```bash
make -C native/smctrl_probe
```

The formal build pins `CUDA_HOME=/usr/local/cuda-13.3` and `CUDA_ARCH=89`.
They are evidence-bearing trust roots, not command-line customization points.
Changing either requires a reviewed Makefile change and produces a new source,
stamp, attestation, and binary identity.

The only safe first run on a driver newer than libsmctrl's validated table is
the unmasked baseline:

```bash
build/smctrl_probe/smid_probe \
  --mode baseline \
  --iterations 4096
```

Masked modes select one enabled TPC bit. They may only be launched by the
higher-level Gate-A runner after its checked-in promotion manifest explicitly
authorizes the exact mode, GPU UUID, binary/build hashes, and reservation
evidence. Do not invoke a masked native mode directly: parent-death protection
is a lifecycle backstop, not authorization, and a bare invocation omits the
Xid monitor, occupancy checks, timeout, health checks, and retained
provenance.

The runner executes the static `smid_probe` launcher. Before loading the
dynamically linked CUDA probe, the launcher parses the expected PID, installs
Linux `PR_SET_PDEATHSIG=SIGKILL`, immediately checks that `getppid()` still
matches, restores default/unblocked `SIGHUP`, `SIGINT`, and `SIGTERM`
lifecycle handling, and removes every `LD_*` variable plus the enumerated
glibc and CUDA injection variables. Business variables such as
`CUDA_VISIBLE_DEVICES`, the MPS bypass, `MASK_OFF`, and the expected parent PID
are preserved.

The launcher opens `smid_probe.real` with `O_NOFOLLOW|O_NONBLOCK`, checks its strict
owner/mode/link metadata, and copies no more than the embedded expected size
plus one byte into an executable memfd. It sets mode `0500`, applies and
verifies the write/grow/shrink/seal seals and `F_SEAL_EXEC` when supported,
then re-checks size, SHA-256, and ELF identity from the immutable memfd. It
closes unrelated inherited descriptors and executes only that memfd through
the `execveat` syscall with `AT_EMPTY_PATH`. There is no pathname execution
fallback and no automatic retry without `MFD_EXEC`; unsupported or denied
kernel functionality fails closed before CUDA loads. The real probe verifies
that `SIGKILL` survived `exec`, re-arms it, and checks the parent again before
any explicit CUDA or libsmctrl call. Missing, invalid, or changed parent
identity and any guard/snapshot/seal/exec failure all reject the launch. The
unmasked baseline does not arm the parent guard but uses the same signal,
environment, immutable-snapshot, and descriptor controls.

The vendored upstream currently recognizes CUDA driver API versions only
through 12.8. On CUDA 13 or any other unknown version, every masked mode exits
without calling libsmctrl unless the runner supplies an explicitly promoted
experimental override. Experimental `stream` mode additionally requires a
predeclared integer-valued `MASK_OFF` because it writes an undocumented CUDA
stream-structure offset. An invalid value may corrupt the process, hang the
target GPU context, or produce an Xid, so no direct offset-discovery command is
documented here. Global and next
modes also require the experimental flag on CUDA 13 because their QMD callback
layout has not been validated there.

The schema is `burstserve.smid-probe-native/v2`. Key fields include the driver
and runtime API versions, the `GPU-...` device UUID reported by CUDA, device
metadata, requested enabled TPC bit, and the observed `SM ID -> block count`
histogram. The `parent_guard` object records its mode and status, expected and
observed parent PIDs, and the inherited and re-installed death signals.
Successful masked output must report `mode=linux_pdeathsig_sigkill`,
`status=armed`, matching positive PIDs, and both death signals as `9`; baseline
output reports `mode=not_required` and `status=not_required`.

On a validated driver, `stream` mode rejects any inherited `MASK_OFF` rather
than letting it silently override the pinned offset. Unset `MASK_OFF` for a
normal supported run. CUDA 8.0 stream masking is also intentionally treated as
unsupported because the pinned upstream implementation falls through to its
CUDA 9.0 offset.

The build configuration is recorded in
`build/smctrl_probe/build-config.stamp`, and the post-link,
machine-readable relation between inputs and ELF outputs is recorded in
`build/smctrl_probe/build-attestation.json`. Formal build and runner
coordination use the private lock
`/run/user/<euid>/burstserve-smctrl-probe/build.lock` with directory mode
`0700`, file mode `0600`, and an inherited locked descriptor checked by
internal Make targets. Build commands run with a small `env -i` allowlist,
absolute tools, and explicit nvcc `-ccbin`. Every attestation subprocess runs
in its own session; on both the success and the failure path its whole process
group is signalled and then boundedly verified to hold no runnable member, so
a descendant that closes the inherited pipes and outlives its parent cannot be
left running. The scan reads `/proc/<pid>/stat` as bytes, decodes only the
fields after the final `)`, and counts any record it cannot classify as a
possibly live member, so a process name is never able to hide a survivor. A
`Z` record is skipped only after two consecutive `/proc/<pid>/task` snapshots
observe every thread dead, because the kernel reports a thread group as `Z`
from the moment its leader thread exits while its other threads keep running.
The scan is deliberately not claimed to be a standalone proof: listing a task
directory is not atomic with reading each thread's state. The guarantee comes
from the ordering — `SIGKILL` reaches the whole group first, after which the
kernel refuses to complete a fork or clone into it, and only then is the group
scanned. A descendant that deliberately calls `setsid()` or `setpgid()` leaves
the group and is outside this guarantee; every command launched here is a
pinned root-owned build tool that does not detach. Every object/archive/ELF is written
to a same-directory temporary, permissioned, and atomically renamed; final
ELFs are owner-only `0500`. Dirty libsmctrl, unexpected owner/mode/link count,
set-id bits, or `security.capability` fail attestation.

Every path, tool, flag, lock descriptor, recursive-build command, and
attestation location that contributes to this contract is assigned with GNU
make's `override` directive. Recursive entry uses a literal `/usr/bin/make`
under `env -i`, so command-line assignments such as `MAKE=/usr/bin/true`,
`PYTHON=/usr/bin/true`, or redirected build/attestation paths cannot turn a
formal Gate into a no-op. Parallelism is only a performance choice; it does not
change these trust roots.

Before the first `$(shell ...)` expansion or recipe, the pinned Makefile uses
`unexport $(.VARIABLES)` to remove the export status of every variable imported
by GNU make. This is intentionally stronger than bare `unexport`, which does
not remove the individually exported status of variables inherited from the
process environment. Consequently `BASH_ENV`, `ENV`, exported Bash functions,
shell-option records, and dynamic-loader variables cannot run code in the
parse guard or the outer lock-acquisition shell. Child environments that the
build needs are reconstructed only by the explicit `env -i` allowlists.

GNU make dry-run/recon, touch, ignore-errors, and question modes are rejected
while parsing, including their long spellings and values inherited through
`MAKEFLAGS`. Command-line reassignment of `MAKEFLAGS` is also rejected so an
active control mode cannot be hidden with `MAKEFLAGS=`. These checks run before
any recipe and therefore also protect direct internal-target invocations.

A second parse guard runs `/usr/bin/python3 -I -S -B` under `env -i`; every
attestation create/finalize/verify command and the pinned native test file uses
the same isolated, no-`site`, no-bytecode interpreter mode. `-B` is explicit
because Python isolated mode also enables `-E` and therefore ignores an
ambient `PYTHONDONTWRITEBYTECODE`. This excludes system
`site-packages`, `.pth` processing, `sitecustomize`, and `usercustomize` before
the attested code starts. The guard locates the actual pinned `/usr/bin/make`
ancestor through `/proc` and checks its original argv, environment, working
directory, and makefile inputs. It rejects
`-E`/`--eval`, extra or alternate `-f` inputs, preloaded `MAKEFILES`, and
`GNUMAKEFLAGS`, `MAKEFLAGS`, `MAKEOVERRIDES`, `MFLAGS`, or `MAKEFILE_LIST`
injection. It also rejects raw `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASHOPTS`,
every `BASH_FUNC_*` record, every `LD_*` record, `GLIBC_TUNABLES`,
`GCONV_PATH`, `LOCPATH`, and `NLSPATH`. The loaded `MAKEFILE_LIST` must contain
exactly the pinned Makefile. A guard failure is converted directly to a
parse-time `$(error ...)`, so an injected `.IGNORE`, replacement recipe, or
malicious prerequisite cannot turn the failure into a successful Gate or
execute a Makefile recipe.

The public `gate-required-check` keeps the outer lock shell alive across the
recursive Gate and prints the exact final line
`burstserve-native-gate-required-check: verified` only after that recursive
Gate succeeds. Formal callers require exactly one such final sentinel and then
independently parse and verify the resulting canonical attestation; process
exit status or empty output alone is never accepted as proof of completion.

There is an earlier boundary that a Makefile cannot self-attest: variables such
as `LD_PRELOAD`, `LD_AUDIT`, `GLIBC_TUNABLES`, or `GCONV_PATH` may affect the
dynamic loader before `/usr/bin/make` reaches its first instruction. The raw
environment guard detects their presence only after that point; it does not
prove that a pre-main hook was harmless. Therefore the formal runner must
launch the initial pinned `/usr/bin/make` itself from a minimal `env -i`
allowlist. Likewise, an external makefile loaded before this pinned Makefile
can already perform parse-time side effects; such an invocation is rejected
but is outside the formal clean-entry contract.

`--eval`/`-E` has the same shape and is explicitly outside the clean-entry
contract. GNU make expands every `--eval` argument during its own start-up, so
that text — including `$(shell ...)` calls and `.IGNORE` — has already run by
the time the pinned Makefile is parsed and the guard inspects the real argv
through `/proc`. Detecting `--eval`/`-E` therefore only converts an already
executed injection into a hard parse error and prevents it from reaching a
recipe or a Gate result; it does not prevent the pre-guard expansion itself.
Only the formal runner originating the fixed argv makes that impossible, which
is why the runner, and not any convenience wrapper, must construct the exact
`/usr/bin/make` command line.

The parse guard is an immediate GNU make expansion, so it runs after
`/usr/bin/make` and its dynamic runtime have entered `main`, but before any
formal recipe. The formal build therefore treats the root-owned
`/usr/bin/make`, `/bin/bash`, `/usr/bin/env`, `/usr/bin/python3`, that
interpreter's standard library, the host dynamic loader and libc, and the
absolute root-owned utilities named by the Makefile as trusted computing-base
inputs. It also relies on Linux procfs semantics for bounded reads of the make
ancestor's `/proc/<pid>/{cmdline,environ,stat}`, its `cwd` link, and
`/proc/self/fd`. The attestation code bounds these reads and rejects malformed
records, but each record is a single snapshot: it is not re-read for A/B
stability, and a process can rewrite its own argv and environ area, so the
guard is not claimed to detect a record that changes after it was read. Nor is
parse-guard success ever treated as proof that a compromised pre-main runtime
was benign. Content-addressing or isolating this root-owned TCB is an external
deployment responsibility.

The attested finite contract binds the listed native sources, libsmctrl bytes
and Git status, CUDA include/libdevice trees, the recorded nvcc subtools,
host `cc1`/assembler/linker/CRT components, compiler version/search/dry-run
fingerprints, explicit flags/environment, link/runtime libraries, and final
output hashes/metadata/xattrs. It is deliberately not described as hermetic:
system headers chosen through the recorded search path, compiler/CUDA
`dlopen` dependencies, and live kernel/firmware/driver state are not all
content-addressed. Runtime dynamic dependencies are recorded at Gate time but
are not copied into the sealed memfd and could be replaced later by a
same-privilege actor. Build-orchestration utilities and individual system
static archives are likewise represented only indirectly by the recorded
contract and final output hash, not as a fully enumerated hermetic closure.
The verifier accepts only the exact canonical JSON byte encoding and rejects
duplicate keys, extra whitespace, and equivalent alternative encodings. It
compares the decoded document by exact JSON type as well as by canonical bytes,
so a substituted `true`/`1`/`1.0` is rejected instead of being accepted through
Python's numeric equality. Depth and node budgets are enforced by a
string- and escape-aware lexical pass before the object graph is built, so an
oversized flat document is rejected without first being allocated. The
advisory lock does not exclude unrelated same-UID writers, and the canonical
JSON attestation is not externally signed.

The static launcher also cannot protect the very small interval from child
creation to its first guard instruction. Mutation while the disk ELF is being
copied either causes the sealed snapshot's size/hash check to reject or, if the
copy already completed, leaves execution on the previously sealed bytes; it
does not reopen the pathname for execution. `make gate-required-check`
rebuilds and verifies all required artifacts and runs CPU-only fixtures plus a
CUDA-linked probe rejection that returns before its first explicit CUDA call,
always with `CUDA_VISIBLE_DEVICES=""`. The test module itself is included in
the source stamp and attestation closure. Unit tests run under `env -i` with
user-site imports disabled and bytecode writes suppressed. The `clean` target
removes only the explicit native artifact list and refuses unsafe build roots;
it never recursively deletes a directory.

Exit codes are semantic and stable:

| Code | Meaning |
| ---: | --- |
| 0 | Probe completed and produced a non-empty observation |
| 2 | Command-line usage error |
| 3 | Masked mode rejected by the fail-closed driver guard |
| 4 | Invalid or unsafe configuration |
| 5 | CUDA API or kernel failure |
| 6 | GPU/TPC topology could not be determined |
| 7 | Masked observation used more SMs than one TPC can contain |

Diagnostics go to standard error. Machine-readable output is a single JSON
line on standard output.
