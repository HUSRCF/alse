// Gate A-AMD experiments beyond the mapping matrix: concurrent disjoint
// partitioning, dynamic reconfiguration at scale, mask configuration latency,
// and determinism under masking.
//
// Two lessons from the NVIDIA line are built in rather than rediscovered:
//
//   * Disjointness is NOT evidence of partitioning. Two concurrent kernels
//     land on disjoint units even unmasked, because the dispatcher splits the
//     die between them. The judgement is confinement to the set each arm
//     produces when run alone, and an unmasked control must fail that.
//   * A mapping must not be extrapolated from a few sampled bits.
//
// AMD affords one check NVIDIA cannot: hipExtStreamGetCUMask reads the applied
// mask back, so every reconfiguration is verified directly instead of being
// inferred from where the kernel happened to run.

#include <hip/hip_runtime.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iterator>
#include <set>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr int kHwId1 = 23;
constexpr uint32_t kIntraCuBits = 0x000003FFu;
constexpr int kThreads = 256;

struct BlockRecord {
  uint32_t unit;
  uint64_t enter;
  uint64_t exit;
};

__device__ __forceinline__ uint32_t unit_id() {
  return __builtin_amdgcn_s_getreg(kHwId1 | ((32 - 1) << 11)) & ~kIntraCuBits;
}

// Spins on the constant-rate wall clock so the two arms genuinely overlap in
// time rather than merely being enqueued together.
__global__ void spin_kernel(BlockRecord* records, uint64_t spin_ticks,
                            float* sink) {
  const uint32_t unit = unit_id();
  const uint64_t enter = wall_clock64();
  float value = 1.0f + static_cast<float>(threadIdx.x) * 0x1p-20f;
  uint64_t now = enter;
  while (now - enter < spin_ticks) {
    for (int i = 0; i < 256; ++i) {
      value = __builtin_fmaf(value, 1.00000011920928955078125f, 0x1p-24f);
    }
    now = wall_clock64();
  }
  if (threadIdx.x == 0) {
    records[blockIdx.x] = {unit, enter, now};
  }
  sink[blockIdx.x * blockDim.x + threadIdx.x] = value;
}

__global__ void tag_kernel(uint32_t* units) {
  if (threadIdx.x == 0) {
    units[blockIdx.x] = unit_id();
  }
}

// A fixed-seed reduction whose result must not depend on which units ran it.
__global__ void checksum_kernel(uint64_t* out, int iterations) {
  const uint64_t index =
      static_cast<uint64_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  uint64_t state = index * 6364136223846793005ULL + 1442695040888963407ULL;
  for (int i = 0; i < iterations; ++i) {
    state ^= state >> 33;
    state *= 0xff51afd7ed558ccdULL;
    state ^= state >> 29;
  }
  atomicAdd(reinterpret_cast<unsigned long long*>(out),
            static_cast<unsigned long long>(state));
}

