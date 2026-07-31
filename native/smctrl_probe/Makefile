# Native Gate-A SM-ID probe.
#
# Every formal entry point acquires the same private runtime lock and passes
# the locked open-file description to all internal/leaf targets. Native
# outputs are always rebuilt into same-directory temporary files and atomically
# renamed only after successful compilation/linking and permission hardening.

# This must remain the first non-comment directive.  GNU make imports ambient
# environment variables as individually exported variables; bare `unexport`
# does not clear that inherited export status.  Expanding `.VARIABLES` here
# marks every variable known at process entry (including BASH_ENV, loader
# variables, and imported BASH_FUNC_* records) as unexported before the first
# `$(shell ...)` or recipe shell can start.  Necessary child environments are
# reconstructed explicitly with the env-i command lines below.
unexport $(.VARIABLES)

override SHELL := /bin/bash
override .SHELLFLAGS := -p -eu -o pipefail -c
override .DEFAULT_GOAL := all
.DELETE_ON_ERROR:

# Independently prove that GNU make honored Bash privileged mode for its very
# first shell expansion.  `-p` suppresses BASH_ENV, SHELLOPTS/BASHOPTS, and
# exported-function startup processing even if a future edit accidentally
# re-exports one of them.
override MAKE_SHELL_MODE_DIAGNOSTIC := $(shell \
	if [[ "$$-" == *p* ]]; then \
		/usr/bin/printf '%s' privileged; \
	else \
		exit 2; \
	fi)
ifneq ($(MAKE_SHELL_MODE_DIAGNOSTIC):$(.SHELLSTATUS),privileged:0)
$(error GNU make shell did not enter required Bash privileged mode)
endif

# GNU make canonicalizes the long spellings of dry-run, touch,
# ignore-errors, and question mode to n/t/i/q in the leading short-option
# word of MAKEFLAGS. Reject them while parsing: a recipe-based check is itself
# bypassed by -n/-t and its error can be ignored by -i. Also reject any
# command-line MAKEFLAGS assignment, which could otherwise hide an already
# active control mode (for example, "-n MAKEFLAGS=").
ifneq ($(origin MAKEFLAGS),file)
$(error command-line or override MAKEFLAGS assignments are forbidden)
endif
ifneq ($(strip $(foreach flag,n t i q,$(if \
	$(findstring $(flag),$(if $(filter -%,$(firstword $(MAKEFLAGS))),,\
	$(firstword $(MAKEFLAGS)))),$(flag)))),)
$(error forbidden GNU make control mode in MAKEFLAGS: $(MAKEFLAGS))
endif

override SOURCE_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
override REPO_ROOT := $(abspath $(SOURCE_DIR)/../..)
override FORMAL_MAKEFILE := $(SOURCE_DIR)/Makefile
override ATTESTATION_SCRIPT := $(SOURCE_DIR)/build_attestation.py

ifneq ($(words $(MAKEFILE_LIST)),1)
$(error formal GNU make invocation loaded unexpected makefiles: $(MAKEFILE_LIST))
endif
ifneq ($(realpath $(firstword $(MAKEFILE_LIST))),$(realpath $(FORMAL_MAKEFILE)))
$(error formal GNU make invocation did not load the pinned Makefile)
endif

# Inspect the actual /usr/bin/make ancestor through /proc. This closes the
# gap where --eval or a preloaded makefile resets MAKEFLAGS/MAKEFILE_LIST after
# injecting .IGNORE, dependencies, or recipes. The helper itself runs isolated
# and any nonzero result becomes a parse error, which .IGNORE cannot suppress.
override MAKE_PARSE_GUARD_DIAGNOSTIC := $(shell \
	/usr/bin/env -i PATH='/usr/bin:/bin' LANG=C LC_ALL=C TZ=UTC \
	PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
	/usr/bin/python3 -I -S -B '$(ATTESTATION_SCRIPT)' make-parse-guard \
	--source-dir '$(SOURCE_DIR)' \
	--makefile '$(FORMAL_MAKEFILE)' 2>&1)
