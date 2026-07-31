#include "sha256.h"

#include <errno.h>
#include <string.h>
#include <unistd.h>

static const uint32_t k_round_constants[64] = {
    0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U, 0x3956c25bU,
    0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U, 0xd807aa98U, 0x12835b01U,
    0x243185beU, 0x550c7dc3U, 0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U,
    0xc19bf174U, 0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
    0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU, 0x983e5152U,
    0xa831c66dU, 0xb00327c8U, 0xbf597fc7U, 0xc6e00bf3U, 0xd5a79147U,
    0x06ca6351U, 0x14292967U, 0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU,
    0x53380d13U, 0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
    0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U, 0xd192e819U,
    0xd6990624U, 0xf40e3585U, 0x106aa070U, 0x19a4c116U, 0x1e376c08U,
    0x2748774cU, 0x34b0bcb5U, 0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU,
    0x682e6ff3U, 0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
    0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U,
};

static uint32_t rotate_right(uint32_t value, unsigned int bits) {
  return (value >> bits) | (value << (32U - bits));
}

static uint32_t load_be32(const unsigned char* input) {
  return ((uint32_t)input[0] << 24U) | ((uint32_t)input[1] << 16U) |
         ((uint32_t)input[2] << 8U) | (uint32_t)input[3];
}

static void store_be32(unsigned char* output, uint32_t value) {
  output[0] = (unsigned char)(value >> 24U);
  output[1] = (unsigned char)(value >> 16U);
  output[2] = (unsigned char)(value >> 8U);
  output[3] = (unsigned char)value;
}

static void transform(struct burstserve_sha256_context* context,
                      const unsigned char block[64]) {
  uint32_t words[64];
  uint32_t a;
  uint32_t b;
  uint32_t c;
  uint32_t d;
  uint32_t e;
  uint32_t f;
  uint32_t g;
  uint32_t h;
  unsigned int index;

  for (index = 0; index < 16U; ++index) {
    words[index] = load_be32(block + index * 4U);
  }
  for (index = 16U; index < 64U; ++index) {
    const uint32_t s0 = rotate_right(words[index - 15U], 7U) ^
                        rotate_right(words[index - 15U], 18U) ^
                        (words[index - 15U] >> 3U);
    const uint32_t s1 = rotate_right(words[index - 2U], 17U) ^
                        rotate_right(words[index - 2U], 19U) ^
                        (words[index - 2U] >> 10U);
    words[index] =
        words[index - 16U] + s0 + words[index - 7U] + s1;
  }

  a = context->state[0];
  b = context->state[1];
  c = context->state[2];
  d = context->state[3];
  e = context->state[4];
  f = context->state[5];
  g = context->state[6];
  h = context->state[7];

  for (index = 0; index < 64U; ++index) {
    const uint32_t sum1 = rotate_right(e, 6U) ^ rotate_right(e, 11U) ^
                          rotate_right(e, 25U);
    const uint32_t choose = (e & f) ^ ((~e) & g);
    const uint32_t temporary1 =
        h + sum1 + choose + k_round_constants[index] + words[index];
    const uint32_t sum0 = rotate_right(a, 2U) ^ rotate_right(a, 13U) ^
                          rotate_right(a, 22U);
    const uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
    const uint32_t temporary2 = sum0 + majority;
    h = g;
    g = f;
    f = e;
    e = d + temporary1;
    d = c;
    c = b;
    b = a;
    a = temporary1 + temporary2;
  }

  context->state[0] += a;
  context->state[1] += b;
  context->state[2] += c;
  context->state[3] += d;
  context->state[4] += e;
  context->state[5] += f;
  context->state[6] += g;
  context->state[7] += h;
}

void burstserve_sha256_init(struct burstserve_sha256_context* context) {
  static const uint32_t initial_state[8] = {
      0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
      0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U,
  };
  memcpy(context->state, initial_state, sizeof(initial_state));
  context->total_bytes = 0;
  context->buffered_bytes = 0;
}

void burstserve_sha256_update(struct burstserve_sha256_context* context,
                              const void* data, size_t length) {
  const unsigned char* cursor = (const unsigned char*)data;
  context->total_bytes += (uint64_t)length;
  while (length > 0) {
    size_t available = 64U - context->buffered_bytes;
    size_t take = length < available ? length : available;
    memcpy(context->buffer + context->buffered_bytes, cursor, take);
    context->buffered_bytes += take;
    cursor += take;
    length -= take;
    if (context->buffered_bytes == 64U) {
      transform(context, context->buffer);
      context->buffered_bytes = 0;
    }
  }
}

void burstserve_sha256_final(struct burstserve_sha256_context* context,
                             unsigned char digest[32]) {
  const uint64_t bit_length = context->total_bytes * UINT64_C(8);
  unsigned int index;
  context->buffer[context->buffered_bytes++] = 0x80U;
  if (context->buffered_bytes > 56U) {
    memset(context->buffer + context->buffered_bytes, 0,
           64U - context->buffered_bytes);
    transform(context, context->buffer);
    context->buffered_bytes = 0;
  }
  memset(context->buffer + context->buffered_bytes, 0,
         56U - context->buffered_bytes);
  for (index = 0; index < 8U; ++index) {
    context->buffer[63U - index] =
        (unsigned char)(bit_length >> (index * 8U));
  }
  transform(context, context->buffer);
  for (index = 0; index < 8U; ++index) {
    store_be32(digest + index * 4U, context->state[index]);
  }
  memset(context, 0, sizeof(*context));
}

int burstserve_sha256_fd(int fd, unsigned char digest[32]) {
  struct burstserve_sha256_context context;
  unsigned char buffer[16384];
  ssize_t count;
  if (lseek(fd, 0, SEEK_SET) < 0) {
    return -1;
  }
  burstserve_sha256_init(&context);
  for (;;) {
    count = read(fd, buffer, sizeof(buffer));
    if (count > 0) {
      burstserve_sha256_update(&context, buffer, (size_t)count);
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
  burstserve_sha256_final(&context, digest);
  return 0;
}

void burstserve_sha256_hex(const unsigned char digest[32], char output[65]) {
  static const char hexadecimal[] = "0123456789abcdef";
  size_t index;
  for (index = 0; index < 32U; ++index) {
    output[index * 2U] = hexadecimal[digest[index] >> 4U];
    output[index * 2U + 1U] = hexadecimal[digest[index] & 0x0fU];
  }
  output[64] = '\0';
}
