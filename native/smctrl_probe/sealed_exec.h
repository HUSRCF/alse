#ifndef BURSTSERVE_SEALED_EXEC_H_
#define BURSTSERVE_SEALED_EXEC_H_

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum burstserve_sealed_exec_fault {
  BURSTSERVE_SEALED_EXEC_FAULT_NONE = 0,
  BURSTSERVE_SEALED_EXEC_FAULT_MEMFD = 1,
  BURSTSERVE_SEALED_EXEC_FAULT_SEAL = 2,
};

struct burstserve_sealed_exec_report {
  uint64_t size_bytes;
  int seals;
  int exec_seal_applied;
};

/*
 * Copy exactly expected_size bytes (and probe for one extra byte) from an
 * already-open regular file into a new executable memfd. The returned memfd
 * is mode 0500, sealed against content/size/execute-bit changes, re-hashed
 * after sealing, and validated as an ELF64 x86-64 executable.
 */
int burstserve_create_sealed_elf_snapshot(
    int source_fd,
    uint64_t expected_size,
    const unsigned char expected_sha256[32],
    enum burstserve_sealed_exec_fault fault,
    int* output_fd,
    struct burstserve_sealed_exec_report* report);

int burstserve_required_seals(void);

#ifdef __cplusplus
}
#endif

#endif  // BURSTSERVE_SEALED_EXEC_H_
