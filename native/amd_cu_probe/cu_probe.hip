// AMD counterpart of the CUDA %smid probe: which compute units does a
// CU-masked stream actually run on?
//
// The two platforms are not symmetric and this probe is built to show that
// rather than paper over it:
//
//   * NVIDIA needs a reverse-engineered write into an opaque CUstream struct
//     at an offset pinned to one driver version, and offers no way to read
//     the mask back. Every claim rests on an indirect %smid histogram.
//   * AMD exposes hipExtStreamCreateWithCUMask, documented and exported since
//     HIP 4.2, plus hipExtStreamGetCUMask which reads the mask back. So each
//     cell here carries direct evidence of what was requested alongside the
//     indirect evidence of where the kernel ran.
//
// The CU identifier is deliberately not decoded from a register-layout
// specification. gfx12 field positions are recovered empirically instead, by
// enabling exactly one CU and observing which identifier appears -- the mask
// API is the ground truth, and a wrong field mask shows up immediately as
// more than one distinct identifier under a single-CU mask.

#include <hip/hip_runtime.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <sstream>
#include <string>
#include <vector>

namespace {

constexpr const char* kSchema = "burstserve.cu-probe-amd/v1";
constexpr int kThreadsPerBlock = 256;
constexpr int kDefaultIterations = 4096;

// HW_REG encoding accepted by s_getreg_b32: id | (offset << 6) | ((n-1) << 11)
constexpr int kHwId1 = 23;
constexpr int kHwId2 = 24;

// HW_ID1's low bits identify the wave and SIMD inside a CU, so they vary
// within one CU and must not be part of a CU identity. Everything above them
// is treated as opaque: the sweep proves whether that is the right cut.
constexpr uint32_t kIntraCuBits = 0x000003FFu;

// Two independent mechanisms, so a CU mapping can be cross-checked rather
// than merely reported: the documented per-stream API, and ROCm's
// process-wide ROC_GLOBAL_CU_MASK. Neither depends on the other.
enum class Mode { kBaseline, kCuMask, kGlobalCuMask };

const char* mode_name(Mode mode) {
  switch (mode) {
    case Mode::kBaseline: return "baseline";
    case Mode::kCuMask: return "stream_cu_mask";
    case Mode::kGlobalCuMask: return "global_cu_mask";
  }
  return "unknown";
}

struct Options {
  Mode mode = Mode::kBaseline;
  int device_ordinal = 0;
  int enabled_cu = -1;
  int iterations = kDefaultIterations;
  int blocks = 0;
  int maskable_units = 0;
};

__global__ void collect_cuid_kernel(uint32_t* ids, float* sinks,
                                    int iterations) {
  const uint32_t hw_id1 =
      __builtin_amdgcn_s_getreg(kHwId1 | ((32 - 1) << 11));

  const uint64_t global_thread =
      static_cast<uint64_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  float value = 1.0f + static_cast<float>((global_thread & 1023U) + 1U) *
                           0x1p-20f;
#pragma unroll 1
  for (int i = 0; i < iterations; ++i) {
    value = __builtin_fmaf(value, 1.00000011920928955078125f, 0x1p-24f);
  }

  if (threadIdx.x == 0) {
    ids[blockIdx.x] = hw_id1;
  }
  sinks[global_thread] = value;
}

std::string json_escape(const std::string& input) {
  std::ostringstream out;
  for (const unsigned char ch : input) {
    if (ch == '"' || ch == '\\') {
      out << '\\' << static_cast<char>(ch);
    } else if (ch < 0x20) {
      constexpr char kHex[] = "0123456789abcdef";
      out << "\\u00" << kHex[(ch >> 4) & 0xf] << kHex[ch & 0xf];
    } else {
      out << static_cast<char>(ch);
    }
  }
  return out.str();
}

bool parse_int(const char* text, int minimum, int maximum, int* output) {
  if (text == nullptr || *text == '\0') {
    return false;
  }
  errno = 0;
  char* end = nullptr;
  const long parsed = std::strtol(text, &end, 10);
  if (errno != 0 || end == text || *end != '\0' || parsed < minimum ||
      parsed > maximum) {
    return false;
  }
  *output = static_cast<int>(parsed);
  return true;
}

[[noreturn]] void fail(const char* status, const std::string& message) {
  std::printf("{\"schema_version\":\"%s\",\"status\":\"%s\",\"error\":\"%s\"}\n",
              kSchema, status, json_escape(message).c_str());
  std::fprintf(stderr, "cu_probe: %s\n", message.c_str());
  std::exit(status[0] == 'u' ? 2 : 4);
}

#define HIP_CHECK(expr, label)                                              \
  do {                                                                      \
    hipError_t error_ = (expr);                                             \
    if (error_ != hipSuccess) {                                             \
      fail("hip_error",                                                     \
           std::string(label) + " failed: " + hipGetErrorString(error_));   \
    }                                                                       \
  } while (0)

}  // namespace

