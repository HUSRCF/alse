// Independently test the three regimes in which the thread-local `next` mask
// is claimed to fail. Written from scratch rather than reusing the reviewer's
// probe, so a bug in one does not propagate into the other.
//
//   1. graph   -- capture N kernels into a CUDA graph, set one mask, launch
//                 the graph. How many nodes are masked, and which?
//   2. theft   -- set a mask, let an unrelated kernel launch first, then the
//                 intended kernel. Who gets the mask?
//   3. leak    -- set a mask, never launch, then launch something unrelated
//                 later. Does the stale mask apply?
//
// Oracle from Phase 0: TPC bit N confines a kernel to SMs {2N, 2N+1}.

#include <cuda.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <set>
#include <vector>

#include "libsmctrl.h"

#define CHECK(expr)                                                       \
  do {                                                                    \
    cudaError_t err_ = (expr);                                            \
    if (err_ != cudaSuccess) {                                            \
      printf("FATAL %s: %s\n", #expr, cudaGetErrorString(err_));          \
      return 1;                                                           \
    }                                                                     \
  } while (0)

constexpr int kBlocks = 32;
constexpr int kThreads = 128;

__global__ void tag(uint32_t* out, int slot, uint64_t spin) {
  uint32_t smid;
  asm volatile("mov.u32 %0, %%smid;" : "=r"(smid));
  uint64_t t0, t1;
  asm volatile("mov.u64 %0, %%globaltimer;" : "=l"(t0));
  do {
    asm volatile("mov.u64 %0, %%globaltimer;" : "=l"(t1));
  } while (t1 - t0 < spin);
  if (threadIdx.x == 0) {
    out[slot * kBlocks + blockIdx.x] = smid;
  }
}

static std::set<uint32_t> sms_of(const std::vector<uint32_t>& host, int slot) {
  std::set<uint32_t> s;
  for (int i = 0; i < kBlocks; ++i) {
    uint32_t v = host[slot * kBlocks + i];
    if (v != 0xffffffffu) s.insert(v);
  }
  return s;
}

static void show(const char* label, const std::set<uint32_t>& s, int bit) {
  std::set<uint32_t> want{(uint32_t)(2 * bit), (uint32_t)(2 * bit + 1)};
  printf("  %-22s %2zu SMs", label, s.size());
  if (s.size() <= 4) {
    printf(" {");
    for (uint32_t v : s) printf(" %u", v);
    printf(" }");
  }
  printf("  -> %s\n", s == want ? "MASKED (matches oracle)" : "not masked");
}

// mask that leaves only TPC bit `bit` enabled
static uint64_t only(int bit) { return ~(UINT64_C(1) << bit); }

int main() {
  constexpr int kSlots = 8;
  uint32_t* device = nullptr;
  CHECK(cudaMalloc(&device, kSlots * kBlocks * sizeof(uint32_t)));
  std::vector<uint32_t> host(kSlots * kBlocks);
  cudaStream_t stream;
  CHECK(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking));

  // Force libsmctrl callback setup to completion before anything is measured,
  // so a cold-start race cannot be mistaken for one of the three regimes.
  libsmctrl_set_global_mask(0);
  CHECK(cudaDeviceSynchronize());

#define RESET() CHECK(cudaMemset(device, 0xff, \
                                 kSlots * kBlocks * sizeof(uint32_t)))

  // ---- 1. CUDA graph -------------------------------------------------
  printf("\n[1] CUDA graph: 4 kernels captured, one next-mask (bit 5 -> SM {10,11})\n");
  RESET();
  cudaGraph_t graph;
  cudaGraphExec_t exec;
  CHECK(cudaStreamBeginCapture(stream, cudaStreamCaptureModeGlobal));
  for (int i = 0; i < 4; ++i) {
    tag<<<kBlocks, kThreads, 0, stream>>>(device, i, 1000000);
  }
  CHECK(cudaStreamEndCapture(stream, &graph));
  CHECK(cudaGraphInstantiate(&exec, graph, nullptr, nullptr, 0));

  libsmctrl_set_next_mask(only(5));
  CHECK(cudaGraphLaunch(exec, stream));
  CHECK(cudaStreamSynchronize(stream));
  CHECK(cudaMemcpy(host.data(), device, kSlots * kBlocks * sizeof(uint32_t),
                   cudaMemcpyDeviceToHost));
  int masked_nodes = 0;
  for (int i = 0; i < 4; ++i) {
    char label[32];
    snprintf(label, sizeof(label), "graph node %d", i);
    auto s = sms_of(host, i);
    show(label, s, 5);
    if (s == std::set<uint32_t>{10, 11}) ++masked_nodes;
  }
  printf("  => %d of 4 nodes masked\n", masked_nodes);

  // ---- 2. mask theft by an interposed launch -------------------------
  printf("\n[2] theft: set mask (bit 3 -> SM {6,7}), interposed kernel launches first\n");
  RESET();
  libsmctrl_set_next_mask(only(3));
  tag<<<kBlocks, kThreads, 0, stream>>>(device, 0, 1000000);  // interposer
  tag<<<kBlocks, kThreads, 0, stream>>>(device, 1, 1000000);  // intended
  CHECK(cudaStreamSynchronize(stream));
  CHECK(cudaMemcpy(host.data(), device, kSlots * kBlocks * sizeof(uint32_t),
                   cudaMemcpyDeviceToHost));
  show("interposed launch", sms_of(host, 0), 3);
  show("intended launch", sms_of(host, 1), 3);

  // ---- 3. stale mask leaking into an unrelated later launch ----------
  printf("\n[3] leak: set mask (bit 20 -> SM {40,41}) and never launch; later launch\n");
  RESET();
  libsmctrl_set_next_mask(only(20));
  CHECK(cudaDeviceSynchronize());  // intended launch never happens
  tag<<<kBlocks, kThreads, 0, stream>>>(device, 0, 1000000);  // unrelated work
  CHECK(cudaStreamSynchronize(stream));
  CHECK(cudaMemcpy(host.data(), device, kSlots * kBlocks * sizeof(uint32_t),
                   cudaMemcpyDeviceToHost));
  show("unrelated later launch", sms_of(host, 0), 20);

  cudaGraphExecDestroy(exec);
  cudaGraphDestroy(graph);
  return 0;
}
