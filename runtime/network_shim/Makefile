# runtime/network_shim/Makefile
# ----------------------------------------------------------------------
# Builds libbambu_networking.so — the aarch64-Termux stub plug-in that
# proxies BambuStudio's NetworkAgent calls to x2d_bridge.py over a
# Unix-domain socket.  Drop the resulting .so at
#   ~/.config/BambuStudio/plugins/libbambu_networking.so
# and BambuStudio's `initialize_network_module` will pick it up.
#
# Targets:
#   make            — build libbambu_networking.so
#   make install    — copy into ~/.config/BambuStudio/plugins/
#   make clean      — remove build artefacts
#
# Requirements:
#   * clang++ (libc++; same toolchain BambuStudio is built with on
#     Termux, so std::string + std::function ABI matches)
#   * pthread

CXX        ?= $(shell command -v clang++ || command -v g++)
# Termux exports PREFIX=$PREFIX/files/usr as the package-install root, so
# our ?= would inherit it and install to the wrong place. Use a distinct
# variable name and fall back to BambuStudioInternal (matches the
# BBL_INTERNAL_TESTING build of bambu-studio used in our patches) so
# `make install` lands at the right plugin folder by default.
INSTALL_TO ?= $(HOME)/.config/BambuStudioInternal/plugins

REPO_ROOT  := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../..)
NLOHMANN   := $(REPO_ROOT)/bs-bionic/src
INCLUDES   := -Iinclude -I$(NLOHMANN)
SRC        := src/bridge_client.cpp src/agent.cpp src/exports.cpp src/exports_ft.cpp
OBJ        := $(SRC:.cpp=.o)
CXXFLAGS   ?= -std=c++17 -fPIC -O2 -Wall -Wextra -Wno-unused-parameter \
              $(INCLUDES) $(EXTRA_CXXFLAGS)
LDFLAGS    ?= -shared -pthread \
              -Wl,--no-undefined -Wl,--as-needed \
              -Wl,-soname,libbambu_networking.so $(EXTRA_LDFLAGS)

.PHONY: all install clean print-cfg

all: libbambu_networking.so libBambuSource.so

libbambu_networking.so: $(OBJ)
	$(CXX) $(LDFLAGS) -o $@ $^
	@echo "[shim] built $@ ($$(stat -c%s $@) bytes)"

# libBambuSource.so — empty stub. BambuStudio's create_network_agent
# gate requires get_bambu_source_entry() to dlopen this file
# successfully; the actual Bambu_* symbols inside are only consumed by
# the optional "browse SD card files" GUI panel, so we ship an empty
# stub to satisfy the gate without implementing the printer-side gst
# source protocol.
libBambuSource.so: src/bambu_source_stub.c
	$(CXX) -shared -fPIC -O2 -Wl,-soname,libBambuSource.so $< -o $@
	@echo "[shim] built $@ ($$(stat -c%s $@) bytes)"

src/%.o: src/%.cpp include/shim_internal.hpp
	$(CXX) $(CXXFLAGS) -c $< -o $@

install: libbambu_networking.so libBambuSource.so
	mkdir -p '$(INSTALL_TO)'
	cp libbambu_networking.so libBambuSource.so '$(INSTALL_TO)/'
	@echo "[shim] installed to $(INSTALL_TO)/"

clean:
	rm -f $(OBJ) libbambu_networking.so libBambuSource.so

print-cfg:
	@echo "CXX        = $(CXX)"
	@echo "REPO_ROOT  = $(REPO_ROOT)"
	@echo "NLOHMANN   = $(NLOHMANN)"
	@echo "PREFIX     = $(INSTALL_TO)"
	@echo "CXXFLAGS   = $(CXXFLAGS)"
	@echo "LDFLAGS    = $(LDFLAGS)"
