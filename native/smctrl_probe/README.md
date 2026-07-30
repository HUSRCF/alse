# Native SM-ID probe

This is the fail-closed native probe for Gate A. It launches a compute-heavy
CUDA kernel, reads `%smid` once per block, and writes one JSON record to
standard output. Generated files are kept under `build/smctrl_probe`.

Build from the repository root:

```bash
make -C native/smctrl_probe
```

The defaults are `CUDA_HOME=/usr/local/cuda-13.3` and `CUDA_ARCH=89`. Both can
be overridden:

```bash
make -C native/smctrl_probe CUDA_HOME=/opt/cuda CUDA_ARCH=90
```

The only safe first run on a driver newer than libsmctrl's validated table is
the unmasked baseline:

```bash
build/smctrl_probe/smid_probe \
  --mode baseline \
  --iterations 4096
```

Masked modes select one enabled TPC bit:

```bash
build/smctrl_probe/smid_probe --mode global --enabled-tpc 0
build/smctrl_probe/smid_probe --mode next --enabled-tpc 0
build/smctrl_probe/smid_probe --mode stream --enabled-tpc 0
```

The vendored upstream currently recognizes CUDA driver API versions only
through 12.8. On CUDA 13 or any other unknown version, every masked mode exits
without calling libsmctrl unless `--allow-unsupported-driver` is explicitly
present. Experimental `stream` mode additionally requires an explicit,
integer-valued `MASK_OFF` environment variable because it writes an
undocumented CUDA stream-structure offset:

```bash
MASK_OFF=0 build/smctrl_probe/smid_probe \
  --mode stream \
  --enabled-tpc 0 \
  --allow-unsupported-driver
```

That command is an offset-discovery experiment, not a supported configuration.
It may crash or hang when the offset is wrong and should be executed only
under the higher-level runner's timeout and GPU isolation. Global and next
modes also require the experimental flag on CUDA 13 because their QMD callback
layout has not been validated there.

The schema is `burstserve.smid-probe-native/v1`. Key fields include the driver
and runtime API versions, the `GPU-...` device UUID reported by CUDA, device
metadata, requested enabled TPC bit, and the observed `SM ID -> block count`
histogram.

On a validated driver, `stream` mode rejects any inherited `MASK_OFF` rather
than letting it silently override the pinned offset. Unset `MASK_OFF` for a
normal supported run. CUDA 8.0 stream masking is also intentionally treated as
unsupported because the pinned upstream implementation falls through to its
CUDA 9.0 offset.

The build configuration is recorded in
`build/smctrl_probe/build-config.stamp`. Changing `CUDA_HOME`, `CUDA_ARCH`,
compiler commands, or compile/link flags causes the native objects to rebuild.
The `clean` target refuses to recursively remove a `BUILD_DIR` outside the
repository's dedicated `build/` subtree.

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
