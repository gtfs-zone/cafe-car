SHELL := /usr/bin/env sh

COMMIT := $(shell git rev-parse --short origin/main)

.PHONY: cp

cp:
	@echo $(COMMIT)
	@printf '%s' $(COMMIT) | xclip -selection clipboard 2>/dev/null && echo "(copied to clipboard)" || true