ifneq ($(.SHELLSTATUS),0)
$(error formal GNU make invocation rejected: $(MAKE_PARSE_GUARD_DIAGNOSTIC))
endif

override CUDA_HOME := /usr/local/cuda-13.3
override BUILD_ROOT := $(REPO_ROOT)/build
override BUILD_DIR := $(BUILD_ROOT)/smctrl_probe
override LIBSMCTRL_DIR := $(REPO_ROOT)/vendor/libsmctrl

override MAKE := /usr/bin/make
override NVCC := $(CUDA_HOME)/bin/nvcc
override CC := /usr/bin/cc
override AR := /usr/bin/ar
override PYTHON := /usr/bin/python3
override ENV := /usr/bin/env
override FLOCK := /usr/bin/flock
override INSTALL := /usr/bin/install
override STAT := /usr/bin/stat
override READLINK := /usr/bin/readlink
override REALPATH := /usr/bin/realpath
override RM := /usr/bin/rm
override RMDIR := /usr/bin/rmdir
override MV := /usr/bin/mv
override CHMOD := /usr/bin/chmod
override CUDA_ARCH := 89

override BUILD_EUID := $(shell /usr/bin/id -u)
override RUNTIME_USER_DIR := /run/user/$(BUILD_EUID)
override BUILD_LOCK_DIR := $(RUNTIME_USER_DIR)/burstserve-smctrl-probe
override BUILD_LOCK := $(BUILD_LOCK_DIR)/build.lock
override LOCK_FD := 9
override BURSTSERVE_BUILD_LOCK_PATH := $(BUILD_LOCK)
override BURSTSERVE_BUILD_LOCK_FD := $(LOCK_FD)

override TARGET := $(BUILD_DIR)/smid_probe
override REAL_TARGET := $(BUILD_DIR)/smid_probe.real
override PROBE_OBJECT := $(BUILD_DIR)/smid_probe.o
override LIBSMCTRL_OBJECT := $(BUILD_DIR)/libsmctrl.o
override LIBSMCTRL_ARCHIVE := $(BUILD_DIR)/libsmctrl.a
override LAUNCHER_OBJECT := $(BUILD_DIR)/guard_launcher.o
override PARENT_GUARD_OBJECT := $(BUILD_DIR)/parent_guard.o
override SEALED_EXEC_OBJECT := $(BUILD_DIR)/sealed_exec.o
override SHA256_OBJECT := $(BUILD_DIR)/sha256.o
override TEST_HELPER_OBJECT := $(BUILD_DIR)/parent_guard_test_helper.o
override TEST_HELPER := $(BUILD_DIR)/parent_guard_test_helper
override REAL_IDENTITY_HEADER := $(BUILD_DIR)/real_probe_identity.h

override EXEC_TEST_LAUNCHER := $(BUILD_DIR)/guard_exec_test_launcher
override EXEC_TEST_REAL := $(BUILD_DIR)/guard_exec_test_launcher.real
override EXEC_TEST_LAUNCHER_OBJECT := $(BUILD_DIR)/guard_exec_test_launcher.o
override EXEC_TEST_IDENTITY_HEADER := $(BUILD_DIR)/guard_exec_test_identity.h

override CONFIG_STAMP := $(BUILD_DIR)/build-config.stamp
override ATTESTATION := $(BUILD_DIR)/build-attestation.json
override TMP_DIR := $(BUILD_DIR)/tmp

override CPPFLAGS := -I$(CUDA_HOME)/include -I$(LIBSMCTRL_DIR)
override CFLAGS := -O2 -g -std=gnu11 -fPIC -Wall -Wextra
override LAUNCHER_CFLAGS := -O2 -g -std=gnu11 -Wall -Wextra -Werror
override NVCCFLAGS := -O3 -lineinfo -std=c++17 -ccbin=$(CC) \
	--generate-code=arch=compute_$(CUDA_ARCH),code=sm_$(CUDA_ARCH) \
	--generate-code=arch=compute_$(CUDA_ARCH),code=compute_$(CUDA_ARCH)
