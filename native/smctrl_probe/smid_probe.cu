#include <cuda.h>
#include <cuda_runtime.h>

#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "libsmctrl.h"

namespace {

constexpr const char* kSchema = "burstserve.smid-probe-native/v1";
constexpr int kThreadsPerBlock = 256;
constexpr int kDefaultIterations = 4096;

enum class ExitCode : int {
  kOk = 0,
  kUsage = 2,
  kUnsupportedDriver = 3,
  kConfiguration = 4,
  kCuda = 5,
  kTopology = 6,
  kMaskObservation = 7,
};

enum class Mode {
  kBaseline,
  kGlobal,
  kNext,
  kStream,
};

struct Options {
  Mode mode = Mode::kBaseline;
  int device_ordinal = 0;
  int enabled_tpc = -1;
  int iterations = kDefaultIterations;
  int blocks = 0;
  bool allow_unsupported_driver = false;
};

struct DeviceInfo {
  int ordinal = 0;
  std::string name;
  std::string uuid;
  int cc_major = -1;
  int cc_minor = -1;
  int sm_count = -1;
};

struct Report {
  std::string status = "configuration_error";
  Mode mode = Mode::kBaseline;
  int driver_version = -1;
  int runtime_version = -1;
  DeviceInfo device;
  int requested_enabled_tpc = -1;
  int tpc_count = -1;
  int blocks = 0;
  int iterations = 0;
  std::map<uint32_t, uint64_t> histogram;
  std::string error;
};

const char* mode_name(Mode mode) {
  switch (mode) {
    case Mode::kBaseline:
      return "baseline";
    case Mode::kGlobal:
      return "global";
    case Mode::kNext:
      return "next";
    case Mode::kStream:
      return "stream";
  }
  return "unknown";
}

bool is_masked_mode(Mode mode) {
  return mode != Mode::kBaseline;
}

std::string json_escape(const std::string& input) {
  std::ostringstream out;
  for (const unsigned char ch : input) {
    switch (ch) {
      case '"':
        out << "\\\"";
        break;
      case '\\':
        out << "\\\\";
        break;
      case '\b':
        out << "\\b";
        break;
      case '\f':
        out << "\\f";
        break;
      case '\n':
        out << "\\n";
        break;
      case '\r':
        out << "\\r";
        break;
      case '\t':
        out << "\\t";
        break;
      default:
        if (ch < 0x20) {
          constexpr char kHex[] = "0123456789abcdef";
          out << "\\u00" << kHex[(ch >> 4) & 0xf] << kHex[ch & 0xf];
        } else {
          out << static_cast<char>(ch);
        }
    }
  }
  return out.str();
}

void emit_report(const Report& report) {
  std::ostringstream out;
  out << "{\"schema_version\":\"" << kSchema << "\""
      << ",\"status\":\"" << json_escape(report.status) << "\""
      << ",\"mode\":\"" << mode_name(report.mode) << "\""
      << ",\"driver_version\":";
  if (report.driver_version >= 0) {
    out << report.driver_version;
  } else {
    out << "null";
  }
  out << ",\"runtime_version\":";
  if (report.runtime_version >= 0) {
    out << report.runtime_version;
  } else {
    out << "null";
  }
  out << ",\"device\":{\"ordinal\":" << report.device.ordinal << ",\"name\":";
  if (!report.device.name.empty()) {
    out << "\"" << json_escape(report.device.name) << "\"";
  } else {
    out << "null";
  }
  out << ",\"uuid\":";
  if (!report.device.uuid.empty()) {
    out << "\"" << json_escape(report.device.uuid) << "\"";
  } else {
    out << "null";
  }
  out << ",\"cc_major\":";
  if (report.device.cc_major >= 0) {
    out << report.device.cc_major;
  } else {
    out << "null";
  }
  out << ",\"cc_minor\":";
  if (report.device.cc_minor >= 0) {
    out << report.device.cc_minor;
  } else {
    out << "null";
  }
  out << ",\"sm_count\":";
  if (report.device.sm_count >= 0) {
    out << report.device.sm_count;
  } else {
    out << "null";
  }
  out << "},\"requested_enabled_tpc\":";
  if (report.requested_enabled_tpc >= 0) {
    out << report.requested_enabled_tpc;
  } else {
    out << "null";
  }
  out << ",\"tpc_count\":";
  if (report.tpc_count >= 0) {
    out << report.tpc_count;
  } else {
    out << "null";
  }
  out << ",\"blocks\":" << report.blocks
      << ",\"threads_per_block\":" << kThreadsPerBlock
      << ",\"iterations\":" << report.iterations
      << ",\"observed_histogram\":{";
  bool first = true;
  for (const auto& [smid, count] : report.histogram) {
    if (!first) {
      out << ',';
    }
    first = false;
    out << '"' << smid << "\":" << count;
  }
  out << '}';
  if (!report.error.empty()) {
    out << ",\"error\":\"" << json_escape(report.error) << "\"";
  }
  out << "}\n";
  std::cout << out.str();
}

void print_usage(const char* argv0) {
  std::cerr
      << "Usage: " << argv0
      << " --mode baseline|global|next|stream [options]\n"
      << "  --enabled-tpc N             Enable exactly TPC mask bit N (masked modes)\n"
      << "  --iterations N              FMA iterations per thread (default 4096)\n"
      << "  --blocks N                  Kernel blocks (default 32 x SM count)\n"
      << "  --device N                  CUDA device ordinal (default 0)\n"
      << "  --allow-unsupported-driver  Explicitly permit an experimental masked run\n";
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

bool parse_mode(const char* text, Mode* mode) {
  if (std::strcmp(text, "baseline") == 0) {
    *mode = Mode::kBaseline;
    return true;
  }
  if (std::strcmp(text, "global") == 0) {
    *mode = Mode::kGlobal;
    return true;
  }
  if (std::strcmp(text, "next") == 0) {
    *mode = Mode::kNext;
    return true;
  }
  if (std::strcmp(text, "stream") == 0) {
    *mode = Mode::kStream;
    return true;
  }
  return false;
}

bool parse_options(int argc, char** argv, Options* options, std::string* error) {
  for (int i = 1; i < argc; ++i) {
    const std::string arg(argv[i]);
    auto require_value = [&](const char* name) -> const char* {
      if (i + 1 >= argc) {
        *error = std::string(name) + " requires a value";
        return nullptr;
      }
      return argv[++i];
    };

    if (arg == "--mode") {
      const char* value = require_value("--mode");
      if (value == nullptr) {
        return false;
      }
      if (!parse_mode(value, &options->mode)) {
        *error = "invalid --mode value";
        return false;
      }
    } else if (arg == "--enabled-tpc") {
      const char* value = require_value("--enabled-tpc");
      if (value == nullptr) {
        return false;
      }
      if (!parse_int(value, 0, 127, &options->enabled_tpc)) {
        *error = "--enabled-tpc must be an integer in [0, 127]";
        return false;
      }
    } else if (arg == "--iterations") {
      const char* value = require_value("--iterations");
      if (value == nullptr) {
        return false;
      }
      if (!parse_int(value, 1, 100000000, &options->iterations)) {
        *error = "--iterations must be a positive integer";
        return false;
      }
    } else if (arg == "--blocks") {
      const char* value = require_value("--blocks");
      if (value == nullptr) {
        return false;
      }
      if (!parse_int(value, 1, 10000000, &options->blocks)) {
        *error = "--blocks must be a positive integer";
        return false;
      }
    } else if (arg == "--device") {
      const char* value = require_value("--device");
      if (value == nullptr) {
        return false;
      }
      if (!parse_int(value, 0, 1023, &options->device_ordinal)) {
        *error = "--device must be a non-negative integer";
        return false;
      }
    } else if (arg == "--allow-unsupported-driver") {
      options->allow_unsupported_driver = true;
    } else if (arg == "--help" || arg == "-h") {
      print_usage(argv[0]);
      std::exit(static_cast<int>(ExitCode::kOk));
    } else {
      *error = "unrecognized argument: " + arg;
      return false;
    }
  }

  if (is_masked_mode(options->mode) && options->enabled_tpc < 0) {
    *error = "--enabled-tpc is required for masked modes";
    return false;
  }
  if (!is_masked_mode(options->mode) && options->enabled_tpc >= 0) {
    *error = "--enabled-tpc is invalid in baseline mode";
    return false;
  }
  return true;
}

bool driver_is_validated_by_upstream(Mode mode, int version) {
  // Keep this exact whitelist synchronized with the switch/table in the
  // vendored libsmctrl. Unknown versions must never silently inherit an
  // adjacent stream-structure offset.
  switch (version) {
    case 6050:
    case 7000:
    case 7050:
      return mode == Mode::kGlobal || mode == Mode::kNext;
    case 8000:
      // The vendored x86_64 stream switch falls through from its CUDA 8.0
      // offset to the CUDA 9.0 offset. Keep stream mode fail-closed until that
      // upstream path is fixed and independently validated.
      return mode == Mode::kGlobal || mode == Mode::kNext;
    case 9000:
    case 9010:
    case 9020:
    case 10000:
    case 10010:
    case 10020:
    case 11000:
    case 11010:
    case 11020:
    case 11030:
    case 11040:
    case 11050:
    case 11060:
    case 11070:
    case 11080:
    case 12000:
    case 12010:
    case 12020:
    case 12030:
    case 12040:
    case 12050:
    case 12060:
    case 12070:
    case 12080:
      return true;
    default:
      return false;
  }
}

bool validate_mask_off(std::string* error) {
  const char* value = std::getenv("MASK_OFF");
  if (value == nullptr || *value == '\0') {
    *error =
        "experimental stream mode requires an explicit integer MASK_OFF";
    return false;
  }
  int offset = 0;
  // libsmctrl applies this to its CUDA-12.2 base (0x4e4). Reject values which
  // its implementation would treat as a negative total address.
  if (!parse_int(value, -0x4e4, 1 << 20, &offset)) {
    *error =
        "MASK_OFF must be an integer whose CUDA-12.2-relative total is nonnegative";
    return false;
  }
  return true;
}

bool mask_off_is_present() {
  return std::getenv("MASK_OFF") != nullptr;
}

std::string format_cuda_uuid(const cudaUUID_t& uuid) {
  const auto* bytes =
      reinterpret_cast<const unsigned char*>(uuid.bytes);
  std::ostringstream out;
  out << "GPU-" << std::hex << std::setfill('0');
  for (int index = 0; index < 16; ++index) {
    if (index == 4 || index == 6 || index == 8 || index == 10) {
      out << '-';
    }
    out << std::setw(2) << static_cast<unsigned int>(bytes[index]);
  }
  return out.str();
}

std::string cuda_error_message(const char* operation, cudaError_t error) {
  std::ostringstream out;
  out << operation << " failed: " << cudaGetErrorName(error) << " ("
      << cudaGetErrorString(error) << ')';
  return out.str();
}

__global__ void collect_smid_kernel(uint32_t* smids, float* sinks,
                                    int iterations) {
  uint32_t smid = 0;
  asm volatile("mov.u32 %0, %%smid;" : "=r"(smid));

  const uint64_t global_thread =
      static_cast<uint64_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  float value = 1.0f + static_cast<float>((global_thread & 1023U) + 1U) *
                           0x1p-20f;
  const float multiplier = 1.00000011920928955078125f;
  const float addend = 0x1p-24f;
#pragma unroll 1
  for (int i = 0; i < iterations; ++i) {
    asm volatile("fma.rn.f32 %0, %0, %1, %2;"
                 : "+f"(value)
                 : "f"(multiplier), "f"(addend));
  }

  if (threadIdx.x == 0) {
    smids[blockIdx.x] = smid;
  }
  sinks[global_thread] = value;
}

ExitCode fail(Report* report, ExitCode code, const char* status,
              const std::string& error) {
  report->status = status;
  report->error = error;
  std::cerr << "smid_probe: " << error << '\n';
  emit_report(*report);
  return code;
}

}  // namespace

int main(int argc, char** argv) {
  Options options;
  Report report;
  std::string parse_error;
  if (!parse_options(argc, argv, &options, &parse_error)) {
    report.mode = options.mode;
    report.requested_enabled_tpc = options.enabled_tpc;
    report.iterations = options.iterations;
    print_usage(argv[0]);
    return static_cast<int>(
        fail(&report, ExitCode::kUsage, "usage_error", parse_error));
  }

  report.mode = options.mode;
  report.device.ordinal = options.device_ordinal;
  report.requested_enabled_tpc = options.enabled_tpc;
  report.iterations = options.iterations;

  CUresult driver_result = cuDriverGetVersion(&report.driver_version);
  if (driver_result != CUDA_SUCCESS) {
    const char* error_name = nullptr;
    cuGetErrorName(driver_result, &error_name);
    return static_cast<int>(fail(
        &report, ExitCode::kCuda, "cuda_error",
        std::string("cuDriverGetVersion failed: ") +
            (error_name == nullptr ? "unknown CUDA driver error" : error_name)));
  }

  cudaError_t runtime_result = cudaRuntimeGetVersion(&report.runtime_version);
  if (runtime_result != cudaSuccess) {
    return static_cast<int>(
        fail(&report, ExitCode::kCuda, "cuda_error",
             cuda_error_message("cudaRuntimeGetVersion", runtime_result)));
  }

  if (is_masked_mode(options.mode) &&
      !driver_is_validated_by_upstream(options.mode, report.driver_version) &&
      !options.allow_unsupported_driver) {
    std::ostringstream error;
    error << "driver API version " << report.driver_version
          << " is not in vendored libsmctrl's validated table; masked mode "
             "requires --allow-unsupported-driver";
    return static_cast<int>(fail(&report, ExitCode::kUnsupportedDriver,
                                 "unsupported_driver", error.str()));
  }

  if (options.mode == Mode::kStream) {
    const bool stream_driver_validated =
        driver_is_validated_by_upstream(options.mode, report.driver_version);
    if (stream_driver_validated && mask_off_is_present()) {
      return static_cast<int>(fail(
          &report, ExitCode::kConfiguration, "configuration_error",
          "MASK_OFF is forbidden for a validated stream driver; unset it to "
          "use the pinned offset"));
    }
    if (!stream_driver_validated && !validate_mask_off(&parse_error)) {
      return static_cast<int>(fail(&report, ExitCode::kConfiguration,
                                   "configuration_error", parse_error));
    }
  }

  runtime_result = cudaSetDevice(options.device_ordinal);
  if (runtime_result != cudaSuccess) {
    return static_cast<int>(
        fail(&report, ExitCode::kCuda, "cuda_error",
             cuda_error_message("cudaSetDevice", runtime_result)));
  }

  cudaDeviceProp properties{};
  runtime_result =
      cudaGetDeviceProperties(&properties, options.device_ordinal);
  if (runtime_result != cudaSuccess) {
    return static_cast<int>(
        fail(&report, ExitCode::kCuda, "cuda_error",
             cuda_error_message("cudaGetDeviceProperties", runtime_result)));
  }
  report.device.name = properties.name;
  report.device.uuid = format_cuda_uuid(properties.uuid);
  report.device.cc_major = properties.major;
  report.device.cc_minor = properties.minor;
  report.device.sm_count = properties.multiProcessorCount;

  uint32_t tpc_count = 0;
  if (is_masked_mode(options.mode)) {
    const int topology_result =
        libsmctrl_get_tpc_info_cuda(&tpc_count, options.device_ordinal);
    if (topology_result != 0 || tpc_count == 0) {
      std::ostringstream error;
      error << "libsmctrl_get_tpc_info_cuda failed with code "
            << topology_result;
      return static_cast<int>(fail(&report, ExitCode::kTopology,
                                   "topology_error", error.str()));
    }
    report.tpc_count = static_cast<int>(tpc_count);
    if (options.enabled_tpc >= static_cast<int>(tpc_count)) {
      std::ostringstream error;
      error << "requested TPC bit " << options.enabled_tpc
            << " is outside detected TPC count " << tpc_count;
      return static_cast<int>(fail(&report, ExitCode::kConfiguration,
                                   "configuration_error", error.str()));
    }
    if ((options.mode == Mode::kGlobal || options.mode == Mode::kNext) &&
        options.enabled_tpc >= 64) {
      return static_cast<int>(fail(
          &report, ExitCode::kConfiguration, "configuration_error",
          "global/next libsmctrl APIs cannot select a TPC bit above 63"));
    }
  }

  const int blocks =
      options.blocks > 0 ? options.blocks : properties.multiProcessorCount * 32;
  report.blocks = blocks;

  uint32_t* device_smids = nullptr;
  float* device_sinks = nullptr;
  cudaStream_t stream = nullptr;

  runtime_result = cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking);
  if (runtime_result != cudaSuccess) {
    return static_cast<int>(
        fail(&report, ExitCode::kCuda, "cuda_error",
             cuda_error_message("cudaStreamCreateWithFlags", runtime_result)));
  }

