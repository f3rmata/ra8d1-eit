/*
 * EIT reconstruction visualization on LCD (direct framebuffer rendering).
 *
 * Renders a pseudo-color heatmap of conductivity change (ds) over the
 * 8-electrode circular mesh. Uses direct pixel writes to the framebuffer
 * in SDRAM — no LVGL or RT-Thread dependency.
 *
 * The heatmap maps ds values to a blue-green-yellow-red colormap and
 * renders each mesh element (triangle) as a flat-filled polygon.
 * Electrode positions are overlaid as white circles.
 */

#include "eit_ui.h"
#include "eit_lcd.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

/* ---- Colormap: blue → green → yellow → red (viridis-inspired) ---- */

typedef struct {
    uint16_t rgb565;
} color_entry_t;

#define CMAP_SIZE 256
#define EIT_UI_DS_DEADZONE 0.1f
#define EIT_UI_DS_DEADZONE_SOFT_EDGE 0.08f
#define EIT_UI_DEADZONE_COLOR EIT_LCD_RGB565(34, 40, 58)
#define EIT_UI_LOW_COLOR EIT_LCD_RGB565(22, 34, 64)
static color_entry_t g_colormap[CMAP_SIZE];

/*
 * Interpolate between two RGB565 colors.
 * t: 0.0 → color a, 1.0 → color b
 */
static uint16_t lerp_rgb565(uint16_t a, uint16_t b, float t)
{
    if (t < 0.0f) t = 0.0f;
    if (t > 1.0f) t = 1.0f;

    int r_a = (a >> 11) & 0x1F;
    int g_a = (a >> 5)  & 0x3F;
    int bl_a = a        & 0x1F;

    int r_b = (b >> 11) & 0x1F;
    int g_b = (b >> 5)  & 0x3F;
    int bl_b = b        & 0x1F;

    int r = r_a + (int) ((r_b - r_a) * t);
    int g = g_a + (int) ((g_b - g_a) * t);
    int bl = bl_a + (int) ((bl_b - bl_a) * t);

    return (uint16_t) (((uint16_t) r << 11) | ((uint16_t) g << 5) | (uint16_t) bl);
}

static float smoothstep(float edge0, float edge1, float x)
{
    float t = (x - edge0) / (edge1 - edge0);
    if (t < 0.0f) t = 0.0f;
    if (t > 1.0f) t = 1.0f;

    return t * t * (3.0f - 2.0f * t);
}

static void build_colormap(void)
{
    /*
     * Four-point colormap:
     *   0/0  → dim blue          — minimum
     *   1/3  → blue   (0x001F)   — low
     *   2/3  → green  (0x07E0)   — mid
     *   3/3  → red    (0xF800)   — max
     */
    for (int i = 0; i < CMAP_SIZE; i++)
    {
        float t = (float) i / (float) (CMAP_SIZE - 1);

        if (t < 0.333f)
        {
            g_colormap[i].rgb565 = lerp_rgb565(EIT_UI_LOW_COLOR, 0x001F, t / 0.333f);
        }
        else if (t < 0.667f)
        {
            g_colormap[i].rgb565 = lerp_rgb565(0x001F, 0x07E0, (t - 0.333f) / 0.334f);
        }
        else
        {
            g_colormap[i].rgb565 = lerp_rgb565(0x07E0, 0xF800, (t - 0.667f) / 0.333f);
        }
    }
}

static uint16_t ds_to_color(float ds, float ds_min, float ds_max)
{
    float abs_ds = fabsf(ds);
    if (abs_ds <= EIT_UI_DS_DEADZONE)
    {
        return EIT_UI_DEADZONE_COLOR;
    }

    float range = ds_max - ds_min;
    if (range < 1.0e-9f)
    {
        return EIT_UI_DEADZONE_COLOR;
    }

    float t = (ds - ds_min) / range;
    if (t <= 0.0f) return g_colormap[0].rgb565;
    if (t >= 1.0f) return g_colormap[CMAP_SIZE - 1].rgb565;

    int idx = (int) (t * (float) (CMAP_SIZE - 1));
    uint16_t color = g_colormap[idx].rgb565;
    if (abs_ds < (EIT_UI_DS_DEADZONE + EIT_UI_DS_DEADZONE_SOFT_EDGE))
    {
        float fade = smoothstep(EIT_UI_DS_DEADZONE,
                                EIT_UI_DS_DEADZONE + EIT_UI_DS_DEADZONE_SOFT_EDGE,
                                abs_ds);
        color = lerp_rgb565(EIT_UI_DEADZONE_COLOR, color, fade);
    }

    return color;
}

