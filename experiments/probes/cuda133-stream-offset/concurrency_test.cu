// Does libsmctrl's thread-local `next` mask deliver CONCURRENT disjoint-SM
// partitioning, i.e. the capability that per-stream masking exists to provide?
//
// Two host threads each set their own next-mask and launch a long kernel on
// their own stream. Each block records its SM id plus the global nanosecond
// timer at entry and exit, so temporal overlap is measured rather than assumed.
//
// This performs no blind struct writes: `next` masking goes through the CUDA
// QMD pre-upload debug callback.

#include <cuda.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <set>
#include <thread>
#include <vector>

#include "libsmctrl.h"

namespace {

constexpr int kThreads = 256;
constexpr int kBlocks = 64;

struct BlockRecord {
  uint32_t smid;
  uint64_t enter_ns;
  uint64_t exit_ns;
};

__global__ void spin_kernel(BlockRecord* records, uint64_t spin_ns,
                            float* sink) {
  uint32_t smid = 0;
  asm volatile("mov.u32 %0, %%smid;" : "=r"(smid));
  uint64_t enter = 0;
  asm volatile("mov.u64 %0, %%globaltimer;" : "=l"(enter));

  float value = 1.0f + static_cast<float>(threadIdx.x) * 0x1p-20f;
  uint64_t now = enter;
  while (now - enter < spin_ns) {
#pragma unroll 1
    for (int i = 0; i < 1024; ++i) {
      asm volatile("fma.rn.f32 %0, %0, %1, %2;"
                   : "+f"(value)
                   : "f"(1.00000011920928955078125f), "f"(0x1p-24f));
    }
    asm volatile("mov.u64 %0, %%globaltimer;" : "=l"(now));
  }

  if (threadIdx.x == 0) {
    records[blockIdx.x].smid = smid;
    records[blockIdx.x].enter_ns = enter;
    records[blockIdx.x].exit_ns = now;
  }
  sink[blockIdx.x * blockDim.x + threadIdx.x] = value;
}

struct Arm {
  const char* name;
  int first_bit;
  int bit_count;
  cudaStream_t stream = nullptr;
  BlockRecord* device_records = nullptr;
  float* device_sink = nullptr;
  std::vector<BlockRecord> host_records;
  cudaError_t error = cudaSuccess;
};

// Launched on its own host thread: sets that thread's TLS next-mask, then
// immediately submits. The mask is consumed by the pre-upload callback.
void run_arm(Arm* arm, uint64_t spin_ns, bool masked) {
  if (masked) {
    uint64_t enabled = 0;
    for (int i = 0; i < arm->bit_count; ++i) {
      enabled |= UINT64_C(1) << (arm->first_bit + i);
    }
    libsmctrl_set_next_mask(~enabled);
  }
  spin_kernel<<<kBlocks, kThreads, 0, arm->stream>>>(
      arm->device_records, spin_ns, arm->device_sink);
  arm->error = cudaGetLastError();
  if (arm->error != cudaSuccess) {
    return;
  }
  arm->error = cudaStreamSynchronize(arm->stream);
}

}  // namespace