override LDLIBS := -L$(CUDA_HOME)/lib64 \
	-Xlinker=-rpath -Xlinker=$(abspath $(CUDA_HOME))/lib64 \
	-lcudart -lcuda -lstdc++

override HERMETIC_PATH := $(CUDA_HOME)/bin:/usr/bin:/bin
override BUILD_ENV := $(ENV) -i \
	PATH='$(HERMETIC_PATH)' \
	LANG=C LC_ALL=C TZ=UTC SOURCE_DATE_EPOCH=0 CUDA_CACHE_DISABLE=1 \
	TMPDIR='$(TMP_DIR)'
override TEST_ENV := $(ENV) -i \
	PATH='$(HERMETIC_PATH)' \
	LANG=C LC_ALL=C TZ=UTC SOURCE_DATE_EPOCH=0 \
	PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
	CUDA_VISIBLE_DEVICES='' CUDA_MPS_PIPE_DIRECTORY='' \
	TMPDIR='$(TMP_DIR)'
override RECURSIVE_MAKE_ENV := $(ENV) -i \
	PATH='$(HERMETIC_PATH)' \
	LANG=C LC_ALL=C TZ=UTC

override NATIVE_SOURCES := \
	$(FORMAL_MAKEFILE) \
	$(SOURCE_DIR)/README.md \
	$(SOURCE_DIR)/smid_probe.cu \
	$(SOURCE_DIR)/guard_launcher.c \
	$(SOURCE_DIR)/guard_exec_test_fixture.c \
	$(SOURCE_DIR)/parent_guard.c \
	$(SOURCE_DIR)/parent_guard.h \
	$(SOURCE_DIR)/parent_guard_test_helper.c \
	$(SOURCE_DIR)/sealed_exec.c \
	$(SOURCE_DIR)/sealed_exec.h \
	$(SOURCE_DIR)/sha256.c \
	$(SOURCE_DIR)/sha256.h \
	$(ATTESTATION_SCRIPT) \
	$(REPO_ROOT)/src/burstserve/git_provenance.py \
	$(REPO_ROOT)/tests/test_native_parent_guard.py \
	$(LIBSMCTRL_DIR)/libsmctrl.c \
	$(LIBSMCTRL_DIR)/libsmctrl.h

override define ATTEST_ENV
$(BUILD_ENV) \
BS_SOURCE_DIR='$(SOURCE_DIR)' \
BS_REPO_ROOT='$(REPO_ROOT)' \
BS_BUILD_DIR='$(BUILD_DIR)' \
BS_BUILD_TMPDIR='$(TMP_DIR)' \
BS_BUILD_LOCK_PATH='$(BUILD_LOCK)' \
BS_HERMETIC_PATH='$(HERMETIC_PATH)' \
BS_CUDA_HOME='$(abspath $(CUDA_HOME))' \
BS_CUDA_ARCH='$(CUDA_ARCH)' \
BS_NVCC='$(NVCC)' \
BS_CC='$(CC)' \
BS_AR='$(AR)' \
BS_CPPFLAGS='$(CPPFLAGS)' \
BS_CFLAGS='$(CFLAGS)' \
BS_NVCCFLAGS='$(NVCCFLAGS)' \
BS_LDLIBS='$(LDLIBS)' \
BS_LAUNCHER_CFLAGS='$(LAUNCHER_CFLAGS)' \
BS_LIBSMCTRL_DIR='$(abspath $(LIBSMCTRL_DIR))'
endef

override ALL_GENERATED_FILES := \
	$(TARGET) $(REAL_TARGET) $(PROBE_OBJECT) \
	$(LIBSMCTRL_OBJECT) $(LIBSMCTRL_ARCHIVE) \
	$(LAUNCHER_OBJECT) $(PARENT_GUARD_OBJECT) $(SEALED_EXEC_OBJECT) \
	$(SHA256_OBJECT) $(TEST_HELPER_OBJECT) $(TEST_HELPER) \
	$(REAL_IDENTITY_HEADER) \
	$(EXEC_TEST_LAUNCHER) $(EXEC_TEST_REAL) \
	$(EXEC_TEST_LAUNCHER_OBJECT) $(EXEC_TEST_IDENTITY_HEADER) \
	$(CONFIG_STAMP) $(ATTESTATION)
