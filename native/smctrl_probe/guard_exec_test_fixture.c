#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <unistd.h>

extern char** environ;

static int fail(const char* message) {
  fprintf(stderr, "guard fixture: %s\n", message);
  return 4;
}

static int parse_positive_int(const char* text, int* output) {
  char* end = NULL;
  long value;
  if (text == NULL || *text == '\0') {
    return -1;
  }
  errno = 0;
  value = strtol(text, &end, 10);
  if (errno != 0 || end == text || *end != '\0' || value <= 0 ||
      value > INT_MAX) {
    return -1;
  }
  *output = (int)value;
  return 0;
}

static int forbidden_environment_is_absent(void) {
  static const char* const exact_names[] = {
      "GLIBC_TUNABLES",
      "GCONV_PATH",
      "LOCPATH",
      "NLSPATH",
  };
  size_t environment_index;

  for (environment_index = 0;
       environ != NULL && environ[environment_index] != NULL;
       ++environment_index) {
    const char* entry = environ[environment_index];
    const char* separator = strchr(entry, '=');
    size_t name_length;
    size_t exact_index;
    if (separator == NULL) {
      return 0;
    }
    name_length = (size_t)(separator - entry);
    if (name_length >= 3U && memcmp(entry, "LD_", 3U) == 0) {
      return 0;
    }
    if (name_length >= sizeof("CUDA_INJECTION") - 1U &&
        memcmp(entry, "CUDA_INJECTION",
               sizeof("CUDA_INJECTION") - 1U) == 0) {
      return 0;
    }
    for (exact_index = 0;
         exact_index < sizeof(exact_names) / sizeof(exact_names[0]);
         ++exact_index) {
      size_t expected_length = strlen(exact_names[exact_index]);
      if (name_length == expected_length &&
          memcmp(entry, exact_names[exact_index], expected_length) == 0) {
        return 0;
      }
    }
  }
  return 1;
}

int main(void) {
  const char* expected_parent_text = getenv("BURSTSERVE_PARENT_PID");
  const char* expected_closed_fd_text =
      getenv("BURSTSERVE_TEST_EXPECT_CLOSED_FD");
  sigset_t blocked;
  int expected_parent;
  int expected_closed_fd;
  int pdeath_signal = 0;
  int self_fd;
  int seals;

  if (parse_positive_int(expected_parent_text, &expected_parent) != 0 ||
      (pid_t)expected_parent != getppid()) {
    return fail("parent identity did not survive exec");
  }
  if (prctl(PR_GET_PDEATHSIG, &pdeath_signal) != 0 ||
      pdeath_signal != SIGKILL) {
    return fail("SIGKILL parent-death guard did not survive exec");
  }
  if (sigprocmask(SIG_SETMASK, NULL, &blocked) != 0 ||
      sigismember(&blocked, SIGHUP) != 0 ||
      sigismember(&blocked, SIGINT) != 0 ||
      sigismember(&blocked, SIGTERM) != 0) {
    return fail("lifecycle signal mask survived launcher");
  }
  if (!forbidden_environment_is_absent()) {
    return fail("injection environment survived launcher");
  }
  if (getenv("CUDA_VISIBLE_DEVICES") == NULL ||
      strcmp(getenv("CUDA_VISIBLE_DEVICES"), "") != 0 ||
      getenv("CUDA_MPS_PIPE_DIRECTORY") == NULL ||
      strcmp(getenv("CUDA_MPS_PIPE_DIRECTORY"), "") != 0 ||
      getenv("MASK_OFF") == NULL || strcmp(getenv("MASK_OFF"), "17") != 0) {
    return fail("business CUDA/MPS/mask environment was not preserved");
  }
  if (parse_positive_int(expected_closed_fd_text, &expected_closed_fd) != 0) {
    return fail("missing inherited-FD expectation");
  }
  errno = 0;
  if (fcntl(expected_closed_fd, F_GETFD) >= 0 || errno != EBADF) {
    return fail("unrelated inherited descriptor survived exec");
  }
  self_fd = open("/proc/self/exe", O_RDONLY | O_CLOEXEC);
  if (self_fd < 0) {
    return fail("cannot reopen sealed executable");
  }
  seals = fcntl(self_fd, F_GET_SEALS);
  close(self_fd);
  if (seals < 0 ||
      (seals & (F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL)) !=
          (F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL)) {
    return fail("required memfd seals are absent");
  }

  printf(
      "{\"schema_version\":\"burstserve.guard-exec-fixture/v1\","
      "\"status\":\"ok\",\"parent_guard\":%d,\"signals_unblocked\":true,"
      "\"environment_sanitized\":true,\"business_environment_preserved\":"
      "true,\"inherited_fd_closed\":true,\"seals\":%d",
      pdeath_signal, seals);
#ifdef F_SEAL_EXEC
  printf(",\"exec_seal\":%s", (seals & F_SEAL_EXEC) != 0 ? "true" : "false");
#else
  printf(",\"exec_seal\":null");
#endif
  printf("}\n");
  return 0;
}
