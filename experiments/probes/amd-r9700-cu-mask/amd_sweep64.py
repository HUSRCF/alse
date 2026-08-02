import json, subprocess
rows = {}
for bit in range(64):
    r = subprocess.run(["./cu_probe", "--mode", "cu_mask", "--enabled-cu", str(bit),
                        "--blocks", "256", "--iterations", "256"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        rows[bit] = {"error": r.stdout.strip()[:120]}
        continue
    d = json.loads([l for l in r.stdout.splitlines() if l.startswith("{")][-1])
    rows[bit] = {"ids": sorted(int(k) for k in d["observed_histogram"]),
                 "readback_matches": d["readback_matches_request"]}
ok = {b: v for b, v in rows.items() if "ids" in v}
print("cells ok:", len(ok), "/ 64")
print("distinct ids per bit :", sorted({len(v["ids"]) for v in ok.values()}))
print("readback always match:", all(v["readback_matches"] for v in ok.values()))
union = sorted({i for v in ok.values() for i in v["ids"]})
print("union of ids         :", len(union))
pairs = [(b, b + 32) for b in range(32)
         if b in ok and b + 32 in ok and ok[b]["ids"] == ok[b + 32]["ids"]]
print("bits N and N+32 identical:", len(pairs), "/ 32")
groups = {}
for b, v in ok.items():
    groups.setdefault(tuple(v["ids"]), []).append(b)
sizes = sorted({len(g) for g in groups.values()})
print("distinct id groups   :", len(groups), "| bits per group:", sizes)
for b in (0, 1, 32, 33):
    if b in ok:
        print("  bit %2d -> %s" % (b, ok[b].get("ids", ok[b])))
json.dump(rows, open("sweep64.json", "w"), indent=2)