override INTERMEDIATE_FILES := \
	$(PROBE_OBJECT) $(LIBSMCTRL_OBJECT) $(LIBSMCTRL_ARCHIVE) \
	$(LAUNCHER_OBJECT) $(PARENT_GUARD_OBJECT) $(SEALED_EXEC_OBJECT) \
	$(SHA256_OBJECT) $(TEST_HELPER_OBJECT) $(EXEC_TEST_LAUNCHER_OBJECT) \
	$(BUILD_ROOT)/.smctrl_probe-build.lock

.PHONY: all gate-required-check verify-attestation clean \
	_locked-all _locked-gate-required-check _locked-clean \
	_locked-verify-attestation require-build-lock \
	trust-root-self-check FORCE FORCE_COMPILE

override define acquire_build_lock
	@umask 077; \
	runtime_dir='$(RUNTIME_USER_DIR)'; \
	lock_dir='$(BUILD_LOCK_DIR)'; \
	lock_path='$(BUILD_LOCK)'; \
	test -d "$$runtime_dir" && test ! -L "$$runtime_dir"; \
	test "$$($(STAT) -Lc '%u:%a' -- "$$runtime_dir")" = '$(BUILD_EUID):700'; \
	if test ! -e "$$lock_dir"; then \
		$(INSTALL) -d -m 0700 -- "$$lock_dir"; \
	fi; \
	test -d "$$lock_dir" && test ! -L "$$lock_dir"; \
	test "$$($(STAT) -Lc '%u:%a' -- "$$lock_dir")" = '$(BUILD_EUID):700'; \
	if test ! -e "$$lock_path"; then \
		$(INSTALL) -m 0600 /dev/null "$$lock_path"; \
	fi; \
	test -f "$$lock_path" && test ! -L "$$lock_path"; \
	test "$$($(STAT) -Lc '%u:%a:%h' -- "$$lock_path")" = \
		'$(BUILD_EUID):600:1'; \
	exec $(LOCK_FD)<>"$$lock_path"; \
	$(FLOCK) -x $(LOCK_FD); \
	test "$$($(READLINK) -f -- /proc/$$$$/fd/$(LOCK_FD))" = "$$lock_path"; \
	$(RECURSIVE_MAKE_ENV) /usr/bin/make \
		--no-print-directory -C '$(SOURCE_DIR)' \
		-f '$(FORMAL_MAKEFILE)' $(1)$(if $(strip $(2)), && \
	$(2))
endef

all:
	$(call acquire_build_lock,_locked-all)

gate-required-check:
	$(call acquire_build_lock,_locked-gate-required-check,\
		/usr/bin/printf '%s\n' \
		'burstserve-native-gate-required-check: verified')

verify-attestation:
	$(call acquire_build_lock,_locked-verify-attestation)

clean:
	$(call acquire_build_lock,_locked-clean)

