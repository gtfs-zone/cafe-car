SHELL := /usr/bin/env sh
REGISTRY := git.kcfam.us/gtfs.zone/redis-gtfs-rt-api

COMMIT := $(shell git rev-parse --short HEAD)
TAG ?= $(COMMIT)
ALLOW_DIRTY ?= 0

.PHONY: push

push:
ifeq ($(ALLOW_DIRTY),0)
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "Error: working directory is dirty. Commit or stash changes, or set ALLOW_DIRTY=1"; \
		exit 1; \
	fi
	@if [ -n "$$(git log @{u}.. 2>/dev/null)" ]; then \
		echo "Error: unpushed commits exist. Push first, or set ALLOW_DIRTY=1"; \
		exit 1; \
	fi
endif
	docker build -t $(REGISTRY):$(TAG) .
	docker push $(REGISTRY):$(TAG)
	@echo "Pushed $(REGISTRY):$(TAG)"
