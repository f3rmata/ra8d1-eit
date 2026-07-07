#ifndef EIT_BOARD_H_
#define EIT_BOARD_H_

#include "hal_data.h"

#include <stdbool.h>
#include <stdint.h>

typedef void (*eit_text_writer_t)(char const * p_text);

typedef enum e_eit_rheo
{
    EIT_RHEO_DRIVE = 0,
    EIT_RHEO_MEAS  = 1,
} eit_rheo_t;

typedef enum e_eit_mux
{
    EIT_MUX_SRC  = 0,
    EIT_MUX_SINK = 1,
    EIT_MUX_VP   = 2,
    EIT_MUX_VN   = 3,
    EIT_MUX_SRC2 = 4,
    EIT_MUX_SINK2 = 5,
    EIT_MUX_VP2  = 6,
    EIT_MUX_VN2  = 7,
} eit_mux_t;

typedef struct st_eit_adc_stats
{
    uint16_t min;
    uint16_t max;
    uint32_t sum;
    uint16_t last;
    uint16_t samples;
} eit_adc_stats_t;

fsp_err_t eit_board_init(void);
void eit_board_print_signals(eit_text_writer_t writer);

void eit_set_power_controls(bool en, bool pwr, bool oe);
void eit_get_power_controls(bool * p_en, bool * p_pwr, bool * p_oe);

void eit_ad5270_write(eit_rheo_t rheo, uint8_t command, uint16_t data);
void eit_ad5270_unlock(eit_rheo_t rheo);
void eit_ad5270_set(eit_rheo_t rheo, uint16_t value);
void eit_ad5270_shutdown(eit_rheo_t rheo, bool shutdown);

uint8_t eit_mux_command(uint8_t channel, bool enable);
uint8_t eit_electrode_to_mux(uint8_t electrode);
bool eit_mux_write(eit_mux_t mux, uint8_t channel, bool enable);
bool eit_mux_all_off(void);
bool eit_route(uint8_t src, uint8_t sink, uint8_t vp, uint8_t vn);

uint16_t eit_adc_read(void);
bool eit_adc_capture(uint16_t * p_out, uint32_t samples, uint32_t rate_hz);
void eit_adc_sample(eit_adc_stats_t * p_stats, uint16_t samples, uint32_t delay_us);

#endif
