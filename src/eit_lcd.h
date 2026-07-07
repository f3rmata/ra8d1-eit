#ifndef EIT_LCD_H_
#define EIT_LCD_H_

#include <stdint.h>
#include <stdbool.h>
#include "r_display_api.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * LCD dimensions for the MIPI 2.0" panel.
 * Must match the GLCDC configuration in ra_gen/common_data.c.
 */
#define EIT_LCD_WIDTH   480
#define EIT_LCD_HEIGHT  360

/* RGB565 color macros */
#define EIT_LCD_RGB565(r, g, b) \
    (uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | (((b) & 0xF8) >> 3))
#define EIT_LCD_COLOR_BLACK      0x0000
#define EIT_LCD_COLOR_WHITE      0xFFFF
#define EIT_LCD_COLOR_RED        0xF800
#define EIT_LCD_COLOR_GREEN      0x07E0
#define EIT_LCD_COLOR_BLUE       0x001F

/*
 * Initialize the full LCD subsystem:
 *   - GLCDC open
 *   - MIPI DSI panel init
 *   - D/AVE 2D GPU init
 *   - GLCDC start
 *   - Backlight on
 *
 * Call after eit_sdram_init() and eit_lcd_reset().
 */
void eit_lcd_init(void);

/*
 * Hardware reset the LCD panel via P11_04 GPIO.
 * Holds reset low for 100ms, then high for 100ms.
 */
void eit_lcd_reset(void);

/* Backlight control via GPT6 PWM on P10_11 */
void eit_lcd_backlight_on(void);
void eit_lcd_backlight_off(void);
void eit_lcd_backlight_set(uint8_t duty_pct);  /* 0-100 */

/* Fill current working framebuffer with solid color */
void eit_lcd_clear(uint16_t color);

/*
 * Swap double buffers and wait for VSync.
 * Returns pointer to the now-active draw buffer.
 */
uint16_t * eit_lcd_buffer_swap(void);

/* Get current draw buffer (the one LVGL/app should render to) */
uint16_t * eit_lcd_get_draw_buffer(void);

/* Single-pixel write */
void eit_lcd_draw_pixel(uint32_t x, uint32_t y, uint16_t color);

/* Filled rectangle */
void eit_lcd_fill_rect(uint32_t x, uint32_t y, uint32_t w, uint32_t h, uint16_t color);

/*
 * VSync callback — called by FSP GLCDC driver on DISPLAY_EVENT_LINE_DETECTION.
 * This function name must match the callback configured in the e2studio GLCDC stack.
 */
void DisplayVsyncCallback(display_callback_args_t *p_args);

#ifdef __cplusplus
}
#endif

#endif /* EIT_LCD_H_ */
