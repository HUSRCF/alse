#define _GNU_SOURCE

#include "parent_guard.h"
#include "sealed_exec.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <unistd.h>

#ifndef BURSTSERVE_REAL_PROBE_SHA256
#error "BURSTSERVE_REAL_PROBE_SHA256 must be supplied by the identity header"
#endif

#ifndef BURSTSERVE_REAL_PROBE_SIZE
#error "BURSTSERVE_REAL_PROBE_SIZE must be supplied by the identity header"
#endif

#ifndef BURSTSERVE_TEST_FAULT_INJECTION
#define BURSTSERVE_TEST_FAULT_INJECTION 0
#endif

extern char** environ;

enum launch_mode {
  LAUNCH_MODE_BASELINE = 0,
  LAUNCH_MODE_MASKED = 1,
  LAUNCH_MODE_INVALID = 2,
};

static enum launch_mode requested_mode(int argc, char** argv) {
  enum launch_mode result = LAUNCH_MODE_BASELINE;
  int index;
  for (index = 1; index < argc; ++index) {
    if (strcmp(argv[index], "--mode") != 0) {
      continue;
    }
    if (index + 1 >= argc) {
      return LAUNCH_MODE_INVALID;
    }
    ++index;
    if (strcmp(argv[index], "baseline") == 0) {
      result = LAUNCH_MODE_BASELINE;
    } else if (strcmp(argv[index], "global") == 0 ||
               strcmp(argv[index], "next") == 0 ||
               strcmp(argv[index], "stream") == 0) {
      result = LAUNCH_MODE_MASKED;
    } else {
      return LAUNCH_MODE_INVALID;
    }
  }
  return result;
}

static int sibling_real_probe_path(char output[PATH_MAX]) {
  static const char suffix[] = ".real";
  ssize_t length = readlink("/proc/self/exe", output, PATH_MAX);
  if (length < 0) {
    return -1;
  }
  if ((size_t)length + sizeof(suffix) > PATH_MAX) {
    errno = ENAMETOOLONG;
    return -1;
  }
  memcpy(output + length, suffix, sizeof(suffix));
  return 0;
}

static int fail_guard(
    const struct burstserve_parent_guard_report* report) {
  const char* name = burstserve_parent_guard_result_name(report->result);
  if (report->result == BURSTSERVE_PARENT_GUARD_PRCTL_FAILED) {
    fprintf(stderr, "smid_probe launcher: parent guard %s: %s\n", name,
            strerror(report->saved_errno));
  } else {
    fprintf(stderr,
            "smid_probe launcher: parent guard %s "
            "(expected=%ld observed=%ld)\n",
            name, (long)report->expected_parent_pid,
            (long)report->observed_parent_pid);
  }
  return 4;
}

static int hexadecimal_digit(unsigned char value) {
  if (value >= (unsigned char)'0' && value <= (unsigned char)'9') {
    return (int)(value - (unsigned char)'0');
  }
  if (value >= (unsigned char)'a' && value <= (unsigned char)'f') {
    return (int)(value - (unsigned char)'a') + 10;
  }
  return -1;
}

static int expected_sha256(unsigned char output[32]) {
  const char* encoded = BURSTSERVE_REAL_PROBE_SHA256;
  size_t index;
  if (strlen(encoded) != 64U) {
    errno = EINVAL;
    return -1;
  }
  for (index = 0; index < 32U; ++index) {
    int high = hexadecimal_digit((unsigned char)encoded[index * 2U]);
    int low = hexadecimal_digit((unsigned char)encoded[index * 2U + 1U]);
    if (high < 0 || low < 0) {
      errno = EINVAL;
      return -1;
    }
    output[index] = (unsigned char)((high << 4) | low);
  }
  return 0;
}

