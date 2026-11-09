# make check: what CI runs (.github/workflows/check.yml).
# shellcheck is skipped when it isn't installed; CI always has it.

SHELL_SCRIPTS = bin/nvmet-target bin/nuke-kernel setup/build.sh \
	setup/debian.sh setup/freebsd.sh setup/kernel.sh nvmelab/probe.sh \
	boot/container.sh boot/mkroot.sh boot/boot.sh boot/guest-base.sh \
	boot/guest-build.sh boot/guest-image.sh boot/guest-boot.sh
PYTHON ?= python3

.PHONY: check test lint

check: lint test

test:
	$(PYTHON) -X warn_default_encoding -W error::EncodingWarning \
		-m unittest discover -s tests

lint:
	@for f in $(SHELL_SCRIPTS); do \
		case $$(head -1 $$f) in \
		*bash*) bash -n $$f ;; \
		*) sh -n $$f ;; \
		esac || exit 1; \
	done
	$(PYTHON) -m py_compile bin/lab nvmelab/*.py tests/*.py
	@if command -v shellcheck >/dev/null; then \
		shellcheck $(SHELL_SCRIPTS); \
	else \
		echo "shellcheck not installed, skipped"; \
	fi
