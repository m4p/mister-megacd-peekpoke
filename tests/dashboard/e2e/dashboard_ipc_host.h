// Host-test replacements for the Main_MiSTer SPI/user_io calls used by
// support/megacd/dashboard_ipc.cpp (compiled with -DDASH_IPC_HOST_TEST).
#ifndef DASHBOARD_IPC_HOST_H
#define DASHBOARD_IPC_HOST_H
#include <stdint.h>
uint16_t spi_uio_cmd_cont(uint16_t cmd);
uint16_t spi_w(uint16_t word);
void DisableIO();
char is_megacd();
#endif