int main(int argc, char** argv) {
  Options options;
  for (int i = 1; i < argc; ++i) {
    const std::string arg(argv[i]);
    auto value = [&]() -> const char* {
      if (i + 1 >= argc) {
        fail("usage_error", arg + " requires a value");
      }
      return argv[++i];
    };
    if (arg == "--mode") {
      const std::string text(value());
      if (text == "baseline") {
        options.mode = Mode::kBaseline;
      } else if (text == "cu_mask") {
        options.mode = Mode::kCuMask;
      } else if (text == "global_cu_mask") {
        options.mode = Mode::kGlobalCuMask;
      } else {
        fail("usage_error", "invalid --mode value");
      }
    } else if (arg == "--enabled-cu") {
      if (!parse_int(value(), 0, 1023, &options.enabled_cu)) {
        fail("usage_error", "--enabled-cu must be a CU index");
      }
    } else if (arg == "--iterations") {
      if (!parse_int(value(), 1, 100000000, &options.iterations)) {
        fail("usage_error", "--iterations must be positive");
      }
    } else if (arg == "--blocks") {
      if (!parse_int(value(), 1, 10000000, &options.blocks)) {
        fail("usage_error", "--blocks must be positive");
      }
    } else if (arg == "--maskable-units") {
      if (!parse_int(value(), 1, 4096, &options.maskable_units)) {
        fail("usage_error", "--maskable-units must be positive");
      }
    } else if (arg == "--device") {
      if (!parse_int(value(), 0, 1023, &options.device_ordinal)) {
        fail("usage_error", "--device must be a device ordinal");
      }
    } else {
      fail("usage_error", "unrecognized argument: " + arg);
    }
  }
  const bool masked = options.mode != Mode::kBaseline;
  if (masked != (options.enabled_cu >= 0)) {
    fail("usage_error",
         "--enabled-cu is required for masked modes and invalid otherwise");
  }

  const char* global_env = std::getenv("ROC_GLOBAL_CU_MASK");
  if (options.mode == Mode::kGlobalCuMask) {
    if (global_env == nullptr || *global_env == '\0') {
      fail("configuration_error",
           "global_cu_mask requires ROC_GLOBAL_CU_MASK in the environment");
    }
    errno = 0;
    char* end = nullptr;
    const unsigned long long parsed = std::strtoull(global_env, &end, 0);
    if (errno != 0 || end == global_env || *end != '\0' ||
        parsed != (1ULL << options.enabled_cu)) {
      fail("configuration_error",
           std::string("ROC_GLOBAL_CU_MASK ") + global_env +
               " does not select exactly the requested CU");
    }
  } else if (global_env != nullptr) {
    fail("configuration_error",
         "ROC_GLOBAL_CU_MASK must be unset unless the mode is global_cu_mask");
  }

  HIP_CHECK(hipSetDevice(options.device_ordinal), "hipSetDevice");
  hipDeviceProp_t properties{};
  HIP_CHECK(hipGetDeviceProperties(&properties, options.device_ordinal),
            "hipGetDeviceProperties");
  // How wide the mask really is, measured rather than inferred. rocminfo
  // calls this part 64 compute units and hipDeviceProp_t reports 32, so the
  // width is genuinely ambiguous from the outside. A 64-bit sweep settled it:
  // bits 0..31 each confine the kernel to exactly one distinct hardware id,
  // while bits 32..63 leave all 32 ids reachable AND make hipExtStreamGetCUMask
  // report a mask different from the one requested -- the documented
  // "extra elements are ignored" behaviour, caught by the readback rather than
  // inferred from the histogram. So one mask bit is one maskable unit, and
  // there are multiProcessorCount of them.
  //
  // (multiProcessorCount under a global mask reports popcount(mask)/2, which
  // is a reporting quirk of masked devices and not a statement about mask
  // width; it is what first suggested a 64-wide vector, and the sweep refuted
  // that.)
  const int reported_units = properties.multiProcessorCount;
  // A global mask perturbs the reported count itself -- with one bit set it
  // reports zero -- so the device cannot be asked how wide its own mask is
  // while a global mask is in force. The width is declared by the caller and
  // checked against the device only when nothing is masking it.
  const int cu_count =
      options.maskable_units > 0 ? options.maskable_units : reported_units;
  if (options.mode != Mode::kGlobalCuMask && options.maskable_units > 0 &&
      options.maskable_units != reported_units) {
    fail("configuration_error",
         "--maskable-units disagrees with the unmasked device report");
  }
  if (masked && options.enabled_cu >= cu_count) {
    fail("configuration_error", "--enabled-cu is outside the device CU count");
  }

  int driver_version = 0;
  int runtime_version = 0;
  HIP_CHECK(hipDriverGetVersion(&driver_version), "hipDriverGetVersion");
  HIP_CHECK(hipRuntimeGetVersion(&runtime_version), "hipRuntimeGetVersion");

  const uint32_t words = static_cast<uint32_t>((cu_count + 31) / 32);
  std::vector<uint32_t> requested_mask(words, 0);
  hipStream_t stream = nullptr;
  if (masked) {
    requested_mask[options.enabled_cu / 32] =
        UINT32_C(1) << (options.enabled_cu % 32);
  }
  if (options.mode == Mode::kCuMask) {
    HIP_CHECK(hipExtStreamCreateWithCUMask(&stream, words,
                                           requested_mask.data()),
              "hipExtStreamCreateWithCUMask");
  } else {
    HIP_CHECK(hipStreamCreateWithFlags(&stream, hipStreamNonBlocking),
              "hipStreamCreateWithFlags");
  }

  // Read the mask back. This has no CUDA counterpart: on NVIDIA the requested
  // mask is unobservable once written, so a no-op write is indistinguishable
  // from a working one except through the kernel's own behaviour.
  std::vector<uint32_t> readback_mask(words, 0);
  bool readback_ok = false;
  if (options.mode == Mode::kCuMask) {
    readback_ok = hipExtStreamGetCUMask(stream, words,
                                        readback_mask.data()) == hipSuccess;
  }

  const int blocks = options.blocks > 0 ? options.blocks : cu_count * 32;
  uint32_t* device_ids = nullptr;
  float* device_sinks = nullptr;
  HIP_CHECK(hipMalloc(&device_ids, static_cast<size_t>(blocks) * sizeof(uint32_t)),
            "hipMalloc(ids)");
  HIP_CHECK(hipMalloc(&device_sinks,
                      static_cast<size_t>(blocks) * kThreadsPerBlock * sizeof(float)),
            "hipMalloc(sinks)");
  HIP_CHECK(hipMemsetAsync(device_ids, 0xff,
                           static_cast<size_t>(blocks) * sizeof(uint32_t), stream),
            "hipMemsetAsync");
  HIP_CHECK(hipStreamSynchronize(stream), "probe initialization");

  collect_cuid_kernel<<<blocks, kThreadsPerBlock, 0, stream>>>(
      device_ids, device_sinks, options.iterations);
  HIP_CHECK(hipGetLastError(), "kernel launch");
  std::vector<uint32_t> host_ids(static_cast<size_t>(blocks));
  HIP_CHECK(hipMemcpyAsync(host_ids.data(), device_ids,
                           static_cast<size_t>(blocks) * sizeof(uint32_t),
                           hipMemcpyDeviceToHost, stream),
            "hipMemcpyAsync");
  HIP_CHECK(hipStreamSynchronize(stream), "probe execution");

  std::map<uint32_t, uint64_t> histogram;
  for (const uint32_t raw : host_ids) {
    histogram[raw & ~kIntraCuBits] += 1;
  }

  std::ostringstream out;
  out << "{\"schema_version\":\"" << kSchema << "\",\"status\":\"ok\""
      << ",\"mode\":\"" << mode_name(options.mode)
      << "\",\"driver_version\":" << driver_version
      << ",\"runtime_version\":" << runtime_version
      << ",\"device\":{\"ordinal\":" << options.device_ordinal
      << ",\"name\":\"" << json_escape(properties.gcnArchName) << "\""
      << ",\"marketing_name\":\"" << json_escape(properties.name) << "\""
      << ",\"cu_count\":" << cu_count
      << ",\"hip_reported_units\":" << reported_units << "}"
      << ",\"requested_enabled_cu\":";
  if (options.enabled_cu >= 0) {
    out << options.enabled_cu;
  } else {
    out << "null";
  }
  auto emit_mask = [&](const std::vector<uint32_t>& mask) {
    out << '[';
    for (size_t i = 0; i < mask.size(); ++i) {
      if (i != 0) out << ',';
      out << mask[i];
    }
    out << ']';
  };
  out << ",\"requested_cu_mask\":";
  emit_mask(requested_mask);
  out << ",\"readback_supported\":" << (readback_ok ? "true" : "false")
      << ",\"readback_cu_mask\":";
  if (readback_ok) {
    emit_mask(readback_mask);
  } else {
    out << "null";
  }
  out << ",\"readback_matches_request\":"
      << ((readback_ok && readback_mask == requested_mask) ? "true" : "false")
      << ",\"intra_cu_bits_masked\":" << kIntraCuBits
      << ",\"blocks\":" << blocks
      << ",\"threads_per_block\":" << kThreadsPerBlock
      << ",\"iterations\":" << options.iterations
      << ",\"observed_histogram\":{";
  bool first = true;
  for (const auto& [cuid, count] : histogram) {
    if (!first) out << ',';
    first = false;
    out << '"' << cuid << "\":" << count;
  }
  out << "}}\n";
  std::printf("%s", out.str().c_str());

  hipFree(device_sinks);
  hipFree(device_ids);
  hipStreamDestroy(stream);
  return 0;
}
