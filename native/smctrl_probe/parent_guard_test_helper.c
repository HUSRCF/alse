#define _GNU_SOURCE

#include "parent_guard.h"
#include "sealed_exec.h"
#include "sha256.h"

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int report_guard_error(
    const struct burstserve_parent_guard_report* report) {
  fprintf(stderr, "%s expected=%ld observed=%ld signal=%d errno=%d\n",
          burstserve_parent_guard_result_name(report->result),
          (long)report->expected_parent_pid,
          (long)report->observed_parent_pid, report->pdeath_signal,
          report->saved_errno);
  return 4;
}

static int print_sha256(const char* path) {
  unsigned char digest[32];
  char hexadecimal[65];
  int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
  if (fd < 0) {
    return 2;
  }
  if (burstserve_sha256_fd(fd, digest) != 0) {
    close(fd);
    return 2;
  }
  close(fd);
  burstserve_sha256_hex(digest, hexadecimal);
  puts(hexadecimal);
  return 0;
}

static int sealed_memfd_self_test(const char* path) {
  struct burstserve_sealed_exec_report report;
  struct stat status;
  unsigned char digest[32];
  unsigned char replacement = 0;
  int source_fd = -1;
  int memfd = -1;
  int write_errno;
  int truncate_errno;

  source_fd =
      open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
  if (source_fd < 0 || fstat(source_fd, &status) != 0 ||
      !S_ISREG(status.st_mode) || status.st_size < 0 ||
      burstserve_sha256_fd(source_fd, digest) != 0 ||
      burstserve_create_sealed_elf_snapshot(
          source_fd, (uint64_t)status.st_size, digest,
          BURSTSERVE_SEALED_EXEC_FAULT_NONE, &memfd, &report) != 0) {
    const int saved_errno = errno;
    if (source_fd >= 0) {
      close(source_fd);
    }
    fprintf(stderr, "sealed self-test setup failed: %s\n",
            strerror(saved_errno));
    return 4;
  }
  close(source_fd);

  errno = 0;
  if (pwrite(memfd, &replacement, 1U, 0) >= 0) {
    close(memfd);
    return 4;
  }
  write_errno = errno;
  errno = 0;
  if (ftruncate(memfd, 0) >= 0) {
    close(memfd);
    return 4;
  }
  truncate_errno = errno;
  close(memfd);
  if (write_errno != EPERM || truncate_errno != EPERM) {
    fprintf(stderr, "sealed self-test unexpected errno: write=%d truncate=%d\n",
            write_errno, truncate_errno);
    return 4;
  }
  printf("sealed size=%llu seals=%d exec_seal=%d write_errno=%d "
         "truncate_errno=%d\n",
         (unsigned long long)report.size_bytes, report.seals,
         report.exec_seal_applied, write_errno, truncate_errno);
  return 0;
}

int main(int argc, char** argv) {
  struct burstserve_parent_guard_report report;
  if (argc == 3 && strcmp(argv[1], "--sha256") == 0) {
    return print_sha256(argv[2]);
  }
  if (argc == 3 && strcmp(argv[1], "--sealed-memfd-self-test") == 0) {
    return sealed_memfd_self_test(argv[2]);
  }
  if (argc != 2) {
    return 2;
  }
  if (strcmp(argv[1], "--sanitize-environment") == 0) {
    if (burstserve_sanitize_loader_environment() != 0) {
      return 4;
    }
    printf("LD_PRELOAD=%s\n", getenv("LD_PRELOAD") == NULL ? "absent" : "set");
    printf("LD_AUDIT=%s\n", getenv("LD_AUDIT") == NULL ? "absent" : "set");
    printf("LD_LIBRARY_PATH=%s\n",
           getenv("LD_LIBRARY_PATH") == NULL ? "absent" : "set");
    printf("GLIBC_TUNABLES=%s\n",
           getenv("GLIBC_TUNABLES") == NULL ? "absent" : "set");
    printf("GCONV_PATH=%s\n",
           getenv("GCONV_PATH") == NULL ? "absent" : "set");
    printf("LOCPATH=%s\n",
           getenv("LOCPATH") == NULL ? "absent" : "set");
    printf("NLSPATH=%s\n",
           getenv("NLSPATH") == NULL ? "absent" : "set");
    printf("CUDA_INJECTION32_PATH=%s\n",
           getenv("CUDA_INJECTION32_PATH") == NULL ? "absent" : "set");
    printf("CUDA_INJECTION64_PATH=%s\n",
           getenv("CUDA_INJECTION64_PATH") == NULL ? "absent" : "set");
    printf("CUDA_VISIBLE_DEVICES=%s\n",
           getenv("CUDA_VISIBLE_DEVICES") == NULL
               ? "absent"
               : getenv("CUDA_VISIBLE_DEVICES"));
    return 0;
  }
  if (strcmp(argv[1], "--arm-and-exit") != 0 &&
      strcmp(argv[1], "--arm-and-wait") != 0) {
    return 2;
  }
  if (burstserve_arm_parent_guard("BURSTSERVE_PARENT_PID", &report) !=
      BURSTSERVE_PARENT_GUARD_OK) {
    return report_guard_error(&report);
  }
  if (burstserve_sanitize_loader_environment() != 0) {
    return 4;
  }
  printf("armed pid=%ld parent=%ld signal=%d cuda=%s\n", (long)getpid(),
         (long)getppid(), report.pdeath_signal,
         getenv("CUDA_VISIBLE_DEVICES") == NULL
             ? "absent"
             : getenv("CUDA_VISIBLE_DEVICES"));
  fflush(stdout);
  if (strcmp(argv[1], "--arm-and-exit") == 0) {
    return 0;
  }
  for (;;) {
    pause();
  }
}