override TRUST_ROOT_VARIABLES := \
	SHELL .SHELLFLAGS .DEFAULT_GOAL MAKE_SHELL_MODE_DIAGNOSTIC \
	SOURCE_DIR REPO_ROOT FORMAL_MAKEFILE \
	ATTESTATION_SCRIPT MAKE_PARSE_GUARD_DIAGNOSTIC CUDA_HOME BUILD_ROOT BUILD_DIR \
	LIBSMCTRL_DIR MAKE NVCC CC AR PYTHON ENV FLOCK INSTALL STAT \
	READLINK REALPATH RM RMDIR MV CHMOD CUDA_ARCH BUILD_EUID \
	RUNTIME_USER_DIR BUILD_LOCK_DIR BUILD_LOCK LOCK_FD \
	BURSTSERVE_BUILD_LOCK_PATH BURSTSERVE_BUILD_LOCK_FD TARGET \
	REAL_TARGET PROBE_OBJECT LIBSMCTRL_OBJECT LIBSMCTRL_ARCHIVE \
	LAUNCHER_OBJECT PARENT_GUARD_OBJECT SEALED_EXEC_OBJECT \
	SHA256_OBJECT TEST_HELPER_OBJECT TEST_HELPER REAL_IDENTITY_HEADER \
	EXEC_TEST_LAUNCHER EXEC_TEST_REAL EXEC_TEST_LAUNCHER_OBJECT \
	EXEC_TEST_IDENTITY_HEADER CONFIG_STAMP ATTESTATION \
	ATTESTATION_SCRIPT TMP_DIR CPPFLAGS CFLAGS LAUNCHER_CFLAGS \
	NVCCFLAGS LDLIBS HERMETIC_PATH BUILD_ENV TEST_ENV \
	RECURSIVE_MAKE_ENV NATIVE_SOURCES ATTEST_ENV ALL_GENERATED_FILES \
	INTERMEDIATE_FILES \
	acquire_build_lock TRUST_ROOT_VARIABLES

trust-root-self-check:
	@set -eu; \
	case "$$-" in *p*) ;; \
		*) echo 'recipe shell is not in Bash privileged mode' >&2; exit 2 ;; \
	esac; \
	$(foreach variable,$(TRUST_ROOT_VARIABLES), \
		test '$(origin $(variable))' = override || { \
			echo 'trust root is not make-override protected: $(variable)' >&2; \
			exit 2; \
		};) \
	test '$(MAKE)' = /usr/bin/make; \
	test '$(PYTHON)' = /usr/bin/python3; \
	test '$(FLOCK)' = /usr/bin/flock; \
	test '$(SOURCE_DIR)' = \
		'$(abspath $(dir $(lastword $(MAKEFILE_LIST))))'; \
	test '$(REPO_ROOT)' = '$(abspath $(SOURCE_DIR)/../..)'; \
	test '$(BUILD_ROOT)' = '$(REPO_ROOT)/build'; \
	test '$(BUILD_DIR)' = '$(REPO_ROOT)/build/smctrl_probe'; \
	test '$(BUILD_LOCK)' = \
		'/run/user/$(BUILD_EUID)/burstserve-smctrl-probe/build.lock'; \
	test '$(ATTESTATION)' = '$(BUILD_DIR)/build-attestation.json'; \
	test '$(BURSTSERVE_BUILD_LOCK_PATH)' = '$(BUILD_LOCK)'; \
	test '$(BURSTSERVE_BUILD_LOCK_FD)' = '$(LOCK_FD)'; \
	printf '%s\n' 'native-trust-root-self-check: ok'

require-build-lock:
	@test '$(BURSTSERVE_BUILD_LOCK_PATH)' = '$(BUILD_LOCK)'
	@test '$(BURSTSERVE_BUILD_LOCK_FD)' = '$(LOCK_FD)'
	@test -e /proc/self/fd/$(LOCK_FD)
	@test "$$($(READLINK) -f -- /proc/self/fd/$(LOCK_FD))" = \
		'$(BUILD_LOCK)'
	@test "$$($(STAT) -Lc '%u:%a:%h' -- '$(BUILD_LOCK)')" = \
		'$(BUILD_EUID):600:1'
	@exec 8<>'$(BUILD_LOCK)'; \
	if $(FLOCK) -n 8; then \
		echo 'internal target does not hold the inherited build lock' >&2; \
		exit 2; \
	fi; \
	$(FLOCK) -n '$(LOCK_FD)'
	@if test ! -e '$(BUILD_ROOT)'; then \
		$(INSTALL) -d -m 0700 -- '$(BUILD_ROOT)'; \
	fi; \
	test -d '$(BUILD_ROOT)' && test ! -L '$(BUILD_ROOT)'; \
	test "$$($(STAT) -Lc '%u' -- '$(BUILD_ROOT)')" = '$(BUILD_EUID)'; \
	$(CHMOD) 0700 -- '$(BUILD_ROOT)'; \
	test "$$($(STAT) -Lc '%u:%a' -- '$(BUILD_ROOT)')" = \
		'$(BUILD_EUID):700'