static float ds_apply_deadzone(float ds)
{
    return (fabsf(ds) < EIT_UI_DS_DEADZONE) ? 0.0f : ds;
}

static void ds_display_range(const float ds_node[EIT_RECON_NODES], float *ds_min, float *ds_max)
{
    float min_value = 0.0f;
    float max_value = 0.0f;

    for (uint32_t node = 0U; node < EIT_RECON_NODES; node++)
    {
        float value = ds_apply_deadzone(ds_node[node]);
        if (value < min_value)
        {
            min_value = value;
        }
        if (value > max_value)
        {
            max_value = value;
        }
    }

    *ds_min = min_value;
    *ds_max = max_value;
}

/* ---- Triangle rasterization ---- */

/*
 * Map a model node (x, y) coordinate to screen pixel (px_x, px_y).
 * The model's xy are in a [-1, +1]² normalized space.
 * We map to a 300×300 square centered on the 480×360 screen with
 * electrode 1 at the top, 3 right, 5 bottom, 7 left.
 */
#define HEATMAP_X0   90
#define HEATMAP_Y0   28
#define HEATMAP_SIZE 300

static void node_to_pixel(uint32_t node, int *px, int *py)
{
    float x = g_eit_recon_node_xy[node][0];
    float y = g_eit_recon_node_xy[node][1];

    *px = HEATMAP_X0 + (int) (( x + 1.0f) * 0.5f * (float) HEATMAP_SIZE);
    *py = HEATMAP_Y0 + (int) ((-y + 1.0f) * 0.5f * (float) HEATMAP_SIZE);
}

/* Edge function for triangle rasterization */
static int edge_func(int x0, int y0, int x1, int y1, int x, int y)
{
    return (x - x0) * (y1 - y0) - (y - y0) * (x1 - x0);
}

/* Check if point is inside a triangle (including edges) */
static bool point_in_triangle(int x, int y,
                              int x0, int y0,
                              int x1, int y1,
                              int x2, int y2)
{
    int e0 = edge_func(x1, y1, x2, y2, x, y);
    int e1 = edge_func(x2, y2, x0, y0, x, y);
    int e2 = edge_func(x0, y0, x1, y1, x, y);

    /* All same sign (or zero) = inside */
    bool neg = (e0 < 0) || (e1 < 0) || (e2 < 0);
    bool pos = (e0 > 0) || (e1 > 0) || (e2 > 0);
    return !(neg && pos);
}

static void rasterize_triangle(int x0, int y0, int x1, int y1, int x2, int y2, uint16_t color)
{
    /* Bounding box */
    int min_x = x0 < x1 ? (x0 < x2 ? x0 : x2) : (x1 < x2 ? x1 : x2);
    int max_x = x0 > x1 ? (x0 > x2 ? x0 : x2) : (x1 > x2 ? x1 : x2);
    int min_y = y0 < y1 ? (y0 < y2 ? y0 : y2) : (y1 < y2 ? y1 : y2);
    int max_y = y0 > y1 ? (y0 > y2 ? y0 : y2) : (y1 > y2 ? y1 : y2);

    /* Clip to screen */
    if (min_x < 0) min_x = 0;
    if (max_x >= EIT_LCD_WIDTH)  max_x = EIT_LCD_WIDTH - 1;
    if (min_y < 0) min_y = 0;
    if (max_y >= EIT_LCD_HEIGHT) max_y = EIT_LCD_HEIGHT - 1;

    uint16_t *fb = eit_lcd_get_draw_buffer();
    for (int y = min_y; y <= max_y; y++)
    {
        for (int x = min_x; x <= max_x; x++)
        {
            if (point_in_triangle(x, y, x0, y0, x1, y1, x2, y2))
            {
                fb[y * EIT_LCD_WIDTH + x] = color;
            }
        }
    }
}

