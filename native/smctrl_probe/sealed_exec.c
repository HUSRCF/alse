#define _GNU_SOURCE

#include "sealed_exec.h"

#include "sha256.h"

#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/memfd.h>
#include <stdint.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef MFD_EXEC
#define MFD_EXEC 0x0010U
#endif

static int create_executable_memfd(void) {
#if defined(SYS_memfd_create)
  return (int)syscall(SYS_memfd_create, "burstserve-smctrl-probe",
                      MFD_CLOEXEC | MFD_ALLOW_SEALING | MFD_EXEC);
#else
  errno = ENOSYS;
  return -1;
#endif
}

static int write_all(int fd, const unsigned char* data, size_t length) {
  while (length > 0U) {
    ssize_t written = write(fd, data, length);
    if (written > 0) {
      data += (size_t)written;
      length -= (size_t)written;
      continue;
    }
    if (written < 0 && errno == EINTR) {
      continue;
    }
    if (written == 0) {
      errno = EIO;
    }
    return -1;
  }
  return 0;
}

static int copy_exact_with_overflow_probe(int source_fd,
                                          int destination_fd,
                                          uint64_t expected_size) {
  unsigned char buffer[16384];
  uint64_t copied = 0;

  if (expected_size > (uint64_t)INT64_MAX) {
    errno = EFBIG;
    return -1;
  }
  if (lseek(source_fd, 0, SEEK_SET) < 0) {
    return -1;
  }
  for (;;) {
    uint64_t remaining = expected_size - copied;
    size_t request =
        remaining >= sizeof(buffer) ? sizeof(buffer) : (size_t)remaining + 1U;
    ssize_t count;

    count = read(source_fd, buffer, request);
    if (count > 0) {
      if ((uint64_t)count > remaining) {
        errno = EFBIG;
        return -1;
      }
      if (write_all(destination_fd, buffer, (size_t)count) != 0) {
        return -1;
      }
      copied += (uint64_t)count;
      continue;
    }
    if (count == 0) {
      break;
    }
    if (errno == EINTR) {
      continue;
    }
    return -1;
  }
  if (copied != expected_size) {
    errno = EIO;
    return -1;
  }
  return 0;
}

int burstserve_required_seals(void) {
  return F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL;
}

static int apply_and_verify_seals(
    int fd,
    enum burstserve_sealed_exec_fault fault,
    struct burstserve_sealed_exec_report* report) {
  int seals;
  int exec_seal_applied = 0;

  if (fault == BURSTSERVE_SEALED_EXEC_FAULT_SEAL) {
    errno = EPERM;
    return -1;
  }
  if (fcntl(fd, F_ADD_SEALS,
            F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK) != 0) {
    return -1;
  }
#ifdef F_SEAL_EXEC
  if (fcntl(fd, F_ADD_SEALS, F_SEAL_EXEC) == 0) {
    exec_seal_applied = 1;
  } else if (errno != EINVAL) {
    return -1;
  }
#endif
  if (fcntl(fd, F_ADD_SEALS, F_SEAL_SEAL) != 0) {
    return -1;
  }
  seals = fcntl(fd, F_GET_SEALS);
  if (seals < 0) {
    return -1;
  }
  if ((seals & burstserve_required_seals()) !=
      burstserve_required_seals()) {
    errno = EPERM;
    return -1;
  }
#ifdef F_SEAL_EXEC
  if (exec_seal_applied != 0 && (seals & F_SEAL_EXEC) == 0) {
    errno = EPERM;
    return -1;
  }
#endif
  report->seals = seals;
  report->exec_seal_applied = exec_seal_applied;
  return 0;
}

static int validate_sealed_metadata(int fd, uint64_t expected_size) {
  struct stat status;
  if (fstat(fd, &status) != 0) {
    return -1;
  }
  if (!S_ISREG(status.st_mode) || (status.st_mode & 07777) != 0500 ||
      status.st_size < 0 || (uint64_t)status.st_size != expected_size) {
    errno = EPERM;
    return -1;
  }
  return 0;
}

static int validate_elf64_x86_64(int fd) {
  Elf64_Ehdr header;
  ssize_t count;

  do {
    count = pread(fd, &header, sizeof(header), 0);
  } while (count < 0 && errno == EINTR);
  if (count != (ssize_t)sizeof(header)) {
    if (count >= 0) {
      errno = ENOEXEC;
    }
    return -1;
  }
  if (memcmp(header.e_ident, ELFMAG, SELFMAG) != 0 ||
      header.e_ident[EI_CLASS] != ELFCLASS64 ||
      header.e_ident[EI_DATA] != ELFDATA2LSB ||
      header.e_ident[EI_VERSION] != EV_CURRENT ||
      (header.e_type != ET_EXEC && header.e_type != ET_DYN) ||
      header.e_machine != EM_X86_64 || header.e_version != EV_CURRENT ||
      header.e_ehsize != sizeof(Elf64_Ehdr)) {
    errno = ENOEXEC;
    return -1;
  }
  return 0;
}

int burstserve_create_sealed_elf_snapshot(
    int source_fd,
    uint64_t expected_size,
    const unsigned char expected_sha256[32],
    enum burstserve_sealed_exec_fault fault,
    int* output_fd,
    struct burstserve_sealed_exec_report* report) {
  struct burstserve_sealed_exec_report local_report;
  unsigned char observed_sha256[32];
  int memfd = -1;

  if (source_fd < 0 || expected_sha256 == NULL || output_fd == NULL ||
      report == NULL) {
    errno = EINVAL;
    return -1;
  }
  memset(&local_report, 0, sizeof(local_report));
  *output_fd = -1;

  if (fault == BURSTSERVE_SEALED_EXEC_FAULT_MEMFD) {
    errno = EACCES;
    return -1;
  }
  memfd = create_executable_memfd();
  /*
   * There is intentionally no pathname execution fallback and no implicit
   * retry without MFD_EXEC. ENOSYS, EACCES, EINVAL, and every other failure
   * fail closed before the dynamic CUDA probe is loaded.
   */
  if (memfd < 0) {
    return -1;
  }
  if (copy_exact_with_overflow_probe(source_fd, memfd, expected_size) != 0 ||
      fchmod(memfd, 0500) != 0 ||
      apply_and_verify_seals(memfd, fault, &local_report) != 0 ||
      validate_sealed_metadata(memfd, expected_size) != 0 ||
      burstserve_sha256_fd(memfd, observed_sha256) != 0) {
    int saved_errno = errno;
    close(memfd);
    errno = saved_errno;
    return -1;
  }
  if (memcmp(observed_sha256, expected_sha256, 32U) != 0) {
    close(memfd);
    errno = EBADMSG;
    return -1;
  }
  if (validate_elf64_x86_64(memfd) != 0) {
    int saved_errno = errno;
    close(memfd);
    errno = saved_errno;
    return -1;
  }
  local_report.size_bytes = expected_size;
  *report = local_report;
  *output_fd = memfd;
  return 0;
}
