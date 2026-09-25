// "Main_MiSTer" for the end-to-end test: the real dashboard_ipc.cpp adapter,
// with SPI routed into the Verilated core. Prints "JOY <hex> <frame>" whenever
// the injected player-1 mask changes.
//
// usage: main_sim --socket PATH [--not-megacd] [--wram-seed N]
#include "Vsim_top.h"
#include "verilated.h"

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "dashboard_ipc.h"
#include "dashboard_ipc_host.h"

static Vsim_top *top;
static uint64_t cycles;
static uint64_t frame;
static int g_is_megacd = 1;
static uint16_t last_joy = 0xFFFF;
static volatile sig_atomic_t stop_flag;
static const uint64_t FRAME_CYCLES = 60000;   // compressed "frame" for simulation speed

static void tick()
{
	top->clk = 0; top->eval();
	top->clk = 1; top->eval();
	cycles++;
	uint64_t ph = cycles % FRAME_CYCLES;
	top->vblank = ph < 4000;
	if (ph == 0) frame++;
	if (top->joy != last_joy) {
		last_joy = top->joy;
		printf("JOY %03x %llu\n", last_joy, (unsigned long long)frame);
		fflush(stdout);
	}
}

uint16_t spi_w(uint16_t w)
{
	top->io_din = w;
	top->io_strobe = 1; tick();
	top->io_strobe = 0; tick(); tick(); tick();
	return top->io_claim ? top->io_dout : 0;   // unclaimed: hps_io answers (modelled as 0)
}

uint16_t spi_uio_cmd_cont(uint16_t cmd)
{
	top->io_enable = 1; tick();
	return spi_w(cmd);
}

void DisableIO()
{
	top->io_enable = 0; tick(); tick();
}

char is_megacd() { return (char)g_is_megacd; }

static uint16_t wram_word(uint32_t i, uint32_t seed)
{
	uint32_t x = (i + 1) * 2654435761u ^ seed;
	x ^= x >> 13; x *= 0x5bd1e995; x ^= x >> 15;
	return (uint16_t)x;
}

static void on_signal(int) { stop_flag = 1; }

int main(int argc, char **argv)
{
	Verilated::commandArgs(argc, argv);
	uint32_t seed = 1;
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--socket") && i + 1 < argc) setenv("MEGACD_DASH_SOCK", argv[++i], 1);
		else if (!strcmp(argv[i], "--not-megacd")) g_is_megacd = 0;
		else if (!strcmp(argv[i], "--wram-seed") && i + 1 < argc) seed = (uint32_t)strtoul(argv[++i], 0, 0);
	}
	signal(SIGTERM, on_signal);
	signal(SIGINT, on_signal);
	signal(SIGPIPE, SIG_IGN);

	top = new Vsim_top;
	top->io_enable = 0; top->io_strobe = 0; top->rom_download = 0;
	for (uint32_t i = 0; i < 32768; i++) {
		top->bd_we = 1; top->bd_addr = i; top->bd_data = wram_word(i, seed);
		tick();
	}
	top->bd_we = 0;
	printf("READY\n");
	fflush(stdout);

	while (!stop_flag) {
		dashboard_ipc_poll();
		for (int i = 0; i < 1500; i++) tick();
	}
	delete top;
	return 0;
}