/* ---- Text rendering (simple 8x13 bitmap font placeholder) ---- */

/*
 * Minimal bitmap font: digits 0-9, letters A-F, a-f, space, dash, dot.
 * Each character is 6 pixels wide × 10 pixels tall.
 * For full implementation, a real font table would be used.
 * This placeholder draws simple colored rectangles as text indicators.
 */

static void draw_char(int x, int y, char ch, uint16_t color)
{
    /* Simple placeholder — fill a 6x10 block with a pattern based on the char */
    uint16_t *fb = eit_lcd_get_draw_buffer();
    int idx = 0;

    if (ch >= '0' && ch <= '9')      idx = ch - '0';
    else if (ch >= 'A' && ch <= 'F') idx = ch - 'A' + 10;
    else if (ch >= 'a' && ch <= 'f') idx = ch - 'a' + 10;
    else if (ch == ' ')              { return; }
    else if (ch == '-')              idx = 16;
    else if (ch == '.')              idx = 17;
    else if (ch == ':')              idx = 18;
    else if (ch == '/')              idx = 19;
    else                             idx = 20;

    /* 7-segment-like simple glyph for digits; others get a bar */
    static const uint8_t glyphs[21][10] = {
        /* 0 */ {0x1E,0x21,0x21,0x21,0x21,0x21,0x21,0x21,0x21,0x1E},
        /* 1 */ {0x04,0x0C,0x14,0x04,0x04,0x04,0x04,0x04,0x04,0x1F},
        /* 2 */ {0x1E,0x21,0x01,0x02,0x04,0x08,0x10,0x20,0x20,0x3F},
        /* 3 */ {0x1E,0x21,0x01,0x01,0x0E,0x01,0x01,0x01,0x21,0x1E},
        /* 4 */ {0x02,0x06,0x0A,0x12,0x22,0x3F,0x02,0x02,0x02,0x02},
        /* 5 */ {0x3F,0x20,0x20,0x20,0x3E,0x01,0x01,0x01,0x21,0x1E},
        /* 6 */ {0x0E,0x10,0x20,0x20,0x3E,0x21,0x21,0x21,0x21,0x1E},
        /* 7 */ {0x3F,0x01,0x02,0x04,0x04,0x08,0x08,0x10,0x10,0x10},
        /* 8 */ {0x1E,0x21,0x21,0x21,0x1E,0x21,0x21,0x21,0x21,0x1E},
        /* 9 */ {0x1E,0x21,0x21,0x21,0x21,0x1F,0x01,0x02,0x04,0x18},
        /* A */ {0x0C,0x12,0x21,0x21,0x21,0x3F,0x21,0x21,0x21,0x21},
        /* B */ {0x3E,0x21,0x21,0x21,0x3E,0x21,0x21,0x21,0x21,0x3E},
        /* C */ {0x1E,0x21,0x20,0x20,0x20,0x20,0x20,0x20,0x21,0x1E},
        /* D */ {0x3C,0x22,0x21,0x21,0x21,0x21,0x21,0x21,0x22,0x3C},
        /* E */ {0x3F,0x20,0x20,0x20,0x3E,0x20,0x20,0x20,0x20,0x3F},
        /* F */ {0x3F,0x20,0x20,0x20,0x3E,0x20,0x20,0x20,0x20,0x20},
        /* - */ {0x00,0x00,0x00,0x00,0x3F,0x00,0x00,0x00,0x00,0x00},
        /* . */ {0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x18,0x18},
        /* : */ {0x00,0x18,0x18,0x00,0x00,0x00,0x18,0x18,0x00,0x00},
        /* / */ {0x01,0x02,0x02,0x04,0x04,0x08,0x08,0x10,0x10,0x20},
        /* ? */ {0x1E,0x21,0x01,0x02,0x04,0x08,0x08,0x00,0x08,0x08},
    };

    uint16_t *p = fb + y * EIT_LCD_WIDTH + x;
    for (int row = 0; row < 10; row++)
    {
        uint8_t bits = glyphs[idx][row];
        for (int col = 0; col < 6; col++)
        {
            if (bits & (0x20 >> col))
            {
                p[col] = color;
            }
        }
        p += EIT_LCD_WIDTH;
    }
}

