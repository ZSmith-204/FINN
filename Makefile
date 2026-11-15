# FINN build/install helpers.
#
# Builds the FINN CLI (`finn`) and the FINN Studio GUI (`finn-studio`) with
# PyInstaller and installs them into $(BINDIR) (default: ~/.local/bin).
#
# Common usage:
#   make install-cli     # build + install the CLI only   -> $(BINDIR)/finn
#   make install         # build + install CLI and GUI    -> $(BINDIR)/{finn,finn-studio}
#   make install-studio  # build + install the GUI only
#   make install-full    # alias for `make install`
#   make build           # build both binaries, no install
#   make ensure-path     # ensure ~/.local/bin is on PATH in ~/.bashrc
#   make clean           # remove untracked build artifacts
#
# The frozen GUI shells out to the `finn` CLI on PATH, so install the CLI too
# (`make install`) if you want build/run from the GUI.

PYINSTALLER ?= .venv/bin/pyinstaller
BINDIR      ?= $(HOME)/.local/bin
CLI_SPEC     = finn.spec
GUI_SPEC     = finn-studio.spec

.PHONY: help build build-cli build-studio install install-cli install-studio \
        install-full _preflight ensure-path clean

help:
	@printf '%s\n' \
		'make install-cli     Build + install the CLI only -> $(BINDIR)/finn' \
		'make install         Build + install CLI + GUI    -> $(BINDIR)/{finn,finn-studio}' \
		'make install-studio  Build + install the GUI only -> $(BINDIR)/finn-studio' \
		'make install-full    Alias for `make install`' \
		'make build           Build both binaries (no install)' \
		'make build-cli       Build dist/finn' \
		'make build-studio    Build dist/finn-studio' \
		'make ensure-path     Ensure ~/.local/bin is on PATH in ~/.bashrc' \
		'make clean           Remove untracked build artifacts'

# PyInstaller preflight shared by all build targets.
_preflight:
	@if [ ! -x "$(PYINSTALLER)" ]; then \
		echo "error: $(PYINSTALLER) not found; run .venv/bin/python -m pip install -r requirements-build.txt first" >&2; \
		exit 1; \
	fi

build-cli: _preflight
	$(PYINSTALLER) $(CLI_SPEC) --clean --noconfirm

build-studio: _preflight
	$(PYINSTALLER) $(GUI_SPEC) --clean --noconfirm

build: build-cli build-studio

# Copy a built binary into $(BINDIR), preserving executable bits.
#   $(call install_bin,<src>,<dest-name>)
define install_bin
	@if command -v install >/dev/null 2>&1; then \
		install -Dm755 "$(1)" "$(BINDIR)/$(2)"; \
	else \
		mkdir -p "$(BINDIR)"; \
		cp "$(1)" "$(BINDIR)/$(2)"; \
		chmod 755 "$(BINDIR)/$(2)"; \
	fi
	@echo "Installed $(BINDIR)/$(2)"
endef

install-cli: build-cli ensure-path
	$(call install_bin,dist/finn,finn)

install-studio: build-studio ensure-path
	$(call install_bin,dist/finn-studio,finn-studio)

install: install-cli install-studio

install-full: install

# Idempotently add ~/.local/bin to PATH.  This target intentionally targets
# the default ~/.local/bin even if $(BINDIR) is overridden for other targets.
# An existing *active* PATH
# assignment/export (a non-comment line beginning with `PATH=` or
# `export PATH=` whose same line also mentions .local/bin) counts as
# configured; incidental mentions elsewhere (e.g. `MYPATH=...`) do not.
# Otherwise append one uniquely-marked guarded case block.
ensure-path:
	@if grep -v '^[[:space:]]*#' "$(HOME)/.bashrc" 2>/dev/null \
		| grep -Eq '(export[[:space:]]+PATH=|^[[:space:]]*PATH=)[^#]*\.local/bin'; then \
		echo "~/.local/bin already configured on PATH in ~/.bashrc; nothing to do"; \
	else \
		printf '%s\n' \
			'# finn-studio: ensure ~/.local/bin is on PATH' \
			'case ":$$PATH:" in' \
			'  *":$$HOME/.local/bin:"*) ;;' \
			'  *) export PATH="$$HOME/.local/bin:$$PATH" ;;' \
			'esac' >> "$(HOME)/.bashrc"; \
		echo "Added ~/.local/bin to PATH in ~/.bashrc (restart shell or source it)"; \
	fi

# Conservative: remove only UNTRACKED artifacts this Makefile produces.
# dist/finn and build/finn/* are git-tracked (committed earlier); `make
# build-cli` overwrites them in place, so `clean` deliberately leaves them.
clean:
	rm -rf build/finn-studio
	rm -f dist/finn-studio dist/finn-studio.exe
	rm -f finn-studio.spec.bak finn-studio.spec.tmp finn.spec.bak finn.spec.tmp
