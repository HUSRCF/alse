"""What CU masks does this device actually honour?

No model, no pipeline: set ROC_GLOBAL_CU_MASK, take a stream, read the
mask back with hipExtStreamGetCUMask. Seconds per point instead of
minutes, because the question is about the runtime and not the workload.
"""
import ctypes, json, os, sys

def read_cu_mask(stream_ptr, words=4):
    hip = ctypes.CDLL("libamdhip64.so")
    fn = hip.hipExtStreamGetCUMask
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                   ctypes.POINTER(ctypes.c_uint32)]
    buf = (ctypes.c_uint32 * words)()
    rc = fn(ctypes.c_void_p(stream_ptr), words, buf)
    if rc != 0:
        return {"rc": rc, "mask": None}
    v = 0
    for i, w in enumerate(buf):
        v |= w << (32 * i)
    return {"rc": 0, "mask": hex(v), "popcount": bin(v).count("1")}

def main():
    import torch
    dev = torch.cuda.current_device()
    props = torch.cuda.get_device_properties(dev)
    s = torch.cuda.Stream()
    rb = read_cu_mask(s.cuda_stream)
    req = os.environ.get("ROC_GLOBAL_CU_MASK")
    want = bin(int(req, 16)).count("1") if req else None
    print(json.dumps({
        "requested": req, "requested_units": want,
        "readback": rb,
        "matches": (rb.get("popcount") == want) if want else None,
        "multi_processor_count": props.multi_processor_count,
        "name": props.name, "arch": getattr(props, "gcnArchName", None),
    }))

main()
