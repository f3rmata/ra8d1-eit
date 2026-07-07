/*
 * LCD framebuffer driver for RA8D1 Vision Board MIPI 2.0" display.
 * Ported from RT-Thread SDK (drv_lcd.c) to bare-metal FSP API.
 *
 * Architecture:
 *   - Double-buffered framebuffers in SDRAM (fb_background[0]/[1])
 *   - VSync-triggered buffer swap via R_GLCDC_BufferChange()
 *   - D/AVE 2D GPU for hardware-accelerated blit/fill operations
 *   - GPT6 PWM backlight control on P10_11
 *   - GLCDC line-detect interrupt for VSync synchronization
 */

#include "hal_data.h"
#include "eit_lcd.h"
#include "eit_lcd_panel.h"

#include <stdbool.h>
#include <string.h>

/* ---- VSync synchronization (bare-metal; replaces RT-Thread rt_completion) ---- */

static volatile bool g_vsync_occurred = false;

void DisplayVsyncCallback(display_callback_args_t *p_args)
{
    if (DISPLAY_EVENT_LINE_DETECTION == p_args->event)
    {
        g_vsync_occurred = true;
    }
}

static void vsync_wait(void)
{
    uint32_t timeout = 1000U;  /* 1 second safety timeout */
    while (!g_vsync_occurred && (timeout > 0U))
    {
        R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MILLISECONDS);
        timeout--;
    }
    g_vsync_occurred = false;
}

/* ---- Frame buffer management ---- */

/*
 * fb_background is declared in ra_gen/common_data.c (FSP-generated).
 * It is a 2-element array of framebuffers placed in the .sdram section:
 *   uint8_t fb_background[2][DISPLAY_BUFFER_STRIDE_BYTES_INPUT0 * DISPLAY_VSIZE_INPUT0]
 *
 * For 480x360 RGB565: stride = 960 bytes, size = 960 * 360 = 345,600 bytes each.
 */

static uint16_t *g_draw_buffer = NULL;  /* pointer to the buffer being drawn to */

/* ---- D/AVE 2D GPU handle (declared in ra_gen/common_data.c) ---- */

extern d2_device *d2_handle0;
static d2_device **p_d2_handle = &d2_handle0;
static d2_renderbuffer *g_renderbuffer;

/* ---- Backlight control (GPT6 PWM on P10_11) ---- */

void eit_lcd_backlight_on(void)
{
    /*
     * Use GPT6 PWM for backlight.
     * g_timer6 must be configured as PWM in e2studio FSP.
     * Period: 10000ns (100kHz), initial duty: 70%.
     */
#ifdef BSP_USING_PWM6
    timer_info_t info;
    fsp_err_t err;

    err = g_backlight_pwm.p_api->open(g_backlight_pwm.p_ctrl, g_backlight_pwm.p_cfg);
    if (FSP_SUCCESS == err)
    {
        /* Set period to 10000 ticks (100kHz at PCLK) */
        (void) g_backlight_pwm.p_api->periodSet(g_backlight_pwm.p_ctrl, 10000U);
        /* Set duty to 70% */
        (void) g_backlight_pwm.p_api->dutyCycleSet(g_backlight_pwm.p_ctrl, 7000U, GPT_IO_PIN_GTIOCA);
        (void) g_backlight_pwm.p_api->start(g_backlight_pwm.p_ctrl);
    }
#else
    /* Fallback: GPIO high on P10_11 */
    R_IOPORT_PinCfg(&g_ioport_ctrl, BSP_IO_PORT_10_PIN_11,
                    (uint32_t) IOPORT_CFG_PORT_DIRECTION_OUTPUT |
                    (uint32_t) IOPORT_CFG_PORT_OUTPUT_HIGH);
#endif
}

void eit_lcd_backlight_off(void)
{
#ifdef BSP_USING_PWM6
    (void) g_backlight_pwm.p_api->stop(g_backlight_pwm.p_ctrl);
#else
    R_IOPORT_PinWrite(&g_ioport_ctrl, BSP_IO_PORT_10_PIN_11, BSP_IO_LEVEL_LOW);
#endif
}

void eit_lcd_backlight_set(uint8_t duty_pct)
{
#ifdef BSP_USING_PWM6
    uint32_t duty = (uint32_t) (duty_pct * 100U);
    if (duty > 10000U) duty = 10000U;
    (void) g_backlight_pwm.p_api->dutyCycleSet(g_backlight_pwm.p_ctrl, duty, GPT_IO_PIN_GTIOCA);
#else
    (void) duty_pct;
#endif
}

/* ---- Hardware reset ---- */

void eit_lcd_reset(void)
{
    /* Configure P11_04 as output */
    R_IOPORT_PinCfg(&g_ioport_ctrl, BSP_IO_PORT_11_PIN_04,
                    (uint32_t) IOPORT_CFG_PORT_DIRECTION_OUTPUT |
                    (uint32_t) IOPORT_CFG_PORT_OUTPUT_HIGH);
    R_BSP_SoftwareDelay(20U, BSP_DELAY_UNITS_MILLISECONDS);

    /* Reset pulse: low 100ms */
    R_IOPORT_PinWrite(&g_ioport_ctrl, BSP_IO_PORT_11_PIN_04, BSP_IO_LEVEL_LOW);
    R_BSP_SoftwareDelay(100U, BSP_DELAY_UNITS_MILLISECONDS);

    /* Release reset: high 100ms */
    R_IOPORT_PinWrite(&g_ioport_ctrl, BSP_IO_PORT_11_PIN_04, BSP_IO_LEVEL_HIGH);
    R_BSP_SoftwareDelay(100U, BSP_DELAY_UNITS_MILLISECONDS);
}