static void draw_string(int x, int y, const char *str, uint16_t color)
{
    int cx = x;
    while (*str)
    {
        draw_char(cx, y, *str, color);
        cx += 7;
        str++;
    }
}

static void draw_number(int x, int y, int value, uint16_t color)
{
    char buf[12];
    int pos = 0;

    if (value < 0)
    {
        draw_char(x, y, '-', color);
        x += 7;
        value = -value;
    }
    if (value == 0)
    {
        draw_char(x, y, '0', color);
        return;
    }
    while (value > 0)
    {
        buf[pos++] = '0' + (value % 10);
        value /= 10;
    }
    for (int i = pos - 1; i >= 0; i--)
    {
        draw_char(x, y, buf[i], color);
        x += 7;
    }
}

static void draw_float(int x, int y, float value, uint16_t color)
{
    char buf[16];
    int n = snprintf(buf, sizeof(buf), "%.3f", value);
    if (n > 0) draw_string(x, y, buf, color);
}

/* ---- Public API ---- */

static bool g_ui_initialized = false;

void eit_ui_init(void)
{
    build_colormap();

    /* Clear screen to dark background */
    eit_lcd_clear(EIT_LCD_RGB565(16, 16, 32));

    /* Draw header background bar */
    eit_lcd_fill_rect(0, 0, 480, 24, EIT_LCD_RGB565(40, 40, 60));
    draw_string(4, 4, "EIT Ready", EIT_LCD_COLOR_WHITE);

    /* Draw footer background bar */
    eit_lcd_fill_rect(0, 340, 480, 20, EIT_LCD_RGB565(40, 40, 60));

    g_ui_initialized = true;
}

