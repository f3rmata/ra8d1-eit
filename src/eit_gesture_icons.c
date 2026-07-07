/*
 * Gesture icon rendering for LCD — Rock / Scissors / Paper.
 *
 * The hand shapes are rasterized 1-bit masks generated from Font Awesome
 * Free solid SVG icons. MCU-side rendering is intentionally simple: no SVG
 * parser, no image decoder, just mask-to-RGB565 blits into the framebuffer.
 */

#include "eit_gesture_icons.h"
#include "eit_gesture_icon_masks.h"
#include "eit_lcd.h"

/* ---- visual style ---- */

#define BG_COLOR       EIT_LCD_RGB565(18, 24, 42)
#define ICON_COLOR     EIT_LCD_RGB565(244, 180, 112)
#define ICON_SHADOW    EIT_LCD_RGB565(8, 12, 22)

static void draw_mask_pixel(int x, int y, uint16_t color)
{
    if (x < 0 || x >= EIT_LCD_WIDTH || y < 0 || y >= EIT_LCD_HEIGHT)
    {
        return;
    }

    uint16_t *buf = eit_lcd_get_draw_buffer();
    buf[y * EIT_LCD_WIDTH + x] = color;
}

static void draw_icon_mask(const uint8_t *mask, int x0, int y0, uint16_t color)
{
    for (uint32_t y = 0U; y < EIT_GESTURE_ICON_H; y++)
    {
        for (uint32_t byte_x = 0U; byte_x < EIT_GESTURE_ICON_STRIDE; byte_x++)
        {
            uint8_t bits = mask[y * EIT_GESTURE_ICON_STRIDE + byte_x];
            if (bits == 0U)
            {
                continue;
            }

            for (uint32_t bit = 0U; bit < 8U; bit++)
            {
                if ((bits & (uint8_t) (0x80U >> bit)) != 0U)
                {
                    int x = x0 + (int) (byte_x * 8U + bit);
                    draw_mask_pixel(x, y0 + (int) y, color);
                }
            }
        }
    }
}

static void draw_svg_icon(const uint8_t *mask)
{
    int x = ((int) EIT_LCD_WIDTH - (int) EIT_GESTURE_ICON_W) / 2;
    int y = ((int) EIT_LCD_HEIGHT - (int) EIT_GESTURE_ICON_H) / 2;

    eit_lcd_clear(BG_COLOR);

    draw_icon_mask(mask, x + 5, y + 5, ICON_SHADOW);
    draw_icon_mask(mask, x, y, ICON_COLOR);

    (void) eit_lcd_buffer_swap();
}

void eit_gesture_draw_rock(void)
{
    draw_svg_icon(g_eit_gesture_rock_mask);
}

void eit_gesture_draw_scissors(void)
{
    draw_svg_icon(g_eit_gesture_scissors_mask);
}

void eit_gesture_draw_paper(void)
{
    draw_svg_icon(g_eit_gesture_paper_mask);
}