  auto cleanup = [&]() {
    if (device_sinks != nullptr) {
      cudaFree(device_sinks);
    }
    if (device_smids != nullptr) {
      cudaFree(device_smids);
    }
    if (stream != nullptr) {
      cudaStreamDestroy(stream);
    }
  };

  runtime_result =
      cudaMalloc(&device_smids, static_cast<size_t>(blocks) * sizeof(uint32_t));
  if (runtime_result != cudaSuccess) {
    cleanup();
    return static_cast<int>(
        fail(&report, ExitCode::kCuda, "cuda_error",
             cuda_error_message("cudaMalloc(smids)", runtime_result)));
  }
  const size_t sink_elements =
      static_cast<size_t>(blocks) * kThreadsPerBlock;
  runtime_result =
      cudaMalloc(&device_sinks, sink_elements * sizeof(float));
  if (runtime_result != cudaSuccess) {
    cleanup();
    return static_cast<int>(
        fail(&report, ExitCode::kCuda, "cuda_error",
             cuda_error_message("cudaMalloc(sinks)", runtime_result)));
  }

  // Finish all lazy runtime initialization before changing a launch mask.
  runtime_result = cudaMemsetAsync(
      device_smids, 0xff, static_cast<size_t>(blocks) * sizeof(uint32_t),
      stream);
  if (runtime_result == cudaSuccess) {
    runtime_result = cudaStreamSynchronize(stream);
  }
  if (runtime_result != cudaSuccess) {
    cleanup();
    return static_cast<int>(
        fail(&report, ExitCode::kCuda, "cuda_error",
             cuda_error_message("probe initialization", runtime_result)));
  }

