#ifndef BURSTSERVE_PARENT_GUARD_H_
#define BURSTSERVE_PARENT_GUARD_H_

#include <signal.h>
#include <sys/types.h>

#ifdef __cplusplus
extern "C" {
#endif

enum burstserve_parent_guard_result {
  BURSTSERVE_PARENT_GUARD_OK = 0,
  BURSTSERVE_PARENT_GUARD_MISSING_ENV = 1,
  BURSTSERVE_PARENT_GUARD_INVALID_ENV = 2,
  BURSTSERVE_PARENT_GUARD_PRCTL_FAILED = 3,
  BURSTSERVE_PARENT_GUARD_PARENT_MISMATCH = 4,
};

struct burstserve_parent_guard_report {
  enum burstserve_parent_guard_result result;
  pid_t expected_parent_pid;
  pid_t observed_parent_pid;
  int pdeath_signal;
  int saved_errno;
};

int burstserve_parse_parent_pid(const char* text, pid_t* output);

enum burstserve_parent_guard_result burstserve_arm_parent_guard(
    const char* environment_name,
    struct burstserve_parent_guard_report* report);

const char* burstserve_parent_guard_result_name(
    enum burstserve_parent_guard_result result);

int burstserve_sanitize_loader_environment(void);

int burstserve_restore_lifecycle_signals(void);

#ifdef __cplusplus
}
#endif

#endif  // BURSTSERVE_PARENT_GUARD_H_