void eit_ui_show_recon_frame(const float ds_node[EIT_RECON_NODES],
                             const eit_recon_summary_t *summary,
                             uint32_t frame_id)
{
    if (!g_ui_initialized)
    {
        eit_ui_init();
    }

    /* Clear heatmap area */
    eit_lcd_fill_rect(HEATMAP_X0 - 5, HEATMAP_Y0 - 5,
                      HEATMAP_SIZE + 10, HEATMAP_SIZE + 10,
                      EIT_LCD_RGB565(16, 16, 32));

    /* Compute display ds range after applying the LCD dead zone. */
    float ds_min = 0.0f;
    float ds_max = 0.0f;
    ds_display_range(ds_node, &ds_min, &ds_max);

    /* Render each mesh element as a filled triangle */
    for (uint32_t elem = 0U; elem < EIT_RECON_ELEMENTS; elem++)
    {
        uint16_t n0 = g_eit_recon_elements[elem][0];
        uint16_t n1 = g_eit_recon_elements[elem][1];
        uint16_t n2 = g_eit_recon_elements[elem][2];

        if (n0 >= EIT_RECON_NODES || n1 >= EIT_RECON_NODES || n2 >= EIT_RECON_NODES)
        {
            continue;
        }

        float ds0 = ds_apply_deadzone(ds_node[n0]);
        float ds1 = ds_apply_deadzone(ds_node[n1]);
        float ds2 = ds_apply_deadzone(ds_node[n2]);
        float ds_avg = (ds0 + ds1 + ds2) / 3.0f;
        uint16_t color = ds_to_color(ds_avg, ds_min, ds_max);

        int px0, py0, px1, py1, px2, py2;
        node_to_pixel(n0, &px0, &py0);
        node_to_pixel(n1, &px1, &py1);
        node_to_pixel(n2, &px2, &py2);

        rasterize_triangle(px0, py0, px1, py1, px2, py2, color);
    }

    /* Draw electrode markers (white filled circles at 8 electrode positions) */
    /*
     * Electrode positions in model space (from 8-electrode circular setup):
     * S1 at top, S2 top-right, S3 right, ... going clockwise.
     * For a 8-electrode circular array, electrode e is at angle:
     *   theta = (e * 45° - 90°) in degrees
     * With S1 at the top.
     */
    for (uint32_t e = 0U; e < EIT_RECON_ELECTRODES; e++)
    {
        float angle = ((float) e * 45.0f - 90.0f) * 3.14159265f / 180.0f;
        float ex = cosf(angle);
        float ey = sinf(angle);
        int px = HEATMAP_X0 + (int) (( ex + 1.0f) * 0.5f * (float) HEATMAP_SIZE);
        int py = HEATMAP_Y0 + (int) ((-ey + 1.0f) * 0.5f * (float) HEATMAP_SIZE);

        /* Small filled circle (3px radius) */
        for (int dy = -3; dy <= 3; dy++)
        {
            for (int dx = -3; dx <= 3; dx++)
            {
                if (dx * dx + dy * dy <= 9)
                {
                    eit_lcd_draw_pixel((uint32_t) (px + dx), (uint32_t) (py + dy),
                                       EIT_LCD_COLOR_WHITE);
                }
            }
        }
    }

    /* Draw header: frame info */
    eit_lcd_fill_rect(0, 0, 480, 24, EIT_LCD_RGB565(40, 40, 60));
    char header[48];
    snprintf(header, sizeof(header), "F:%u v:%u i:%u r:%u",
             (unsigned int) frame_id,
             (unsigned int) summary->valid_count,
             (unsigned int) summary->invalid_count,
             (unsigned int) summary->retry_count);
    draw_string(4, 4, header, EIT_LCD_COLOR_WHITE);

    /* Draw footer: statistics */
    eit_lcd_fill_rect(0, 340, 480, 20, EIT_LCD_RGB565(40, 40, 60));
    char footer[48];
    snprintf(footer, sizeof(footer), "min:%.2f max:%.2f p98:%.2f L2:%.2f",
             (double) summary->ds_min,
             (double) summary->ds_max,
             (double) summary->ds_abs_p98,
             (double) summary->rel_l2);
    draw_string(4, 342, footer, EIT_LCD_COLOR_WHITE);

    /* Draw color scale bar on the right side */
    for (int i = 0; i < HEATMAP_SIZE; i++)
    {
        float t = (float) i / (float) (HEATMAP_SIZE - 1);
        float ds_val = ds_min + t * (ds_max - ds_min);
        uint16_t color = ds_to_color(ds_val, ds_min, ds_max);
        int bar_x = HEATMAP_X0 + HEATMAP_SIZE + 16;
        int bar_y = HEATMAP_Y0 + HEATMAP_SIZE - i;
        eit_lcd_draw_pixel((uint32_t) bar_x, (uint32_t) bar_y, color);
    }

    /* Swap buffers to display the new frame */
    (void) eit_lcd_buffer_swap();
}

void eit_ui_show_status(const char *message)
{
    if (!g_ui_initialized)
    {
        eit_ui_init();
    }

    /* Clear center area */
    eit_lcd_fill_rect(0, 24, 480, 316, EIT_LCD_RGB565(16, 16, 32));

    /* Center the status message */
    int msg_len = (int) strlen(message);
    int x = (480 - msg_len * 7) / 2;
    if (x < 4) x = 4;
    draw_string(x, 170, message, EIT_LCD_COLOR_WHITE);

    (void) eit_lcd_buffer_swap();
}

void eit_ui_test_color(uint16_t color)
{
    eit_lcd_clear(color);
    draw_string(200, 175, "LCD TEST", EIT_LCD_COLOR_WHITE);
    (void) eit_lcd_buffer_swap();
}