int main(int argc, char** argv) {
  const uint64_t spin_ns = argc > 1 ? strtoull(argv[1], nullptr, 10) : 200000000;
  // Control arm: identical launches with masking disabled. If the two arms
  // land on disjoint SMs even unmasked, the masked result proves nothing.
  const bool masked = !(argc > 2 && strcmp(argv[2], "--no-mask") == 0);
  printf("masking: %s\n", masked ? "next" : "DISABLED (control)");

  cudaDeviceProp properties{};
  if (cudaGetDeviceProperties(&properties, 0) != cudaSuccess) {
    fprintf(stderr, "cudaGetDeviceProperties failed\n");
    return 1;
  }
  printf("device: %s, %d SMs\n", properties.name,
         properties.multiProcessorCount);

  Arm arms[2] = {{"urgent", 0, 8}, {"video", 32, 8}};
  for (Arm& arm : arms) {
    if (cudaStreamCreateWithFlags(&arm.stream, cudaStreamNonBlocking) !=
            cudaSuccess ||
        cudaMalloc(&arm.device_records, kBlocks * sizeof(BlockRecord)) !=
            cudaSuccess ||
        cudaMalloc(&arm.device_sink, kBlocks * kThreads * sizeof(float)) !=
            cudaSuccess ||
        cudaMemset(arm.device_records, 0xff,
                   kBlocks * sizeof(BlockRecord)) != cudaSuccess) {
      fprintf(stderr, "setup failed for arm %s\n", arm.name);
      return 1;
    }
    arm.host_records.resize(kBlocks);
  }
  // Force all lazy runtime initialization to finish before any mask is set.
  if (cudaDeviceSynchronize() != cudaSuccess) {
    fprintf(stderr, "warm-up sync failed\n");
    return 1;
  }

  std::thread workers[2];
  for (int i = 0; i < 2; ++i) {
    workers[i] = std::thread(run_arm, &arms[i], spin_ns, masked);
  }
  for (std::thread& worker : workers) {
    worker.join();
  }

  bool ok = true;
  uint64_t window_start = UINT64_MAX;
  uint64_t window_end = 0;
  std::set<uint32_t> sm_sets[2];
  for (int i = 0; i < 2; ++i) {
    Arm& arm = arms[i];
    if (arm.error != cudaSuccess) {
      fprintf(stderr, "arm %s failed: %s\n", arm.name,
              cudaGetErrorString(arm.error));
      return 1;
    }
    if (cudaMemcpy(arm.host_records.data(), arm.device_records,
                   kBlocks * sizeof(BlockRecord),
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      fprintf(stderr, "copyback failed for arm %s\n", arm.name);
      return 1;
    }
    uint64_t first = UINT64_MAX;
    uint64_t last = 0;
    for (const BlockRecord& record : arm.host_records) {
      sm_sets[i].insert(record.smid);
      if (record.enter_ns < first) first = record.enter_ns;
      if (record.exit_ns > last) last = record.exit_ns;
    }
    printf("arm %-7s bits %2d..%2d -> %zu SMs:", arm.name, arm.first_bit,
           arm.first_bit + arm.bit_count - 1, sm_sets[i].size());
    for (uint32_t smid : sm_sets[i]) printf(" %u", smid);
    printf("\n           window [%lu, %lu] ns, span %.1f ms\n", first, last,
           (last - first) / 1e6);
    if (first < window_start) window_start = first;
    if (last > window_end) window_end = last;
  }

  // Overlap of the two arms' [first_enter, last_exit] windows.
  uint64_t a_first = UINT64_MAX, a_last = 0, b_first = UINT64_MAX, b_last = 0;
  for (const BlockRecord& r : arms[0].host_records) {
    if (r.enter_ns < a_first) a_first = r.enter_ns;
    if (r.exit_ns > a_last) a_last = r.exit_ns;
  }
  for (const BlockRecord& r : arms[1].host_records) {
    if (r.enter_ns < b_first) b_first = r.enter_ns;
    if (r.exit_ns > b_last) b_last = r.exit_ns;
  }
  const uint64_t overlap_start = a_first > b_first ? a_first : b_first;
  const uint64_t overlap_end = a_last < b_last ? a_last : b_last;
  const double overlap_ms =
      overlap_end > overlap_start ? (overlap_end - overlap_start) / 1e6 : 0.0;
  printf("temporal overlap: %.1f ms\n", overlap_ms);

  std::set<uint32_t> intersection;
  for (uint32_t smid : sm_sets[0]) {
    if (sm_sets[1].count(smid)) intersection.insert(smid);
  }
  printf("SM overlap: %zu\n", intersection.size());

  // The discriminating evidence is CONFINEMENT to the set the Phase-0
  // oracle predicts (bit N -> SMs {2N, 2N+1}), not disjointness: the
  // unmasked control also produces disjoint sets, because the block
  // scheduler happens to split the die evenly between two concurrent
  // kernels. Only set identity separates a real mask from that artifact.
  bool confined = true;
  for (int i = 0; i < 2; ++i) {
    std::set<uint32_t> predicted;
    for (int b = 0; b < arms[i].bit_count; ++b) {
      predicted.insert(2 * (arms[i].first_bit + b));
      predicted.insert(2 * (arms[i].first_bit + b) + 1);
    }
    if (sm_sets[i] != predicted) {
      confined = false;
      printf("arm %s does NOT match the oracle-predicted SM set\n",
             arms[i].name);
    }
  }
  printf("matches oracle prediction: %s\n", confined ? "yes" : "no");
  ok = ok && confined && intersection.empty() && overlap_ms > 1.0;
  if (!masked) {
    printf("(control run: a disjoint result here would invalidate the test)\n");
  }
  printf("VERDICT: %s\n", ok ? "CONCURRENT_DISJOINT_PARTITION_CONFIRMED"
                             : "FAILED");
  return ok ? 0 : 1;
}