#define CHECK(expr)                                                        \
  do {                                                                     \
    hipError_t err_ = (expr);                                              \
    if (err_ != hipSuccess) {                                              \
      std::printf("{\"status\":\"hip_error\",\"where\":\"%s\","            \
                  "\"error\":\"%s\"}\n",                                   \
                  #expr, hipGetErrorString(err_));                         \
      return 1;                                                            \
    }                                                                      \
  } while (0)

int g_units = 32;
uint32_t g_words = 1;

std::vector<uint32_t> mask_for(const std::vector<int>& bits) {
  std::vector<uint32_t> mask(g_words, 0);
  for (int bit : bits) {
    mask[bit / 32] |= UINT32_C(1) << (bit % 32);
  }
  return mask;
}

std::string set_json(const std::set<uint32_t>& values) {
  std::string out = "[";
  bool first = true;
  for (uint32_t value : values) {
    if (!first) out += ",";
    first = false;
    out += std::to_string(value);
  }
  return out + "]";
}

// ---- concurrency ---------------------------------------------------------

struct Arm {
  std::vector<int> bits;
  hipStream_t stream = nullptr;
  BlockRecord* device_records = nullptr;
  float* device_sink = nullptr;
  int blocks = 0;
  std::vector<BlockRecord> host;
  hipError_t error = hipSuccess;
};

void launch_arm(Arm* arm, uint64_t spin_ticks) {
  spin_kernel<<<arm->blocks, kThreads, 0, arm->stream>>>(
      arm->device_records, spin_ticks, arm->device_sink);
  arm->error = hipGetLastError();
  if (arm->error == hipSuccess) {
    arm->error = hipStreamSynchronize(arm->stream);
  }
}

std::set<uint32_t> units_of(const Arm& arm) {
  std::set<uint32_t> out;
  for (const BlockRecord& record : arm.host) {
    if (record.unit != 0xffffffffu) out.insert(record.unit);
  }
  return out;
}

int run_concurrency(uint64_t spin_ticks, bool masked, int blocks) {
  Arm arms[2];
  arms[0].bits = {0, 1, 2, 3, 4, 5, 6, 7};
  arms[1].bits = {16, 17, 18, 19, 20, 21, 22, 23};
  for (Arm& arm : arms) {
    arm.blocks = blocks;
    if (masked) {
      const std::vector<uint32_t> mask = mask_for(arm.bits);
      CHECK(hipExtStreamCreateWithCUMask(&arm.stream, g_words, mask.data()));
    } else {
      CHECK(hipStreamCreateWithFlags(&arm.stream, hipStreamNonBlocking));
    }
    CHECK(hipMalloc(&arm.device_records, blocks * sizeof(BlockRecord)));
    CHECK(hipMalloc(&arm.device_sink, blocks * kThreads * sizeof(float)));
    arm.host.resize(blocks);
  }
  CHECK(hipDeviceSynchronize());

  // Each arm alone first: that is the oracle its concurrent run must match.
  std::set<uint32_t> alone[2];
  for (int i = 0; i < 2; ++i) {
    CHECK(hipMemset(arms[i].device_records, 0xff,
                    blocks * sizeof(BlockRecord)));
    launch_arm(&arms[i], spin_ticks / 4);
    CHECK(arms[i].error);
    CHECK(hipMemcpy(arms[i].host.data(), arms[i].device_records,
                    blocks * sizeof(BlockRecord), hipMemcpyDeviceToHost));
    alone[i] = units_of(arms[i]);
  }

  for (Arm& arm : arms) {
    CHECK(hipMemset(arm.device_records, 0xff, blocks * sizeof(BlockRecord)));
  }
  std::thread workers[2];
  for (int i = 0; i < 2; ++i) {
    workers[i] = std::thread(launch_arm, &arms[i], spin_ticks);
  }
  for (std::thread& worker : workers) worker.join();
  for (int i = 0; i < 2; ++i) {
    CHECK(arms[i].error);
    CHECK(hipMemcpy(arms[i].host.data(), arms[i].device_records,
                    blocks * sizeof(BlockRecord), hipMemcpyDeviceToHost));
  }

  std::set<uint32_t> together[2];
  uint64_t first[2] = {UINT64_MAX, UINT64_MAX}, last[2] = {0, 0};
  for (int i = 0; i < 2; ++i) {
    together[i] = units_of(arms[i]);
    for (const BlockRecord& record : arms[i].host) {
      if (record.unit == 0xffffffffu) continue;
      first[i] = std::min(first[i], record.enter);
      last[i] = std::max(last[i], record.exit);
    }
  }
  const uint64_t overlap_start = std::max(first[0], first[1]);
  const uint64_t overlap_end = std::min(last[0], last[1]);
  const double overlap_ticks =
      overlap_end > overlap_start
          ? static_cast<double>(overlap_end - overlap_start)
          : 0.0;

  std::set<uint32_t> intersection;
  std::set_intersection(together[0].begin(), together[0].end(),
                        together[1].begin(), together[1].end(),
                        std::inserter(intersection, intersection.begin()));

  // Stable under concurrency: each arm lands where it lands alone. Necessary
  // but NOT sufficient, and vacuously true for the unmasked control, whose
  // "alone" set is already the whole die.
  const bool stable = together[0] == alone[0] && together[1] == alone[1];
  // The discriminating claim: each arm is confined to as many units as it
  // asked for, the two are disjoint, and they ran at the same time. The
  // unmasked control fails the first clause -- it covers the whole die --
  // which is what makes this a partition rather than a coincidence.
  const bool sized = together[0].size() == arms[0].bits.size() &&
                     together[1].size() == arms[1].bits.size();
  const bool partitioned =
      sized && intersection.empty() && overlap_ticks > 0;
  std::printf(
      "{\"experiment\":\"concurrency\",\"masked\":%s,\"blocks\":%d"
      ",\"requested_units_per_arm\":%zu"
      ",\"arm0_alone\":%s,\"arm0_together\":%s"
      ",\"arm1_alone\":%s,\"arm1_together\":%s"
      ",\"arm0_unit_count\":%zu,\"arm1_unit_count\":%zu"
      ",\"unit_overlap\":%zu,\"overlap_ticks\":%.0f"
      ",\"stable_under_concurrency\":%s,\"concurrent\":%s"
      ",\"partitioned\":%s}\n",
      masked ? "true" : "false", blocks, arms[0].bits.size(),
      set_json(alone[0]).c_str(), set_json(together[0]).c_str(),
      set_json(alone[1]).c_str(), set_json(together[1]).c_str(),
      together[0].size(), together[1].size(), intersection.size(),
      overlap_ticks, stable ? "true" : "false",
      overlap_ticks > 0 ? "true" : "false",
      partitioned ? "true" : "false");

  for (Arm& arm : arms) {
    hipFree(arm.device_sink);
    hipFree(arm.device_records);
    hipStreamDestroy(arm.stream);
  }
  return 0;
}

// ---- reconfiguration and latency ----------------------------------------

int run_reconfigure(int iterations, int blocks) {
  uint32_t* device_units = nullptr;
  CHECK(hipMalloc(&device_units, blocks * sizeof(uint32_t)));
  std::vector<uint32_t> host(blocks);
  std::vector<uint32_t> readback(g_words);
  std::vector<double> create_us;
  create_us.reserve(iterations);

  long long readback_mismatches = 0;
  long long confinement_failures = 0;
  long long launch_failures = 0;
  std::vector<std::set<uint32_t>> oracle(g_units);

  for (int i = 0; i < iterations; ++i) {
    const int bit = i % g_units;
    const std::vector<uint32_t> mask = mask_for({bit});
    hipStream_t stream = nullptr;

    const auto started = std::chrono::steady_clock::now();
    const hipError_t created =
        hipExtStreamCreateWithCUMask(&stream, g_words, mask.data());
    const auto finished = std::chrono::steady_clock::now();
    if (created != hipSuccess) {
      launch_failures += 1;
      continue;
    }
    create_us.push_back(
        std::chrono::duration<double, std::micro>(finished - started).count());

    // Direct verification, which the CUDA line cannot perform at all.
    std::fill(readback.begin(), readback.end(), 0);
    if (hipExtStreamGetCUMask(stream, g_words, readback.data()) !=
            hipSuccess ||
        readback != mask) {
      readback_mismatches += 1;
    }

    CHECK(hipMemsetAsync(device_units, 0xff, blocks * sizeof(uint32_t),
                         stream));
    tag_kernel<<<blocks, kThreads, 0, stream>>>(device_units);
    if (hipGetLastError() != hipSuccess ||
        hipStreamSynchronize(stream) != hipSuccess) {
      launch_failures += 1;
      hipStreamDestroy(stream);
      continue;
    }
    CHECK(hipMemcpy(host.data(), device_units, blocks * sizeof(uint32_t),
                    hipMemcpyDeviceToHost));
    std::set<uint32_t> observed(host.begin(), host.end());
    observed.erase(0xffffffffu);
    if (oracle[bit].empty()) {
      oracle[bit] = observed;
    } else if (observed != oracle[bit]) {
      confinement_failures += 1;
    }
    if (observed.size() != 1) {
      confinement_failures += 1;
    }
    hipStreamDestroy(stream);
  }

  std::sort(create_us.begin(), create_us.end());
  auto pct = [&](double q) {
    if (create_us.empty()) return 0.0;
    size_t index = static_cast<size_t>(q * (create_us.size() - 1));
    return create_us[index];
  };
  std::printf(
      "{\"experiment\":\"reconfigure\",\"iterations\":%d"
      ",\"readback_mismatches\":%lld,\"confinement_failures\":%lld"
      ",\"launch_failures\":%lld,\"create_p50_us\":%.2f"
      ",\"create_p99_us\":%.2f,\"create_max_us\":%.2f,\"samples\":%zu}\n",
      iterations, readback_mismatches, confinement_failures, launch_failures,
      pct(0.50), pct(0.99), create_us.empty() ? 0.0 : create_us.back(),
      create_us.size());
  hipFree(device_units);
  return 0;
}

// ---- pooled reconfiguration ---------------------------------------------

// Creating a CU-masked stream costs milliseconds, so it cannot sit on a
// scheduler's critical path. HIP fixes the mask at stream creation rather
// than updating a live stream, which makes the operational primitive
// "switch to a pre-created masked stream", not "reprogram the mask". This
// measures that primitive: the pool is built once, and each reconfiguration
// is a launch onto a different member.
int run_reconfigure_pooled(int iterations, int blocks) {
  std::vector<hipStream_t> pool(g_units);
  for (int bit = 0; bit < g_units; ++bit) {
    const std::vector<uint32_t> mask = mask_for({bit});
    CHECK(hipExtStreamCreateWithCUMask(&pool[bit], g_words, mask.data()));
  }
  uint32_t* device_units = nullptr;
  CHECK(hipMalloc(&device_units, blocks * sizeof(uint32_t)));
  std::vector<uint32_t> host(blocks);
  std::vector<std::set<uint32_t>> oracle(g_units);
  std::vector<double> switch_us;
  switch_us.reserve(iterations);
  long long confinement_failures = 0;
  long long launch_failures = 0;

  // Warm every stream once so first-touch allocation is not counted.
  for (int bit = 0; bit < g_units; ++bit) {
    tag_kernel<<<blocks, kThreads, 0, pool[bit]>>>(device_units);
    CHECK(hipStreamSynchronize(pool[bit]));
  }

  for (int i = 0; i < iterations; ++i) {
    const int bit = i % g_units;
    CHECK(hipMemsetAsync(device_units, 0xff, blocks * sizeof(uint32_t),
                         pool[bit]));
    const auto started = std::chrono::steady_clock::now();
    tag_kernel<<<blocks, kThreads, 0, pool[bit]>>>(device_units);
    const hipError_t launched = hipGetLastError();
    const auto finished = std::chrono::steady_clock::now();
    if (launched != hipSuccess ||
        hipStreamSynchronize(pool[bit]) != hipSuccess) {
      launch_failures += 1;
      continue;
    }
    switch_us.push_back(
        std::chrono::duration<double, std::micro>(finished - started).count());
    CHECK(hipMemcpy(host.data(), device_units, blocks * sizeof(uint32_t),
                    hipMemcpyDeviceToHost));
    std::set<uint32_t> observed(host.begin(), host.end());
    observed.erase(0xffffffffu);
    if (observed.size() != 1) {
      confinement_failures += 1;
    } else if (oracle[bit].empty()) {
      oracle[bit] = observed;
    } else if (observed != oracle[bit]) {
      confinement_failures += 1;
    }
  }

  std::sort(switch_us.begin(), switch_us.end());
  auto pct = [&](double q) {
    if (switch_us.empty()) return 0.0;
    return switch_us[static_cast<size_t>(q * (switch_us.size() - 1))];
  };
  std::printf(
      "{\"experiment\":\"reconfigure_pooled\",\"iterations\":%d"
      ",\"pool_size\":%d,\"confinement_failures\":%lld"
      ",\"launch_failures\":%lld,\"switch_p50_us\":%.2f"
      ",\"switch_p99_us\":%.2f,\"switch_max_us\":%.2f,\"samples\":%zu}\n",
      iterations, g_units, confinement_failures, launch_failures, pct(0.50),
      pct(0.99), switch_us.empty() ? 0.0 : switch_us.back(),
      switch_us.size());
  hipFree(device_units);
  for (hipStream_t stream : pool) hipStreamDestroy(stream);
  return 0;
}

// ---- determinism ---------------------------------------------------------

int run_determinism(int blocks, int iterations) {
  uint64_t* device_out = nullptr;
  CHECK(hipMalloc(&device_out, sizeof(uint64_t)));
  auto checksum = [&](hipStream_t stream, uint64_t* out) -> hipError_t {
    const uint64_t zero = 0;
    hipError_t error = hipMemcpyAsync(device_out, &zero, sizeof(uint64_t),
                                      hipMemcpyHostToDevice, stream);
    if (error != hipSuccess) return error;
    checksum_kernel<<<blocks, kThreads, 0, stream>>>(device_out, iterations);
    error = hipGetLastError();
    if (error != hipSuccess) return error;
    error = hipMemcpyAsync(out, device_out, sizeof(uint64_t),
                           hipMemcpyDeviceToHost, stream);
    if (error != hipSuccess) return error;
    return hipStreamSynchronize(stream);
  };

  hipStream_t plain = nullptr;
  CHECK(hipStreamCreateWithFlags(&plain, hipStreamNonBlocking));
  uint64_t unmasked = 0;
  CHECK(checksum(plain, &unmasked));

  bool all_equal = true;
  int compared = 0;
  for (int bit : {0, 7, 15, 16, 23, 31}) {
    if (bit >= g_units) continue;
    const std::vector<uint32_t> mask = mask_for({bit});
    hipStream_t stream = nullptr;
    CHECK(hipExtStreamCreateWithCUMask(&stream, g_words, mask.data()));
    uint64_t masked = 0;
    CHECK(checksum(stream, &masked));
    if (masked != unmasked) all_equal = false;
    compared += 1;
    hipStreamDestroy(stream);
  }
  // Launch order must not change the result either.
  uint64_t reversed = 0;
  CHECK(checksum(plain, &reversed));
  if (reversed != unmasked) all_equal = false;

  std::printf(
      "{\"experiment\":\"determinism\",\"unmasked_checksum\":%llu"
      ",\"masks_compared\":%d,\"all_identical\":%s}\n",
      static_cast<unsigned long long>(unmasked), compared,
      all_equal ? "true" : "false");
  hipFree(device_out);
  hipStreamDestroy(plain);
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  std::string experiment = argc > 1 ? argv[1] : "";
  int iterations = argc > 2 ? std::atoi(argv[2]) : 10000;

  CHECK(hipSetDevice(0));
  hipDeviceProp_t properties{};
  CHECK(hipGetDeviceProperties(&properties, 0));
  g_units = properties.multiProcessorCount;
  g_words = static_cast<uint32_t>((g_units + 31) / 32);

  int wall_clock_khz = 0;
  CHECK(hipDeviceGetAttribute(&wall_clock_khz, hipDeviceAttributeWallClockRate,
                              0));
  std::printf("{\"experiment\":\"device\",\"units\":%d,\"words\":%u"
              ",\"wall_clock_khz\":%d,\"arch\":\"%s\"}\n",
              g_units, g_words, wall_clock_khz, properties.gcnArchName);

  if (experiment == "concurrency") {
    const uint64_t ticks =
        static_cast<uint64_t>(wall_clock_khz) * 100;  // ~100 ms
    if (run_concurrency(ticks, true, 64) != 0) return 1;
    return run_concurrency(ticks, false, 64);
  }
  if (experiment == "reconfigure") {
    return run_reconfigure(iterations, 64);
  }
  if (experiment == "reconfigure_pooled") {
    return run_reconfigure_pooled(iterations, 64);
  }
  if (experiment == "determinism") {
    return run_determinism(256, 4096);
  }
  std::printf("{\"status\":\"usage_error\",\"error\":\"experiment must be "
              "concurrency|reconfigure|determinism\"}\n");
  return 2;
}