_locked-all: require-build-lock $(ATTESTATION)
	$(RM) -f -- $(foreach file,$(INTERMEDIATE_FILES),'$(file)')

_locked-gate-required-check: require-build-lock $(ATTESTATION)
	test -x '$(TARGET)'
	test -x '$(REAL_TARGET)'
	test -x '$(TEST_HELPER)'
	test -x '$(EXEC_TEST_LAUNCHER)'
	test -x '$(EXEC_TEST_REAL)'
	test -s '$(CONFIG_STAMP)'
	test -s '$(ATTESTATION)'
	$(ATTEST_ENV) $(PYTHON) -I -S -B '$(ATTESTATION_SCRIPT)' verify \
		--stamp '$(CONFIG_STAMP)' \
		--attestation '$(ATTESTATION)'
	$(RM) -f -- $(foreach file,$(INTERMEDIATE_FILES),'$(file)')
	cd '$(REPO_ROOT)' && \
		$(TEST_ENV) /usr/bin/python3 -I -S -B \
		'$(REPO_ROOT)/tests/test_native_parent_guard.py' -v
	$(ATTEST_ENV) $(PYTHON) -I -S -B '$(ATTESTATION_SCRIPT)' verify \
		--stamp '$(CONFIG_STAMP)' \
		--attestation '$(ATTESTATION)'

_locked-verify-attestation: require-build-lock trust-root-self-check
	$(ATTEST_ENV) $(PYTHON) -I -S -B '$(ATTESTATION_SCRIPT)' verify \
		--stamp '$(CONFIG_STAMP)' \
		--attestation '$(ATTESTATION)'
	@printf '%s\n' 'native-attestation-verification: ok'

$(BUILD_DIR): require-build-lock
	$(INSTALL) -d -m 0700 -- '$@'
	test "$$($(STAT) -Lc '%u:%a' -- '$@')" = '$(BUILD_EUID):700'

$(TMP_DIR): FORCE | $(BUILD_DIR)
	$(INSTALL) -d -m 0700 -- '$@'
	test "$$($(STAT) -Lc '%u:%a' -- '$@')" = '$(BUILD_EUID):700'

$(CONFIG_STAMP): FORCE $(NATIVE_SOURCES) | $(BUILD_DIR) $(TMP_DIR)
	$(ATTEST_ENV) $(PYTHON) -I -S -B '$(ATTESTATION_SCRIPT)' stamp --output '$@'

FORCE:
FORCE_COMPILE:

