import json, subprocess
rows, bad = {}, []
for bit in range(32):
    r = subprocess.run(["./cu_probe", "--mode", "cu_mask", "--enabled-cu", str(bit),
                        "--blocks", "256", "--iterations", "256"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        bad.append((bit, r.returncode, r.stderr.strip()[:80]))
        continue
    d = json.loads([l for l in r.stdout.splitlines() if l.startswith("{")][-1])
    rows[bit] = {"ids": sorted(int(k) for k in d["observed_histogram"]),
                 "readback_ok": d["readback_supported"],
                 "readback_matches": d["readback_matches_request"]}
print("cells run:", len(rows), "failures:", bad)
print("readback supported everywhere :", all(v["readback_ok"] for v in rows.values()))
print("readback matches request       :", all(v["readback_matches"] for v in rows.values()))
print("distinct ids per single-bit mask:", sorted({len(v["ids"]) for v in rows.values()}))
allids = sorted({i for v in rows.values() for i in v["ids"]})
print("union of all ids               :", len(allids))
overlap = [(a, b) for a in rows for b in rows if a < b
           and set(rows[a]["ids"]) & set(rows[b]["ids"])]
print("bit pairs sharing an id        :", len(overlap))
for b in (0, 1, 15, 16, 31):
    if b in rows:
        print("  bit %2d -> %s" % (b, rows[b]["ids"]))
json.dump(rows, open("sweep32.json", "w"), indent=2)