  if (options.mode == Mode::kGlobal) {
    const uint64_t disabled_mask =
        ~(UINT64_C(1) << options.enabled_tpc);
    libsmctrl_set_global_mask(disabled_mask);
  } else if (options.mode == Mode::kNext) {
    const uint64_t disabled_mask =
        ~(UINT64_C(1) << options.enabled_tpc);
    libsmctrl_set_next_mask(disabled_mask);
  } else if (options.mode == Mode::kStream) {
    uint128_t disabled_mask_ext = ~static_cast<uint128_t>(0);
    disabled_mask_ext ^= static_cast<uint128_t>(1) << options.enabled_tpc;
    libsmctrl_set_stream_mask_ext(stream, disabled_mask_ext);
  }

  collect_smid_kernel<<<blocks, kThreadsPerBlock, 0, stream>>>(
      device_smids, device_sinks, options.iterations);
  runtime_result = cudaGetLastError();

  // Restore unrestricted launch defaults immediately after the probe launch;
  // masks apply when the launch descriptor is created, not while it executes.
  if (options.mode == Mode::kGlobal) {
    libsmctrl_set_global_mask(0);
  } else if (options.mode == Mode::kStream) {
    libsmctrl_set_stream_mask_ext(stream, static_cast<uint128_t>(0));
  }