$(LIBSMCTRL_OBJECT): FORCE_COMPILE $(LIBSMCTRL_DIR)/libsmctrl.c \
		$(LIBSMCTRL_DIR)/libsmctrl.h $(CONFIG_STAMP) | $(BUILD_DIR) $(TMP_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(CC) $(CPPFLAGS) $(CFLAGS) \
		-c '$(LIBSMCTRL_DIR)/libsmctrl.c' -o "$$tmp"; \
	$(CHMOD) 0400 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(LIBSMCTRL_ARCHIVE): FORCE_COMPILE $(LIBSMCTRL_OBJECT) | $(BUILD_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(AR) rcs "$$tmp" '$(LIBSMCTRL_OBJECT)'; \
	$(CHMOD) 0400 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(PROBE_OBJECT): FORCE_COMPILE $(SOURCE_DIR)/smid_probe.cu \
		$(LIBSMCTRL_DIR)/libsmctrl.h $(CONFIG_STAMP) | $(BUILD_DIR) $(TMP_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(NVCC) $(CPPFLAGS) $(NVCCFLAGS) \
		-c '$(SOURCE_DIR)/smid_probe.cu' -o "$$tmp"; \
	$(CHMOD) 0400 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(REAL_TARGET): FORCE_COMPILE $(PROBE_OBJECT) $(LIBSMCTRL_ARCHIVE) \
		| $(BUILD_DIR) $(TMP_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(NVCC) $(NVCCFLAGS) \
		'$(PROBE_OBJECT)' '$(LIBSMCTRL_ARCHIVE)' $(LDLIBS) -o "$$tmp"; \
	$(CHMOD) 0500 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(REAL_IDENTITY_HEADER): FORCE_COMPILE $(REAL_TARGET) $(CONFIG_STAMP) \
		| $(BUILD_DIR)
	$(ATTEST_ENV) $(PYTHON) -I -S -B '$(ATTESTATION_SCRIPT)' identity-header \
		--input '$(REAL_TARGET)' --output '$@'

$(PARENT_GUARD_OBJECT): FORCE_COMPILE $(SOURCE_DIR)/parent_guard.c \
		$(SOURCE_DIR)/parent_guard.h $(CONFIG_STAMP) | $(BUILD_DIR) $(TMP_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(CC) -I'$(SOURCE_DIR)' $(LAUNCHER_CFLAGS) \
		-c '$(SOURCE_DIR)/parent_guard.c' -o "$$tmp"; \
	$(CHMOD) 0400 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(SHA256_OBJECT): FORCE_COMPILE $(SOURCE_DIR)/sha256.c \
		$(SOURCE_DIR)/sha256.h $(CONFIG_STAMP) | $(BUILD_DIR) $(TMP_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(CC) -I'$(SOURCE_DIR)' $(LAUNCHER_CFLAGS) \
		-c '$(SOURCE_DIR)/sha256.c' -o "$$tmp"; \
	$(CHMOD) 0400 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(SEALED_EXEC_OBJECT): FORCE_COMPILE $(SOURCE_DIR)/sealed_exec.c \
		$(SOURCE_DIR)/sealed_exec.h $(SOURCE_DIR)/sha256.h \
		$(CONFIG_STAMP) | $(BUILD_DIR) $(TMP_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(CC) -I'$(SOURCE_DIR)' $(LAUNCHER_CFLAGS) \
		-c '$(SOURCE_DIR)/sealed_exec.c' -o "$$tmp"; \
	$(CHMOD) 0400 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(LAUNCHER_OBJECT): FORCE_COMPILE $(SOURCE_DIR)/guard_launcher.c \
		$(SOURCE_DIR)/parent_guard.h $(SOURCE_DIR)/sealed_exec.h \
		$(REAL_IDENTITY_HEADER) $(CONFIG_STAMP) | $(BUILD_DIR) $(TMP_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(CC) -I'$(SOURCE_DIR)' $(LAUNCHER_CFLAGS) \
		-include '$(REAL_IDENTITY_HEADER)' \
		-c '$(SOURCE_DIR)/guard_launcher.c' -o "$$tmp"; \
	$(CHMOD) 0400 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(TARGET): FORCE_COMPILE $(LAUNCHER_OBJECT) $(PARENT_GUARD_OBJECT) \
		$(SEALED_EXEC_OBJECT) $(SHA256_OBJECT) | $(BUILD_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(CC) -static $(LAUNCHER_CFLAGS) \
		'$(LAUNCHER_OBJECT)' '$(PARENT_GUARD_OBJECT)' \
		'$(SEALED_EXEC_OBJECT)' '$(SHA256_OBJECT)' -o "$$tmp"; \
	$(CHMOD) 0500 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(TEST_HELPER_OBJECT): FORCE_COMPILE \
		$(SOURCE_DIR)/parent_guard_test_helper.c \
		$(SOURCE_DIR)/parent_guard.h $(SOURCE_DIR)/sealed_exec.h \
		$(SOURCE_DIR)/sha256.h $(CONFIG_STAMP) | $(BUILD_DIR) $(TMP_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(CC) -I'$(SOURCE_DIR)' $(LAUNCHER_CFLAGS) \
		-c '$(SOURCE_DIR)/parent_guard_test_helper.c' -o "$$tmp"; \
	$(CHMOD) 0400 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(TEST_HELPER): FORCE_COMPILE $(TEST_HELPER_OBJECT) $(PARENT_GUARD_OBJECT) \
		$(SEALED_EXEC_OBJECT) $(SHA256_OBJECT) | $(BUILD_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(CC) -static $(LAUNCHER_CFLAGS) \
		'$(TEST_HELPER_OBJECT)' '$(PARENT_GUARD_OBJECT)' \
		'$(SEALED_EXEC_OBJECT)' '$(SHA256_OBJECT)' -o "$$tmp"; \
	$(CHMOD) 0500 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(EXEC_TEST_REAL): FORCE_COMPILE $(SOURCE_DIR)/guard_exec_test_fixture.c \
		$(CONFIG_STAMP) | $(BUILD_DIR) $(TMP_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(CC) $(LAUNCHER_CFLAGS) \
		'$(SOURCE_DIR)/guard_exec_test_fixture.c' -o "$$tmp"; \
	$(CHMOD) 0500 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(EXEC_TEST_IDENTITY_HEADER): FORCE_COMPILE $(EXEC_TEST_REAL) \
		$(CONFIG_STAMP) | $(BUILD_DIR)
	$(ATTEST_ENV) $(PYTHON) -I -S -B '$(ATTESTATION_SCRIPT)' identity-header \
		--input '$(EXEC_TEST_REAL)' --output '$@'

$(EXEC_TEST_LAUNCHER_OBJECT): FORCE_COMPILE \
		$(SOURCE_DIR)/guard_launcher.c $(SOURCE_DIR)/parent_guard.h \
		$(SOURCE_DIR)/sealed_exec.h $(EXEC_TEST_IDENTITY_HEADER) \
		$(CONFIG_STAMP) | $(BUILD_DIR) $(TMP_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(CC) -I'$(SOURCE_DIR)' $(LAUNCHER_CFLAGS) \
		-DBURSTSERVE_TEST_FAULT_INJECTION=1 \
		-include '$(EXEC_TEST_IDENTITY_HEADER)' \
		-c '$(SOURCE_DIR)/guard_launcher.c' -o "$$tmp"; \
	$(CHMOD) 0400 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(EXEC_TEST_LAUNCHER): FORCE_COMPILE $(EXEC_TEST_LAUNCHER_OBJECT) \
		$(PARENT_GUARD_OBJECT) $(SEALED_EXEC_OBJECT) $(SHA256_OBJECT) \
		| $(BUILD_DIR)
	@tmp='$@.tmp'; $(RM) -f -- "$$tmp"; trap '$(RM) -f -- "$$tmp"' EXIT; \
	$(BUILD_ENV) $(CC) -static $(LAUNCHER_CFLAGS) \
		'$(EXEC_TEST_LAUNCHER_OBJECT)' '$(PARENT_GUARD_OBJECT)' \
		'$(SEALED_EXEC_OBJECT)' '$(SHA256_OBJECT)' -o "$$tmp"; \
	$(CHMOD) 0500 "$$tmp"; $(MV) -f -- "$$tmp" '$@'; trap - EXIT

$(ATTESTATION): FORCE_COMPILE $(TARGET) $(REAL_TARGET) $(TEST_HELPER) \
		$(REAL_IDENTITY_HEADER) $(EXEC_TEST_LAUNCHER) $(EXEC_TEST_REAL) \
		$(EXEC_TEST_IDENTITY_HEADER) $(CONFIG_STAMP) $(ATTESTATION_SCRIPT)
	$(ATTEST_ENV) $(PYTHON) -I -S -B '$(ATTESTATION_SCRIPT)' finalize \
		--stamp '$(CONFIG_STAMP)' --output '$@'

_locked-clean: require-build-lock
	@target="$$( $(REALPATH) -m -- '$(BUILD_DIR)' )"; \
	safe_root="$$( $(REALPATH) -m -- '$(REPO_ROOT)/build' )"; \
	case "$$target" in "$$safe_root"/*) ;; \
		*) echo "Refusing unsafe BUILD_DIR: $$target" >&2; exit 2 ;; esac; \
	$(RM) -f -- $(foreach file,$(ALL_GENERATED_FILES),'$(file)'); \
	$(RMDIR) -- '$(TMP_DIR)' '$(BUILD_DIR)'