/* ---- D/AVE 2D GPU initialization ---- */

static void g2d_hw_init(void)
{
    d2_device *handle;

    handle = d2_opendevice(0);
    if (NULL == handle)
    {
        return;
    }

    if (0 != d2_inithw(handle, 0U))
    {
        /*
         * D/AVE initialization can fail while the display path is still usable
         * for CPU framebuffer drawing. Avoid d2_closedevice() here because the
         * current bare-metal DRW/newlib heap pairing faults in d1_freemem().
         */
        return;
    }

    /*
     * Set rendering buffer to the current working framebuffer.
     * For double-buffering, this gets updated on each swap.
     */
    g_renderbuffer = d2_newrenderbuffer(handle, 480, 360);
    /* Update GPU framebuffer */
    d2_framebuffer(handle, g_draw_buffer, 480, 360, 480, d2_mode_rgb565);
    *p_d2_handle = handle;

    /*
     * Reset GPU to idle state before use.
     * d2_flushframe with a NULL renderbuffer puts GPU in a known state.
     */
    (void) d2_flushframe(handle);
}

/* ---- LCD subsystem initialization ---- */

void eit_lcd_init(void)
{
    fsp_err_t err;

    /* Point draw buffer to framebuffer[0] initially */
    g_draw_buffer = (uint16_t *) &fb_background[0][0];

    /* Fill both framebuffers with black */
    memset(fb_background[0], 0x00, sizeof(fb_background[0]));
    memset(fb_background[1], 0x00, sizeof(fb_background[1]));

    /* Open GLCDC display driver */
    err = g_display0.p_api->open(g_display0.p_ctrl, g_display0.p_cfg);
    if (FSP_SUCCESS != err)
    {
        return;
    }

    /* Initialize MIPI DSI panel (must be after GLCDC open, before GLCDC start) */
    eit_lcd_panel_init();

    /* Initialize D/AVE 2D GPU */
    g2d_hw_init();

    /* Start GLCDC output (frame streaming begins) */
    err = g_display0.p_api->start(g_display0.p_ctrl);
    if (FSP_SUCCESS != err)
    {
        return;
    }

    /* Turn on backlight */
    eit_lcd_backlight_on();
}

/* ---- Drawing operations ---- */

void eit_lcd_clear(uint16_t color)
{
    uint16_t *fb = g_draw_buffer;
    uint32_t px_count = (uint32_t) EIT_LCD_WIDTH * EIT_LCD_HEIGHT;
    for (uint32_t i = 0U; i < px_count; i++)
    {
        fb[i] = color;
    }
}

uint16_t * eit_lcd_get_draw_buffer(void)
{
    return g_draw_buffer;
}

uint16_t * eit_lcd_buffer_swap(void)
{
    fsp_err_t err;

    /*
     * For double-buffering: the draw buffer is the "back" buffer.
     * After calling R_GLCDC_BufferChange(), the hardware flips to the
     * newly drawn buffer at the next VSync, and the other buffer becomes
     * available for drawing.
     */
    err = R_GLCDC_BufferChange(g_display0.p_ctrl, (uint8_t *) g_draw_buffer, DISPLAY_FRAME_LAYER_1);
    if (FSP_SUCCESS != err)
    {
        return g_draw_buffer;
    }

    /* Wait for VSync to complete the flip */
    vsync_wait();

    /*
     * Now g_draw_buffer is being displayed. Switch to the other buffer
     * for drawing the next frame.
     */
    if (g_draw_buffer == (uint16_t *) &fb_background[0][0])
    {
        g_draw_buffer = (uint16_t *) &fb_background[1][0];
    }
    else
    {
        g_draw_buffer = (uint16_t *) &fb_background[0][0];
    }

    return g_draw_buffer;
}

void eit_lcd_draw_pixel(uint32_t x, uint32_t y, uint16_t color)
{
    if ((x < EIT_LCD_WIDTH) && (y < EIT_LCD_HEIGHT))
    {
        g_draw_buffer[y * EIT_LCD_WIDTH + x] = color;
    }
}

void eit_lcd_fill_rect(uint32_t x, uint32_t y, uint32_t w, uint32_t h, uint16_t color)
{
    /* Clip to screen bounds */
    if (x >= (uint32_t) EIT_LCD_WIDTH || y >= (uint32_t) EIT_LCD_HEIGHT)
    {
        return;
    }
    if ((x + w) > (uint32_t) EIT_LCD_WIDTH)
    {
        w = (uint32_t) EIT_LCD_WIDTH - x;
    }
    if ((y + h) > (uint32_t) EIT_LCD_HEIGHT)
    {
        h = (uint32_t) EIT_LCD_HEIGHT - y;
    }

    for (uint32_t row = 0U; row < h; row++)
    {
        for (uint32_t col = 0U; col < w; col++)
        {
            g_draw_buffer[(y + row) * EIT_LCD_WIDTH + (x + col)] = color;
        }
    }
}
