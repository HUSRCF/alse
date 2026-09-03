"""Does the STREAM path honour a 52-bit mask on gfx90a?

xarch.sh measured the cross-architecture negative at 52+52 through
hipExtStreamCreateWithCUMask, not through ROC_GLOBAL_CU_MASK. The env
path rounds 52 up to 64. If the stream path does the same, that
measurement was taken on masks wider than requested -- and the reduced
contract warns exactly this way round: a runtime that accepts the call
and quietly hands over more of the device produces an unusually LOW
co-run penalty, which reads as good news.
"""
import ctypes, json

hip = ctypes.CDLL("libamdhip64.so")
hip.hipExtStreamCreateWithCUMask.restype = ctypes.c_int
hip.hipExtStreamCreateWithCUMask.argtypes = [
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32)]
hip.hipExtStreamGetCUMask.restype = ctypes.c_int
hip.hipExtStreamGetCUMask.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]


def make_and_read(mask_int, words=4):
    src = (ctypes.c_uint32 * words)()
    for i in range(words):
        src[i] = (mask_int >> (32 * i)) & 0xFFFFFFFF
    handle = ctypes.c_void_p()
    rc = hip.hipExtStreamCreateWithCUMask(ctypes.byref(handle), words, src)
    if rc != 0:
        return {"create_rc": rc}
    buf = (ctypes.c_uint32 * words)()
    rc2 = hip.hipExtStreamGetCUMask(handle, words, buf)
    v = 0
    for i, w in enumerate(buf):
        v |= w << (32 * i)
    return {"create_rc": 0, "read_rc": rc2, "mask": hex(v),
            "popcount": bin(v).count("1")}


import torch  # noqa: E402  -- after ctypes, so the context exists
torch.cuda.init()
print(json.dumps({"device": torch.cuda.get_device_properties(0).name,
                  "mpc": torch.cuda.get_device_properties(0).multi_processor_count}))
for units in (8, 16, 26, 32, 52, 64, 78, 104):
    low = (1 << units) - 1
    high = ((1 << units) - 1) << (104 - units)
    r_low = make_and_read(low)
    r_high = make_and_read(high)
    print(json.dumps({
        "units": units,
        "low_half": {**r_low, "matches": r_low.get("popcount") == units},
        "high_half": {**r_high, "matches": r_high.get("popcount") == units},
    }))
