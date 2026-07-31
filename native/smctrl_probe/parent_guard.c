#define _GNU_SOURCE

#include "parent_guard.h"

#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <unistd.h>

extern char** environ;

_Static_assert((pid_t)-1 < 0, "parent guard requires a signed pid_t");
_Static_assert(sizeof(pid_t) <= sizeof(uint64_t),
               "parent guard requires pid_t to fit in uint64_t");

static uint64_t pid_t_maximum(void) {
  const unsigned int value_bits =
      (unsigned int)(sizeof(pid_t) * CHAR_BIT - 1U);
  if (value_bits >= 64U) {
    return UINT64_MAX >> 1U;
  }
  return (UINT64_C(1) << value_bits) - UINT64_C(1);
}

int burstserve_parse_parent_pid(const char* text, pid_t* output) {
  uint64_t parsed = 0;
  uint64_t maximum;
  const unsigned char* cursor;

  if (text == NULL || *text == '\0' || output == NULL) {
    return 0;
  }
  maximum = pid_t_maximum();
  cursor = (const unsigned char*)text;
  while (*cursor != '\0') {
    uint64_t digit;
    if (*cursor < (unsigned char)'0' || *cursor > (unsigned char)'9') {
      return 0;
    }
    digit = (uint64_t)(*cursor - (unsigned char)'0');
    if (parsed > (maximum - digit) / UINT64_C(10)) {
      return 0;
    }
    parsed = parsed * UINT64_C(10) + digit;
    ++cursor;
  }
  if (parsed == 0) {
    return 0;
  }
  *output = (pid_t)parsed;
  return 1;
}

enum burstserve_parent_guard_result burstserve_arm_parent_guard(
    const char* environment_name,
    struct burstserve_parent_guard_report* report) {
  const char* expected_text;
  pid_t expected_parent = 0;
  pid_t observed_parent;

  if (report == NULL || environment_name == NULL ||
      *environment_name == '\0') {
    errno = EINVAL;
    return BURSTSERVE_PARENT_GUARD_INVALID_ENV;
  }
  report->result = BURSTSERVE_PARENT_GUARD_INVALID_ENV;
  report->expected_parent_pid = (pid_t)-1;
  report->observed_parent_pid = getppid();
  report->pdeath_signal = 0;
  report->saved_errno = 0;

  expected_text = getenv(environment_name);
  if (expected_text == NULL || *expected_text == '\0') {
    report->result = BURSTSERVE_PARENT_GUARD_MISSING_ENV;
    return report->result;
  }
  if (!burstserve_parse_parent_pid(expected_text, &expected_parent)) {
    report->result = BURSTSERVE_PARENT_GUARD_INVALID_ENV;
    return report->result;
  }
  report->expected_parent_pid = expected_parent;

  errno = 0;
  if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0) {
    report->saved_errno = errno;
    report->result = BURSTSERVE_PARENT_GUARD_PRCTL_FAILED;
    return report->result;
  }

  /*
   * This identity read must immediately follow PR_SET_PDEATHSIG. If the
   * expected parent died before prctl(), the already-completed death event
   * could not have delivered the newly installed signal.
   */
  observed_parent = getppid();
  report->pdeath_signal = SIGKILL;
  report->observed_parent_pid = observed_parent;
  if (observed_parent != expected_parent) {
    report->result = BURSTSERVE_PARENT_GUARD_PARENT_MISMATCH;
    return report->result;
  }
  report->result = BURSTSERVE_PARENT_GUARD_OK;
  return report->result;
}

const char* burstserve_parent_guard_result_name(
    enum burstserve_parent_guard_result result) {
  switch (result) {
    case BURSTSERVE_PARENT_GUARD_OK:
      return "armed";
    case BURSTSERVE_PARENT_GUARD_MISSING_ENV:
      return "missing_env";
    case BURSTSERVE_PARENT_GUARD_INVALID_ENV:
      return "invalid_env";
    case BURSTSERVE_PARENT_GUARD_PRCTL_FAILED:
      return "prctl_failed";
    case BURSTSERVE_PARENT_GUARD_PARENT_MISMATCH:
      return "parent_mismatch";
  }
  return "unknown";
}

int burstserve_sanitize_loader_environment(void) {
  static const char* const exact_names[] = {
      "GLIBC_TUNABLES",
      "GCONV_PATH",
      "LOCPATH",
      "NLSPATH",
  };
  size_t environment_index = 0;

  while (environ != NULL && environ[environment_index] != NULL) {
    const char* entry = environ[environment_index];
    const char* separator = strchr(entry, '=');
    size_t name_length;
    size_t exact_index;
    int remove_entry = 0;
    char* name;

    if (separator == NULL) {
      errno = EINVAL;
      return -1;
    }
    name_length = (size_t)(separator - entry);
    if (name_length >= 3U && memcmp(entry, "LD_", 3U) == 0) {
      remove_entry = 1;
    }
    if (name_length >= sizeof("CUDA_INJECTION") - 1U &&
        memcmp(entry, "CUDA_INJECTION",
               sizeof("CUDA_INJECTION") - 1U) == 0) {
      remove_entry = 1;
    }
    for (exact_index = 0;
         remove_entry == 0 &&
         exact_index < sizeof(exact_names) / sizeof(exact_names[0]);
         ++exact_index) {
      const size_t expected_length = strlen(exact_names[exact_index]);
      if (name_length == expected_length &&
          memcmp(entry, exact_names[exact_index], expected_length) == 0) {
        remove_entry = 1;
      }
    }
    if (remove_entry == 0) {
      ++environment_index;
      continue;
    }
    name = (char*)malloc(name_length + 1U);
    if (name == NULL) {
      return -1;
    }
    memcpy(name, entry, name_length);
    name[name_length] = '\0';
    if (unsetenv(name) != 0) {
      const int saved_errno = errno;
      free(name);
      errno = saved_errno;
      return -1;
    }
    free(name);
    /* unsetenv() compacts environ; inspect this index again. */
  }
  return 0;
}

int burstserve_restore_lifecycle_signals(void) {
  static const int signals[] = {SIGHUP, SIGINT, SIGTERM};
  struct sigaction action;
  sigset_t unblock;
  size_t index;

  memset(&action, 0, sizeof(action));
  action.sa_handler = SIG_DFL;
  if (sigemptyset(&action.sa_mask) != 0 || sigemptyset(&unblock) != 0) {
    return -1;
  }
  for (index = 0; index < sizeof(signals) / sizeof(signals[0]); ++index) {
    if (sigaction(signals[index], &action, NULL) != 0 ||
        sigaddset(&unblock, signals[index]) != 0) {
      return -1;
    }
  }
  return sigprocmask(SIG_UNBLOCK, &unblock, NULL);
}
