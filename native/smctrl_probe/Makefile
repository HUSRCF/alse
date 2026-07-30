# Native Gate-A SM-ID probe.
#
# This Makefile intentionally keeps every generated file outside the source
# directory.  Override CUDA_HOME, BUILD_DIR, or CUDA_ARCH when needed:
#
#   make CUDA_HOME=/opt/cuda CUDA_ARCH=90
#
SOURCE_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
REPO_ROOT := $(abspath $(SOURCE_DIR)/../..)
CUDA_HOME ?= /usr/local/cuda-13.3
BUILD_DIR ?= $(REPO_ROOT)/build/smctrl_probe
LIBSMCTRL_DIR ?= $(REPO_ROOT)/vendor/libsmctrl

NVCC ?= $(CUDA_HOME)/bin/nvcc
CC ?= cc
AR ?= ar
CUDA_ARCH ?= 89

TARGET := $(BUILD_DIR)/smid_probe
PROBE_OBJECT := $(BUILD_DIR)/smid_probe.o
LIBSMCTRL_OBJECT := $(BUILD_DIR)/libsmctrl.o
LIBSMCTRL_ARCHIVE := $(BUILD_DIR)/libsmctrl.a
CONFIG_STAMP := $(BUILD_DIR)/build-config.stamp

CPPFLAGS := -I$(CUDA_HOME)/include -I$(LIBSMCTRL_DIR)
CFLAGS ?= -O2 -g
CFLAGS += -std=gnu11 -fPIC -Wall -Wextra
NVCCFLAGS ?= -O3 -lineinfo
NVCCFLAGS += -std=c++17 \
	--generate-code=arch=compute_$(CUDA_ARCH),code=sm_$(CUDA_ARCH) \
	--generate-code=arch=compute_$(CUDA_ARCH),code=compute_$(CUDA_ARCH)
LDLIBS := -L$(CUDA_HOME)/lib64 -lcudart -lcuda

.PHONY: all clean FORCE

all: $(TARGET)

$(BUILD_DIR):
	mkdir -p $@

$(CONFIG_STAMP): FORCE | $(BUILD_DIR)
	@tmp='$@.tmp.$$$$'; \
	{ \
		printf '%s\n' \
			'CUDA_HOME=$(abspath $(CUDA_HOME))' \
			'CUDA_ARCH=$(CUDA_ARCH)' \
			'NVCC=$(NVCC)' \
			'CC=$(CC)' \
			'AR=$(AR)' \
			'CPPFLAGS=$(CPPFLAGS)' \
			'CFLAGS=$(CFLAGS)' \
			'NVCCFLAGS=$(NVCCFLAGS)' \
			'LDLIBS=$(LDLIBS)' \
			'LIBSMCTRL_DIR=$(abspath $(LIBSMCTRL_DIR))'; \
	} > "$$tmp"; \
	if test ! -f '$@' || ! cmp -s "$$tmp" '$@'; then \
		mv -f "$$tmp" '$@'; \
	else \
		rm -f "$$tmp"; \
	fi

FORCE:

$(LIBSMCTRL_OBJECT): $(LIBSMCTRL_DIR)/libsmctrl.c \
		$(LIBSMCTRL_DIR)/libsmctrl.h $(CONFIG_STAMP) \
		$(SOURCE_DIR)/Makefile | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

$(LIBSMCTRL_ARCHIVE): $(LIBSMCTRL_OBJECT)
	$(AR) rcs $@ $<

$(PROBE_OBJECT): $(SOURCE_DIR)/smid_probe.cu $(LIBSMCTRL_DIR)/libsmctrl.h \
		$(CONFIG_STAMP) $(SOURCE_DIR)/Makefile | $(BUILD_DIR)
	$(NVCC) $(CPPFLAGS) $(NVCCFLAGS) -c $< -o $@

$(TARGET): $(PROBE_OBJECT) $(LIBSMCTRL_ARCHIVE)
	$(NVCC) $(NVCCFLAGS) $^ $(LDLIBS) -o $@

clean:
	@target="$$(realpath -m -- '$(BUILD_DIR)')"; \
	safe_root="$$(realpath -m -- '$(REPO_ROOT)/build')"; \
	case "$$target" in \
		"$$safe_root"/*) ;; \
		*) \
			echo "Refusing to clean unsafe BUILD_DIR: $$target" >&2; \
			exit 2; \
			;; \
	esac; \
	$(RM) -r -- "$$target"
