# The Gate-A attestation pin cannot match, and the build is not why

Written 2026-08-25. The decision this asks for is whether to change what
`approved_build_attestation_sha256` covers. Nothing here has been
changed: the manifest, the pin and the acceptance are as they were.

## What happened

Running `python3 -m burstserve.smctrl_runner build` produced a
`build-attestation.json` whose SHA-256 differs from the manifest's
`approved_build_attestation_sha256`, so `run` refuses to start -- and
`tests/test_smctrl_runner.py::test_default_manifest_is_artifact_pinned_but_still_unpromoted`
now fails. The refusal is the gate working. The failing test is a
consequence of the rebuild, not of any source change.

**The copy of `build-attestation.json` that matched the pin is gone.**
`build/` is in `.gitignore`, so it was not recoverable, and the rebuild
overwrote it. That is a real loss and it was mine. It does not lose
evidence for any claim -- the attestation is a build output, not a
measurement -- but the rule is to preserve artifacts and moving the
directory aside first was the right move.

## What reproduces, and what does not

Every artifact the manifest pins reproduces bit for bit:

| file | pin | result |
| --- | --- | --- |
| `smid_probe` | `approved_launcher_sha256` | matches |
| `smid_probe.real` | `approved_real_probe_sha256` | matches |
| `build-config.stamp` | `approved_build_stamp_sha256` | matches |
| `build-attestation.json` | `approved_build_attestation_sha256` | **differs** |

Two consecutive builds, minutes apart, differ in exactly eight fields of
the attestation and in nothing else:

    build_stamp.metadata.inode
    outputs.guard_exec_test_fixture.metadata.inode
    outputs.guard_exec_test_identity_header.metadata.inode
    outputs.guard_exec_test_launcher.metadata.inode
    outputs.launcher.metadata.inode
    outputs.parent_guard_test_helper.metadata.inode
    outputs.real_probe.metadata.inode
    outputs.real_probe_identity_header.metadata.inode

Every content hash, size, mode, device, source hash and toolchain entry
is identical. The compiled binaries are byte-identical across builds.

**An earlier reading of this was wrong and is corrected here.** The
first comparison put `approved_launcher_sha256` against
`guard_exec_test_launcher.real`, concluded a test fixture was
irreproducible, and reported that. The pin refers to `smid_probe`, which
matches. There is no irreproducible binary.

## What this means

The build is reproducible. The attestation document is not, because it
records inode numbers, and an inode changes every time a file is
written. A hash over that document therefore cannot serve as a build
identity across rebuilds -- not because of drift or a toolchain change,
but by construction. The pin has never been able to do its job, and the
only reason this did not surface earlier is that nothing had rebuilt
since the manifest was written on 2026-08-06.

The instruction was to try (a) -- make the build reproducible -- and to
consider redefining the pin only if (a) proved infeasible, with reasons
written down. (a) is complete, and its answer is that there was never a
build defect to fix. These are the reasons.

## What the inode is used for

Checked before proposing to move it. The attestation's inode fields are
validated for shape when a record is read -- the value must be an `int`
-- and are not compared against anything live. The run-time identity
check that does use an inode,
`opened_launcher_fd_matches_attested_output`, compares the opened file
descriptor's inode against a **freshly stat'd** value, not against the
attested document; only the SHA-256 is compared against the attestation.
Removing the inode from the pinned document therefore weakens no
run-time check.

## Options

1. **Stop recording inodes in the attestation.** Simplest. The document
   becomes reproducible and its hash becomes a usable pin. Loses the
   record of which file object a given build produced.
2. **Pin a canonical hash that excludes the inode fields.** Keeps the
   information in the document and makes the pin reproducible. Changes
   the manifest schema and what the pin means, which is a change to a
   safety gate.
3. **Move the inodes into a sibling record that is not pinned** -- a
   build-environment file beside the attestation. Keeps the provenance,
   keeps the pin's meaning, and makes the pinned document reproducible.

All three change the pinned value once, which is why none has been done.
Recommendation is **3**: it is the only one that neither discards
provenance nor redefines what the pin covers.

## What has not been done

The pin has not been changed. The manifest has not been edited. No
acceptance has been weakened. `run` still refuses, and the test still
fails, which is the correct state until this is decided.