  if (runtime_result != cudaSuccess) {
    cleanup();
    return static_cast<int>(
        fail(&report, ExitCode::kCuda, "cuda_error",
             cuda_error_message("collect_smid_kernel launch", runtime_result)));
  }

  std::vector<uint32_t> host_smids(static_cast<size_t>(blocks));
  runtime_result = cudaMemcpyAsync(
      host_smids.data(), device_smids,
      static_cast<size_t>(blocks) * sizeof(uint32_t), cudaMemcpyDeviceToHost,
      stream);
  if (runtime_result == cudaSuccess) {
    runtime_result = cudaStreamSynchronize(stream);
  }
  if (runtime_result != cudaSuccess) {
    cleanup();
    return static_cast<int>(
        fail(&report, ExitCode::kCuda, "cuda_error",
             cuda_error_message("probe execution/copy", runtime_result)));
  }
  cleanup();

  for (const uint32_t smid : host_smids) {
    ++report.histogram[smid];
  }
  if (report.histogram.empty()) {
    return static_cast<int>(fail(&report, ExitCode::kCuda, "cuda_error",
                                 "probe produced an empty SM-ID histogram"));
  }

  if (is_masked_mode(options.mode)) {
    const int max_sms_per_tpc =
        (properties.multiProcessorCount + static_cast<int>(tpc_count) - 1) /
        static_cast<int>(tpc_count);
    if (static_cast<int>(report.histogram.size()) > max_sms_per_tpc) {
      std::ostringstream error;
      error << "single-TPC mask observed " << report.histogram.size()
            << " unique SM IDs, exceeding topology bound "
            << max_sms_per_tpc;
      return static_cast<int>(
          fail(&report, ExitCode::kMaskObservation, "mask_observation_error",
               error.str()));
    }
  }

  report.status = "ok";
  emit_report(report);
  return static_cast<int>(ExitCode::kOk);
}
