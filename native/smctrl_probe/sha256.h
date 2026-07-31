#ifndef BURSTSERVE_SHA256_H_
#define BURSTSERVE_SHA256_H_

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

struct burstserve_sha256_context {
  uint32_t state[8];
  uint64_t total_bytes;
  unsigned char buffer[64];
  size_t buffered_bytes;
};

void burstserve_sha256_init(struct burstserve_sha256_context* context);
void burstserve_sha256_update(struct burstserve_sha256_context* context,
                              const void* data, size_t length);
void burstserve_sha256_final(struct burstserve_sha256_context* context,
                             unsigned char digest[32]);
int burstserve_sha256_fd(int fd, unsigned char digest[32]);
void burstserve_sha256_hex(const unsigned char digest[32], char output[65]);

#ifdef __cplusplus
}
#endif

#endif  // BURSTSERVE_SHA256_H_