static int validate_source_fd(int fd) {
  struct stat status;
  unsigned char capability;
  ssize_t capability_size;

  if (fstat(fd, &status) != 0) {
    return -1;
  }
  if (!S_ISREG(status.st_mode) || status.st_uid != geteuid() ||
      status.st_nlink != 1 || (status.st_mode & 07777) != 0500 ||
      status.st_size < 0 ||
      (uint64_t)status.st_size != (uint64_t)BURSTSERVE_REAL_PROBE_SIZE) {
    errno = EPERM;
    return -1;
  }
  errno = 0;
  capability_size =
      fgetxattr(fd, "security.capability", &capability, sizeof(capability));
  if (capability_size >= 0) {
    errno = EPERM;
    return -1;
  }
  if (errno != ENODATA && errno != ENOTSUP && errno != EOPNOTSUPP) {
    return -1;
  }
  return 0;
}

static int pin_exec_fd_at_three(int memfd) {
  int descriptor_flags;
  if (memfd != 3) {
    if (dup3(memfd, 3, O_CLOEXEC) != 3) {
      return -1;
    }
    close(memfd);
  } else {
    descriptor_flags = fcntl(memfd, F_GETFD);
    if (descriptor_flags < 0 ||
        fcntl(memfd, F_SETFD, descriptor_flags | FD_CLOEXEC) != 0) {
      return -1;
    }
  }
#if defined(SYS_close_range)
  if (syscall(SYS_close_range, 4U, UINT_MAX, 0U) != 0) {
    return -1;
  }
#else
  errno = ENOSYS;
  return -1;
#endif
  return 3;
}

#if BURSTSERVE_TEST_FAULT_INJECTION
static int parse_test_fd(const char* text, int* output) {
  char* end = NULL;
  long parsed;
  if (text == NULL || *text == '\0') {
    return -1;
  }
  errno = 0;
  parsed = strtol(text, &end, 10);
  if (errno != 0 || end == text || *end != '\0' || parsed < 3 ||
      parsed > INT_MAX) {
    errno = EINVAL;
    return -1;
  }
  *output = (int)parsed;
  return 0;
}

static enum burstserve_sealed_exec_fault requested_test_fault(
    int* fail_execveat) {
  const char* value = getenv("BURSTSERVE_TEST_SEALED_EXEC_FAULT");
  *fail_execveat = 0;
  if (value == NULL || *value == '\0') {
    return BURSTSERVE_SEALED_EXEC_FAULT_NONE;
  }
  if (strcmp(value, "memfd") == 0) {
    return BURSTSERVE_SEALED_EXEC_FAULT_MEMFD;
  }
  if (strcmp(value, "seal") == 0) {
    return BURSTSERVE_SEALED_EXEC_FAULT_SEAL;
  }
  if (strcmp(value, "execveat") == 0) {
    *fail_execveat = 1;
    return BURSTSERVE_SEALED_EXEC_FAULT_NONE;
  }
  errno = EINVAL;
  return (enum burstserve_sealed_exec_fault)-1;
}

static int test_pause_before_exec(void) {
  const char* ready_text = getenv("BURSTSERVE_TEST_READY_FD");
  const char* continue_text = getenv("BURSTSERVE_TEST_CONTINUE_FD");
  unsigned char byte = 0;
  int ready_fd;
  int continue_fd;
  ssize_t result;

  if (ready_text == NULL && continue_text == NULL) {
    return 0;
  }
  if (parse_test_fd(ready_text, &ready_fd) != 0 ||
      parse_test_fd(continue_text, &continue_fd) != 0) {
    return -1;
  }
  do {
    result = write(ready_fd, "R", 1U);
  } while (result < 0 && errno == EINTR);
  if (result != 1) {
    if (result >= 0) {
      errno = EIO;
    }
    return -1;
  }
  do {
    result = read(continue_fd, &byte, 1U);
  } while (result < 0 && errno == EINTR);
  if (result != 1 || byte != (unsigned char)'C') {
    if (result >= 0) {
      errno = EIO;
    }
    return -1;
  }
  return 0;
}
#endif

