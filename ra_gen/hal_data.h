/* generated HAL header file - do not edit */
#ifndef HAL_DATA_H_
#define HAL_DATA_H_
#include <stdint.h>
#include "bsp_api.h"
#include "common_data.h"
#include "r_sci_b_i2c.h"
#include "r_i2c_master_api.h"
#include "r_gpt.h"
#include "r_timer_api.h"
#include "r_dmac.h"
#include "r_transfer_api.h"
#include "r_sci_b_spi.h"
#include "r_spi_api.h"
#include "r_sci_b_uart.h"
#include "r_uart_api.h"
FSP_HEADER
extern const i2c_master_cfg_t g_i2c_touch_cfg;
/* I2C on SCI Instance. */
extern const i2c_master_instance_t g_i2c_touch;
#ifndef sci_b_i2c_master_callback
void sci_b_i2c_master_callback(i2c_master_callback_args_t *p_args);
#endif

extern const sci_b_i2c_extended_cfg_t g_i2c_touch_cfg_extend;
extern sci_b_i2c_instance_ctrl_t g_i2c_touch_ctrl;
/** Timer on GPT Instance. */
extern const timer_instance_t g_backlight_pwm;

/** Access the GPT instance using these structures when calling API functions directly (::p_api is not used). */
extern gpt_instance_ctrl_t g_backlight_pwm_ctrl;
extern const timer_cfg_t g_backlight_pwm_cfg;

#ifndef NULL
void NULL(timer_callback_args_t *p_args);
#endif
/* Transfer on DMAC Instance. */
extern const transfer_instance_t g_adc_port_dma;

/** Access the DMAC instance using these structures when calling API functions directly (::p_api is not used). */
extern dmac_instance_ctrl_t g_adc_port_dma_ctrl;
extern const transfer_cfg_t g_adc_port_dma_cfg;

#ifndef adc_dma_callback
void adc_dma_callback(transfer_callback_args_t *p_args);
#endif
/** Timer on GPT Instance. */
extern const timer_instance_t g_adc_sample_timer;

/** Access the GPT instance using these structures when calling API functions directly (::p_api is not used). */
extern gpt_instance_ctrl_t g_adc_sample_timer_ctrl;
extern const timer_cfg_t g_adc_sample_timer_cfg;

#ifndef NULL
void NULL(timer_callback_args_t *p_args);
#endif
/** SPI on SCI Instance. */
extern const spi_instance_t g_sci_spi_h;

/** Access the SCI_B_SPI instance using these structures when calling API functions directly (::p_api is not used). */
extern sci_b_spi_instance_ctrl_t g_sci_spi_h_ctrl;
extern const spi_cfg_t g_sci_spi_h_cfg;

/** Called by the driver when a transfer has completed or an error has occurred (Must be implemented by the user). */
#ifndef sci_b_spi_h_callback
void sci_b_spi_h_callback(spi_callback_args_t *p_args);
#endif
/** UART on SCI Instance. */
extern const uart_instance_t g_uart9;

/** Access the UART instance using these structures when calling API functions directly (::p_api is not used). */
extern sci_b_uart_instance_ctrl_t g_uart9_ctrl;
extern const uart_cfg_t g_uart9_cfg;
extern const sci_b_uart_extended_cfg_t g_uart9_cfg_extend;

#ifndef uart9_callback
void uart9_callback(uart_callback_args_t *p_args);
#endif
void hal_entry(void);
void g_hal_init(void);
FSP_FOOTER
#endif /* HAL_DATA_H_ */
