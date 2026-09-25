# megacd-dashboard bridge
#   make                         host build (tests, development)
#   make CROSS_COMPILE=arm-none-linux-gnueabihf-   MiSTer build (into build-arm/)
#   make test                    host unit + HTTP tests against the mock core
# Host and cross builds use separate directories, so running the tests never
# overwrites the ARM binary.

CC      = $(CROSS_COMPILE)gcc
CFLAGS ?= -O2
CFLAGS += -std=c11 -D_GNU_SOURCE -Wall -Wextra -Wno-unused-parameter
LDLIBS  = -lm
ifneq ($(CROSS_COMPILE),)
LDFLAGS += -static
OUT      = build-arm
else
OUT      = build
endif

SRC  = src/main.c src/api.c src/dev.c src/json.c src/transport_ipc.c src/transport_mock.c
LIB  = src/api.c src/dev.c src/json.c src/transport_ipc.c src/transport_mock.c
HDR  = $(wildcard src/*.h)
BIN  = $(OUT)/megacd-dashboard

.PHONY: all test clean

all: $(BIN)

$(BIN): $(SRC) $(HDR)
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(SRC) $(LDLIBS)

build/unit_tests: tests/unit_tests.c $(LIB) $(HDR)
ifneq ($(CROSS_COMPILE),)
	$(error run the tests with the host compiler: make test (without CROSS_COMPILE))
endif
	@mkdir -p build
	$(CC) $(CFLAGS) -Isrc -o $@ tests/unit_tests.c $(LIB) $(LDLIBS)

test: $(BIN) build/unit_tests
	./build/unit_tests
	python3 tests/http_tests.py --bin $(BIN)

clean:
	rm -rf $(OUT)