int main(int argc, char** argv) {
  enum launch_mode mode = requested_mode(argc, argv);
  enum burstserve_sealed_exec_fault fault =
      BURSTSERVE_SEALED_EXEC_FAULT_NONE;
  struct burstserve_parent_guard_report guard_report;
  struct burstserve_sealed_exec_report snapshot_report;
  unsigned char expected_digest[32];
  char real_path[PATH_MAX];
  int real_fd = -1;
  int memfd = -1;
  int exec_fd;
#if BURSTSERVE_TEST_FAULT_INJECTION
  int fail_execveat = 0;
#endif

  if (mode == LAUNCH_MODE_INVALID) {
    fprintf(stderr, "smid_probe launcher: invalid or missing --mode value\n");
    return 2;
  }
  if (mode == LAUNCH_MODE_MASKED &&
      burstserve_arm_parent_guard("BURSTSERVE_PARENT_PID", &guard_report) !=
          BURSTSERVE_PARENT_GUARD_OK) {
    return fail_guard(&guard_report);
  }
  if (burstserve_restore_lifecycle_signals() != 0) {
    fprintf(stderr,
            "smid_probe launcher: cannot restore lifecycle signals: %s\n",
            strerror(errno));
    return 4;
  }
  if (burstserve_sanitize_loader_environment() != 0) {
    fprintf(stderr,
            "smid_probe launcher: loader environment cleanup failed: %s\n",
            strerror(errno));
    return 4;
  }
#if BURSTSERVE_TEST_FAULT_INJECTION
  fault = requested_test_fault(&fail_execveat);
  if ((int)fault < 0) {
    fprintf(stderr, "smid_probe launcher: invalid test fault request\n");
    return 4;
  }
#endif
  if (sibling_real_probe_path(real_path) != 0 ||
      expected_sha256(expected_digest) != 0) {
    fprintf(stderr, "smid_probe launcher: invalid real-probe identity: %s\n",
            strerror(errno));
    return 4;
  }
  real_fd =
      open(real_path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
  if (real_fd < 0) {
    fprintf(stderr, "smid_probe launcher: cannot open real probe: %s\n",
            strerror(errno));
    return 4;
  }
  if (validate_source_fd(real_fd) != 0 ||
      burstserve_create_sealed_elf_snapshot(
          real_fd, (uint64_t)BURSTSERVE_REAL_PROBE_SIZE, expected_digest,
          fault, &memfd, &snapshot_report) != 0) {
    const int saved_errno = errno;
    close(real_fd);
    fprintf(stderr,
            "smid_probe launcher: sealed executable snapshot rejected: %s\n",
            strerror(saved_errno));
    return 4;
  }
  close(real_fd);

#if BURSTSERVE_TEST_FAULT_INJECTION
  if (test_pause_before_exec() != 0) {
    const int saved_errno = errno;
    close(memfd);
    fprintf(stderr, "smid_probe launcher: test synchronization failed: %s\n",
            strerror(saved_errno));
    return 4;
  }
#endif

  exec_fd = pin_exec_fd_at_three(memfd);
  if (exec_fd < 0) {
    const int saved_errno = errno;
    close(memfd);
    fprintf(stderr, "smid_probe launcher: inherited-FD cleanup failed: %s\n",
            strerror(saved_errno));
    return 4;
  }
  (void)snapshot_report;
#if BURSTSERVE_TEST_FAULT_INJECTION
  if (fail_execveat != 0) {
    errno = ENOSYS;
    fprintf(stderr, "smid_probe launcher: execveat failed closed: %s\n",
            strerror(errno));
    return 4;
  }
#endif
#if defined(SYS_execveat)
  syscall(SYS_execveat, exec_fd, "", argv, environ, AT_EMPTY_PATH);
#else
  errno = ENOSYS;
#endif
  fprintf(stderr, "smid_probe launcher: execveat failed closed: %s\n",
          strerror(errno));
  return 4;
}
