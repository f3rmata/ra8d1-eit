#include "hal_data.h"
#include "eit_board.h"
#include "eit_recon.h"
#include "eit_sdram.h"
#include "eit_lcd.h"
#include "eit_ui.h"

#include <ctype.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#if (1 == BSP_MULTICORE_PROJECT) && BSP_TZ_SECURE_BUILD
bsp_ipc_semaphore_handle_t g_core_start_semaphore =
{
    .semaphore_num = 0
};
#endif

#define FW_VERSION              "ra8d1-eit-p0-dma-ac10k-picostat-cpha-even-v1"
#define LED1_PIN                BSP_IO_PORT_01_PIN_06
#define LED_BLINK_DELAY_MS      (500U)
#define UART9_TX_TIMEOUT_MS     (100U)
#define UART9_BAUD              (460800U)
#define UART9_BAUD_MAX_ERROR_X1000 (5000U)
#define UART9_RX_BUF_SIZE       (256U)
#define CLI_LINE_LEN            (128U)
#define ADC_MAX_SAMPLES         (4096U)
#define SCAN_MAX_ROUTES         (32U * (32U - 3U))
#define EIT_BIN_MAGIC_0         ('E')
#define EIT_BIN_MAGIC_1         ('I')
#define EIT_BIN_MAGIC_2         ('T')
#define EIT_BIN_MAGIC_3         ('B')
#define EIT_BIN_VERSION         (1U)
#define EIT_BIN_TYPE_SCANSTAT   (1U)
#define EIT_BIN_TYPE_RECONFAST  (2U)
#define EIT_BIN_HEADER_SIZE     (32U)
#define EIT_BIN_SCANSTAT_ROW_SIZE (32U)
#define EIT_BIN_SCANSTAT_ROWS_PER_CHUNK (16U)
#define EIT_BIN_RECONFAST_SUMMARY_SIZE (32U)
#define EIT_BIN_RECONFAST_NODE_STRIDE (4U)

#define SCAN_FLAG_OVERRANGE     (0x01U)
#define SCAN_FLAG_LOW_VALID     (0x02U)
#define SCAN_FLAG_PP_ABS        (0x04U)
#define SCAN_FLAG_PP_FRAME      (0x08U)
#define SCAN_FLAG_RMS_RATIO     (0x10U)
#define SCAN_FLAG_RMS_FRAME     (0x20U)
#define SCAN_DEFAULT_PP_LIMIT   (180U)
#define SCAN_DEFAULT_RETRIES    (1U)
#define SCAN_RETRY_MIN_SETTLE_MS (10U)
#define SCAN_FRAME_PP_MULT      (5U)
#define SCAN_FRAME_PP_FLOOR     (120U)
#define SCAN_RMS_RATIO_NUM      (9U)
#define SCAN_RMS_RATIO_DEN      (20U)
#define SCAN_RMS_FRAME_MULT     (3U)
#define SCAN_RMS_FRAME_FLOOR    (30U)
#define SCAN_EXCITE_HZ          (10000U)
#define SCAN_PI                 (3.14159265358979323846)
#define SCAN_ADC_VREF           (2.5f)
#define SCAN_SQRT2              (1.4142135623730950488f)
#define SCAN_RMS_MILLI_TO_AMP_V (SCAN_ADC_VREF * SCAN_SQRT2 / (1023.0f * 1000.0f))

typedef struct st_scan_stat
{
    uint32_t route_index;
    uint32_t src;
    uint32_t sink;
    uint32_t vp;
    uint32_t vn;
    uint32_t mean_milli;
    uint32_t rms_milli;
    uint16_t min_code;
    uint16_t max_code;
    uint32_t pp_code;
    uint32_t overrange_count;
    uint32_t valid_count;
    uint32_t flags;
    uint32_t raw_flags;
    uint32_t retry_count;
} scan_stat_t;

static void uart9_write_string(char const * p_text);
static void uart9_write_bytes(uint8_t const * p_data, uint32_t length);
static void uart9_configure_baud(void);
static void uart9_write_u32(uint32_t value);
static void uart9_write_i32(int32_t value);
static void uart9_write_u32_padded(uint32_t value, uint32_t width);
static void uart9_write_fixed3(uint32_t milli);
static void uart9_write_float(float value);
static void uart9_write_hex2(uint8_t value);
static void error_blink(void);
static void print_help(void);
static void process_line(char * p_line);
static bool parse_u32(char ** pp_text, uint32_t * p_value);
static bool parse_role(char ** pp_text, eit_mux_t * p_mux);
static void skip_spaces(char ** pp_text);
static void command_prompt(void);
static void command_adc(char * p);
static void command_scanraw(char * p);
static void command_scanstat(char * p);
static void command_scanstatbin(char * p);
static void command_recon(char * p);
static void command_reconfast(char * p);
static void command_reconfastbin(char * p);
static void command_recon_common(char * p, bool fast);
static void command_reconbase(char * p);
static void command_recondump(void);
static void command_lcdtest(char * p);
static void command_raw(char * p, bool all_off_first);
static void compute_scan_stat(uint16_t const * p_samples,
                              uint32_t samples,
                              uint32_t rate,
                              uint32_t pp_limit,
                              scan_stat_t * p_stat);
static bool capture_scan_stat(uint16_t * p_samples,
                              uint32_t samples,
                              uint32_t rate,
                              uint32_t settle_ms,
                              uint32_t pp_limit,
                              uint32_t retries,
                              scan_stat_t * p_stat);
static void apply_frame_outlier_filter(scan_stat_t * p_stats, uint32_t count);
static int compare_u32(const void * p_a, const void * p_b);
static bool route_is_valid(scan_stat_t const * p_stat);
static bool route_is_recon_valid(scan_stat_t const * p_stat);
static float scan_stat_amp_v(scan_stat_t const * p_stat);
static bool capture_recon_stats(uint32_t samples,
                                uint32_t rate,
                                uint32_t settle_ms,
                                uint32_t pp_limit,
                                uint32_t retries,
                                scan_stat_t stats[EIT_RECON_ROUTES]);
static void recon_stats_to_vectors(scan_stat_t const stats[EIT_RECON_ROUTES],
                                   float amp_v[EIT_RECON_ROUTES],
                                   bool valid[EIT_RECON_ROUTES],
                                   uint32_t * p_retry_count);
static void write_scanstat_binary_frame(uint32_t frame_id,
                                        uint32_t electrodes,
                                        uint32_t samples,
                                        uint32_t rate,
                                        scan_stat_t const * p_stats,
                                        uint32_t route_count);
static void write_reconfast_binary_frame(uint32_t frame_id,
                                         eit_recon_summary_t const * p_summary,
                                         float const ds_node[EIT_RECON_NODES]);
static void pack_scanstat_binary_row(scan_stat_t const * p_stat, uint8_t row[EIT_BIN_SCANSTAT_ROW_SIZE]);
static void pack_reconfast_summary(eit_recon_summary_t const * p_summary,
                                   uint8_t summary[EIT_BIN_RECONFAST_SUMMARY_SIZE]);
static void put_le16(uint8_t * p_dst, uint16_t value);
static void put_le32(uint8_t * p_dst, uint32_t value);
static void put_le_float32(uint8_t * p_dst, float value);
static uint16_t crc16_ccitt_update(uint16_t crc, uint8_t byte);
static void led_heartbeat(void);

static volatile bool g_uart9_tx_complete = false;
static volatile bool g_uart9_error = false;
static volatile uint8_t g_uart9_rx_buf[UART9_RX_BUF_SIZE];
static volatile uint16_t g_uart9_rx_head = 0U;
static volatile uint16_t g_uart9_rx_tail = 0U;
static uint32_t g_frame_id = 0U;
static uint16_t g_last_drive_gain = 512U;
static uint16_t g_last_meas_gain = 512U;
static uint16_t g_scan_hist[1024];

void hal_entry(void)
{
    fsp_err_t err = R_IOPORT_Open(&g_ioport_ctrl, &g_bsp_pin_cfg);
    if ((FSP_SUCCESS != err) && (FSP_ERR_ALREADY_OPEN != err))
    {
        __BKPT(0);
    }

    __enable_irq();

    err = g_uart9.p_api->open(g_uart9.p_ctrl, g_uart9.p_cfg);
    if (FSP_SUCCESS != err)
    {
        error_blink();
    }
    uart9_configure_baud();

    err = eit_board_init();
    if (FSP_SUCCESS != err)
    {
        uart9_write_string("EIT GPIO init failed\r\n");
        error_blink();
    }
    eit_recon_init();

    /* Initialize SDRAM first (framebuffer storage) */
    eit_sdram_init();

    /* Initialize LCD subsystem */
    eit_lcd_reset();
    eit_lcd_init();
    eit_ui_init();

    uart9_write_string("\r\nready\r\n");
    command_prompt();

    char line[CLI_LINE_LEN];
    uint32_t line_len = 0U;

    while (1)
    {
        led_heartbeat();

        while (g_uart9_rx_tail != g_uart9_rx_head)
        {
            uint8_t ch = g_uart9_rx_buf[g_uart9_rx_tail];
            g_uart9_rx_tail = (uint16_t) ((g_uart9_rx_tail + 1U) % UART9_RX_BUF_SIZE);

            if ((ch == '\r') || (ch == '\n'))
            {
                uart9_write_string("\r\n");
                line[line_len] = '\0';
                if (line_len > 0U)
                {
                    process_line(line);
                }
                line_len = 0U;
                command_prompt();
            }
            else if ((ch == 0x08U) || (ch == 0x7FU))
            {
                if (line_len > 0U)
                {
                    line_len--;
                    uart9_write_string("\b \b");
                }
            }
            else if ((ch >= 0x20U) && (ch < 0x7FU))
            {
                if (line_len < (CLI_LINE_LEN - 1U))
                {
                    line[line_len++] = (char) ch;
                    char echo[2] = { (char) ch, '\0' };
                    uart9_write_string(echo);
                }
            }
        }

        R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MILLISECONDS);
    }
}

static void process_line(char * p_line)
{
    char * p = p_line;
    skip_spaces(&p);

    char command[16] = {0};
    uint32_t len = 0U;
    while ((isalnum((unsigned char) *p) || ('_' == *p)) && (len < (sizeof(command) - 1U)))
    {
        command[len++] = (char) tolower((unsigned char) *p);
        p++;
    }

    if ((0 == strcmp(command, "h")) || (0 == strcmp(command, "help")) || (0 == strcmp(command, "?")))
    {
        print_help();
    }
    else if (0 == strcmp(command, "ver"))
    {
        uart9_write_string("version ");
        uart9_write_string(FW_VERSION);
        uart9_write_string("\r\n");
    }
    else if (0 == strcmp(command, "p"))
    {
        uint32_t en;
        uint32_t pwdn;
        uint32_t oe_n;
        if (parse_u32(&p, &en) && parse_u32(&p, &pwdn) && parse_u32(&p, &oe_n))
        {
            eit_set_power_controls(0U != en, 0U != pwdn, 0U != oe_n);
            uart9_write_string("power ok\r\n");
            if (0U != en)
            {
                R_BSP_SoftwareDelay(10U, BSP_DELAY_UNITS_MILLISECONDS);
                eit_ad5270_set(EIT_RHEO_DRIVE, g_last_drive_gain);
                eit_ad5270_set(EIT_RHEO_MEAS, g_last_meas_gain);
            }
        }
        else
        {
            uart9_write_string("usage: p en pwdn oe_n\r\n");
        }
    }
    else if (0 == strcmp(command, "g"))
    {
        uint32_t drive;
        uint32_t meas;
        if (parse_u32(&p, &drive) && parse_u32(&p, &meas) && (drive <= 1023U) && (meas <= 1023U))
        {
            g_last_drive_gain = (uint16_t) drive;
            g_last_meas_gain = (uint16_t) meas;
            eit_ad5270_set(EIT_RHEO_DRIVE, (uint16_t) drive);
            eit_ad5270_set(EIT_RHEO_MEAS, (uint16_t) meas);
            uart9_write_string("gain drive=");
            uart9_write_u32(drive);
            uart9_write_string(" meas=");
            uart9_write_u32(meas);
            uart9_write_string("\r\n");
        }
        else
        {
            uart9_write_string("usage: g drive meas\r\n");
        }
    }
    else if (0 == strcmp(command, "raw"))
    {
        command_raw(p, false);
    }
    else if (0 == strcmp(command, "rawonly"))
    {
        command_raw(p, true);
    }
    else if (0 == strcmp(command, "off"))
    {
        if (eit_mux_all_off())
        {
            uart9_write_string("all mux off cmd=0x80\r\n");
        }
        else
        {
            uart9_write_string("ERR: mux off spi_error cmd=0x80\r\n");
        }
    }
    else if (0 == strcmp(command, "adc"))
    {
        command_adc(p);
    }
    else if (0 == strcmp(command, "scanraw"))
    {
        command_scanraw(p);
    }
    else if (0 == strcmp(command, "scanstat"))
    {
        command_scanstat(p);
    }
    else if (0 == strcmp(command, "scanstatbin"))
    {
        command_scanstatbin(p);
    }
    else if (0 == strcmp(command, "recon"))
    {
        command_recon(p);
    }
    else if (0 == strcmp(command, "reconfast"))
    {
        command_reconfast(p);
    }
    else if (0 == strcmp(command, "reconfastbin"))
    {
        command_reconfastbin(p);
    }
    else if (0 == strcmp(command, "reconbase"))
    {
        command_reconbase(p);
    }
    else if (0 == strcmp(command, "recondump"))
    {
        command_recondump();
    }
    else if ((0 == strcmp(command, "lcd")) || (0 == strcmp(command, "lcdtest")))
    {
        command_lcdtest(p);
    }
    else
    {
        uart9_write_string("bad command; use h\r\n");
    }
}

static void print_help(void)
{
    uart9_write_string("\r\n");
    uart9_write_string("RA8D1 EIT control\r\n");
    uart9_write_string("Version: ");
    uart9_write_string(FW_VERSION);
    uart9_write_string("\r\n");
    uart9_write_string("Commands:\r\n");
    uart9_write_string("  h                          help\r\n");
    uart9_write_string("  ver                        firmware version\r\n");
    uart9_write_string("  p en pwdn oe_n             ADC usually uses p 1 0 0\r\n");
    uart9_write_string("  g drive meas               AD5270 RDACs, 0..1023\r\n");
    uart9_write_string("  raw role channel [enable]  role=src/sink/vp/vn, channel N=S(N+1)\r\n");
    uart9_write_string("  rawonly role channel [en]  all off, then enable one ADG731 channel\r\n");
    uart9_write_string("  off                        all ADG731 off\r\n");
    uart9_write_string("  adc [samples] [rate_hz]    Port0 timed ADC capture\r\n");
    uart9_write_string("  scanraw [elec] [samples] [settle_ms] [rate_hz] [pp_limit] [retries]\r\n");
    uart9_write_string("  scanstat [elec] [samples] [settle_ms] [rate_hz] [pp_limit] [retries] [progress]\r\n");
    uart9_write_string("  scanstatbin [elec] [samples] [settle_ms] [rate_hz] [pp_limit] [retries]\r\n");
    uart9_write_string("  recon 8 256 20 200000 180 1          one MCU-side JAC frame\r\n");
    uart9_write_string("  reconfast 8 256 20 200000 180 1      compact ds-only MCU-side JAC frame\r\n");
    uart9_write_string("  reconfastbin 8 256 20 200000 180 1   binary ds-only MCU-side JAC frame\r\n");
    uart9_write_string("  reconbase 8 N 256 20 200000 180 1    average N valid frames into RAM baseline\r\n");
    uart9_write_string("  recondump                             print reconstruction model metadata\r\n");
    uart9_write_string("  lcd [color]                           LCD test: no arg=RGB cycle, arg=0xRRGGBB fill\r\n");
    eit_board_print_signals(uart9_write_string);
    uart9_write_string("\r\n");
}

static void command_prompt(void)
{
    uart9_write_string("eit> ");
}

static void command_raw(char * p, bool all_off_first)
{
    eit_mux_t mux;
    uint32_t channel;
    uint32_t enable = 1U;
    if (!parse_role(&p, &mux) || !parse_u32(&p, &channel))
    {
        uart9_write_string(all_off_first ? "usage: rawonly role channel [enable]\r\n" : "usage: raw role channel [enable]\r\n");
        return;
    }
    (void) parse_u32(&p, &enable);
    if (channel > 31U)
    {
        uart9_write_string("channel must be 0..31\r\n");
        return;
    }

    bool ok = true;
    if (all_off_first)
    {
        ok = eit_mux_all_off();
        R_BSP_SoftwareDelay(10U, BSP_DELAY_UNITS_MICROSECONDS);
    }
    if (0U != enable)
    {
        if (!eit_mux_write(mux, (uint8_t) channel, true))
        {
            ok = false;
        }
    }
    else
    {
        if (!eit_mux_write(mux, 0U, false))
        {
            ok = false;
        }
    }

    if (!ok)
    {
        uart9_write_string(all_off_first ? "rawonly spi_error cmd=0x" : "raw spi_error cmd=0x");
    }
    else
    {
        uart9_write_string(all_off_first ? "rawonly ok cmd=0x" : "raw ok cmd=0x");
    }
    uart9_write_hex2(eit_mux_command((uint8_t) channel, 0U != enable));
    uart9_write_string("\r\n");
}

static void command_adc(char * p)
{
    uint32_t samples = 1024U;
    uint32_t rate = 200000U;
    (void) parse_u32(&p, &samples);
    (void) parse_u32(&p, &rate);

    if (samples < 1U)
    {
        samples = 1U;
    }
    if (samples > ADC_MAX_SAMPLES)
    {
        samples = ADC_MAX_SAMPLES;
    }
    if (rate < 1000U)
    {
        rate = 1000U;
    }

    static uint16_t decoded[ADC_MAX_SAMPLES];
    if (!eit_adc_capture(decoded, samples, rate))
    {
        uart9_write_string("ERR: route or adc capture unavailable\r\n");
        return;
    }

    uart9_write_string("ADC_BEGIN,");
    uart9_write_u32(samples);
    uart9_write_string(",");
    uart9_write_u32(rate);
    uart9_write_string(",ADC0_LSB\r\n");
    uart9_write_string("i,value\r\n");
    for (uint32_t i = 0U; i < samples; i++)
    {
        uart9_write_u32(i);
        uart9_write_string(",");
        uart9_write_u32(decoded[i]);
        uart9_write_string("\r\n");
        led_heartbeat();
    }
    uart9_write_string("ADC_END\r\n");
}

static void command_scanraw(char * p)
{
    uint32_t electrodes = 8U;
    uint32_t samples = 128U;
    uint32_t settle_ms = 2U;
    uint32_t rate = 200000U;
    uint32_t pp_limit = SCAN_DEFAULT_PP_LIMIT;
    uint32_t retries = SCAN_DEFAULT_RETRIES;
    (void) parse_u32(&p, &electrodes);
    (void) parse_u32(&p, &samples);
    (void) parse_u32(&p, &settle_ms);
    (void) parse_u32(&p, &rate);
    (void) parse_u32(&p, &pp_limit);
    (void) parse_u32(&p, &retries);

    if (electrodes < 4U)
    {
        electrodes = 4U;
    }
    if (electrodes > 32U)
    {
        electrodes = 32U;
    }
    if (samples > ADC_MAX_SAMPLES)
    {
        samples = ADC_MAX_SAMPLES;
    }
    if (retries > 3U)
    {
        retries = 3U;
    }

    static uint16_t decoded[ADC_MAX_SAMPLES];
    g_frame_id++;
    uart9_write_string("FRAME_BEGIN,");
    uart9_write_u32(g_frame_id);
    uart9_write_string(",");
    uart9_write_u32(electrodes);
    uart9_write_string(",");
    uart9_write_u32(samples);
    uart9_write_string(",");
    uart9_write_u32(rate);
    uart9_write_string(",raw\r\n");

    for (uint32_t src = 0U; src < electrodes; src++)
    {
        uint32_t sink = (src + 1U) % electrodes;
        for (uint32_t vp = 0U; vp < electrodes; vp++)
        {
            uint32_t vn = (vp + 1U) % electrodes;
            if ((vp == src) || (vp == sink) || (vn == src) || (vn == sink))
            {
                continue;
            }

            scan_stat_t stat;
            memset(&stat, 0, sizeof(stat));
            stat.src = src;
            stat.sink = sink;
            stat.vp = vp;
            stat.vn = vn;

            if (!capture_scan_stat(decoded, samples, rate, settle_ms, pp_limit, retries, &stat))
            {
                eit_mux_all_off();
                uart9_write_string("ERR: route or adc capture unavailable\r\n");
                return;
            }
            eit_mux_all_off();

            uart9_write_string("ROUTE,");
            uart9_write_u32(src);
            uart9_write_string(",");
            uart9_write_u32(sink);
            uart9_write_string(",");
            uart9_write_u32(vp);
            uart9_write_string(",");
            uart9_write_u32(vn);
            uart9_write_string("\r\n");
            uart9_write_string("ROUTE_STAT,");
            uart9_write_u32(stat.flags);
            uart9_write_string(",");
            uart9_write_u32(stat.retry_count);
            uart9_write_string(",");
            uart9_write_u32(stat.raw_flags);
            uart9_write_string(",");
            uart9_write_u32(stat.pp_code);
            uart9_write_string("\r\n");

            for (uint32_t i = 0U; i < samples; i++)
            {
                uint32_t overrange = ((decoded[i] == 0U) || (decoded[i] >= 1023U)) ? 1U : 0U;
                uart9_write_u32(i);
                uart9_write_string(",");
                uart9_write_u32(decoded[i]);
                uart9_write_string(",");
                uart9_write_u32(overrange);
                uart9_write_string("\r\n");
            }
            uart9_write_string("END\r\n");
            led_heartbeat();
        }
    }
    eit_mux_all_off();
    uart9_write_string("SCAN_DONE\r\n");
}

static void command_scanstat(char * p)
{
    uint32_t electrodes = 8U;
    uint32_t samples = 128U;
    uint32_t settle_ms = 2U;
    uint32_t rate = 200000U;
    uint32_t pp_limit = SCAN_DEFAULT_PP_LIMIT;
    uint32_t retries = SCAN_DEFAULT_RETRIES;
    uint32_t progress = 0U;
    (void) parse_u32(&p, &electrodes);
    (void) parse_u32(&p, &samples);
    (void) parse_u32(&p, &settle_ms);
    (void) parse_u32(&p, &rate);
    (void) parse_u32(&p, &pp_limit);
    (void) parse_u32(&p, &retries);
    (void) parse_u32(&p, &progress);

    if (electrodes < 4U)
    {
        electrodes = 4U;
    }
    if (electrodes > 32U)
    {
        electrodes = 32U;
    }
    if (samples > ADC_MAX_SAMPLES)
    {
        samples = ADC_MAX_SAMPLES;
    }
    if (retries > 3U)
    {
        retries = 3U;
    }

    static uint16_t decoded[ADC_MAX_SAMPLES];
    static scan_stat_t stats[SCAN_MAX_ROUTES];
    g_frame_id++;
    uart9_write_string("STAT_BEGIN,");
    uart9_write_u32(g_frame_id);
    uart9_write_string(",");
    uart9_write_u32(electrodes);
    uart9_write_string(",");
    uart9_write_u32(samples);
    uart9_write_string(",");
    uart9_write_u32(rate);
    uart9_write_string("\r\n");
    uart9_write_string("route,src,sink,vp,vn,mean_code,min_code,max_code,pp_code,rms_code,overrange_count,valid_count,flags,retry_count,raw_flags\r\n");

    uint32_t route_index = 0U;
    for (uint32_t src = 0U; src < electrodes; src++)
    {
        uint32_t sink = (src + 1U) % electrodes;
        for (uint32_t vp = 0U; vp < electrodes; vp++)
        {
            uint32_t vn = (vp + 1U) % electrodes;
            if ((vp == src) || (vp == sink) || (vn == src) || (vn == sink))
            {
                continue;
            }

            scan_stat_t * p_stat = &stats[route_index];
            memset(p_stat, 0, sizeof(*p_stat));
            p_stat->route_index = route_index;
            p_stat->src = src;
            p_stat->sink = sink;
            p_stat->vp = vp;
            p_stat->vn = vn;

            if (0U != progress)
            {
                uart9_write_string("STAT_ROUTE_BEGIN,");
                uart9_write_u32(p_stat->route_index);
                uart9_write_string(",");
                uart9_write_u32(p_stat->src);
                uart9_write_string(",");
                uart9_write_u32(p_stat->sink);
                uart9_write_string(",");
                uart9_write_u32(p_stat->vp);
                uart9_write_string(",");
                uart9_write_u32(p_stat->vn);
                uart9_write_string("\r\n");
            }

            if (!capture_scan_stat(decoded, samples, rate, settle_ms, pp_limit, retries, p_stat))
            {
                eit_mux_all_off();
                uart9_write_string("ERR: route or adc capture unavailable route=");
                uart9_write_u32(p_stat->route_index);
                uart9_write_string("\r\n");
                return;
            }
            eit_mux_all_off();
            if (0U != progress)
            {
                uart9_write_string("STAT_ROUTE_DONE,");
                uart9_write_u32(p_stat->route_index);
                uart9_write_string(",");
                uart9_write_u32(p_stat->flags);
                uart9_write_string(",");
                uart9_write_u32(p_stat->pp_code);
                uart9_write_string("\r\n");
            }
            route_index++;
            led_heartbeat();
        }
    }

    apply_frame_outlier_filter(stats, route_index);

    for (uint32_t i = 0U; i < route_index; i++)
    {
        scan_stat_t const * p_stat = &stats[i];
        uart9_write_u32(p_stat->route_index);
        uart9_write_string(",");
        uart9_write_u32(p_stat->src);
        uart9_write_string(",");
        uart9_write_u32(p_stat->sink);
        uart9_write_string(",");
        uart9_write_u32(p_stat->vp);
        uart9_write_string(",");
        uart9_write_u32(p_stat->vn);
        uart9_write_string(",");
        uart9_write_fixed3(p_stat->mean_milli);
        uart9_write_string(",");
        uart9_write_u32(p_stat->min_code);
        uart9_write_string(",");
        uart9_write_u32(p_stat->max_code);
        uart9_write_string(",");
        uart9_write_u32(p_stat->pp_code);
        uart9_write_string(",");
        uart9_write_fixed3(p_stat->rms_milli);
        uart9_write_string(",");
        uart9_write_u32(p_stat->overrange_count);
        uart9_write_string(",");
        uart9_write_u32(p_stat->valid_count);
        uart9_write_string(",");
        uart9_write_u32(p_stat->flags);
        uart9_write_string(",");
        uart9_write_u32(p_stat->retry_count);
        uart9_write_string(",");
        uart9_write_u32(p_stat->raw_flags);
        uart9_write_string("\r\n");
    }

    eit_mux_all_off();
    uart9_write_string("STAT_DONE\r\n");
}

static void command_scanstatbin(char * p)
{
    uint32_t electrodes = 8U;
    uint32_t samples = 128U;
    uint32_t settle_ms = 2U;
    uint32_t rate = 200000U;
    uint32_t pp_limit = SCAN_DEFAULT_PP_LIMIT;
    uint32_t retries = SCAN_DEFAULT_RETRIES;
    (void) parse_u32(&p, &electrodes);
    (void) parse_u32(&p, &samples);
    (void) parse_u32(&p, &settle_ms);
    (void) parse_u32(&p, &rate);
    (void) parse_u32(&p, &pp_limit);
    (void) parse_u32(&p, &retries);

    if (electrodes < 4U)
    {
        electrodes = 4U;
    }
    if (electrodes > 32U)
    {
        electrodes = 32U;
    }
    if (samples > ADC_MAX_SAMPLES)
    {
        samples = ADC_MAX_SAMPLES;
    }
    if (retries > 3U)
    {
        retries = 3U;
    }

    static uint16_t decoded[ADC_MAX_SAMPLES];
    static scan_stat_t stats[SCAN_MAX_ROUTES];
    g_frame_id++;

    uint32_t route_index = 0U;
    for (uint32_t src = 0U; src < electrodes; src++)
    {
        uint32_t sink = (src + 1U) % electrodes;
        for (uint32_t vp = 0U; vp < electrodes; vp++)
        {
            uint32_t vn = (vp + 1U) % electrodes;
            if ((vp == src) || (vp == sink) || (vn == src) || (vn == sink))
            {
                continue;
            }

            scan_stat_t * p_stat = &stats[route_index];
            memset(p_stat, 0, sizeof(*p_stat));
            p_stat->route_index = route_index;
            p_stat->src = src;
            p_stat->sink = sink;
            p_stat->vp = vp;
            p_stat->vn = vn;

            if (!capture_scan_stat(decoded, samples, rate, settle_ms, pp_limit, retries, p_stat))
            {
                eit_mux_all_off();
                uart9_write_string("ERR: route or adc capture unavailable route=");
                uart9_write_u32(p_stat->route_index);
                uart9_write_string("\r\n");
                return;
            }
            eit_mux_all_off();
            route_index++;
            led_heartbeat();
        }
    }

    apply_frame_outlier_filter(stats, route_index);
    write_scanstat_binary_frame(g_frame_id, electrodes, samples, rate, stats, route_index);
}

static void command_recon(char * p)
{
    command_recon_common(p, false);
}

static void command_reconfast(char * p)
{
    command_recon_common(p, true);
}

static void command_reconfastbin(char * p)
{
    uint32_t electrodes = EIT_RECON_ELECTRODES;
    uint32_t samples = 256U;
    uint32_t settle_ms = 20U;
    uint32_t rate = 200000U;
    uint32_t pp_limit = SCAN_DEFAULT_PP_LIMIT;
    uint32_t retries = SCAN_DEFAULT_RETRIES;
    (void) parse_u32(&p, &electrodes);
    (void) parse_u32(&p, &samples);
    (void) parse_u32(&p, &settle_ms);
    (void) parse_u32(&p, &rate);
    (void) parse_u32(&p, &pp_limit);
    (void) parse_u32(&p, &retries);

    if (electrodes != EIT_RECON_ELECTRODES)
    {
        uart9_write_string("ERR: recon model supports exactly 8 electrodes\r\n");
        return;
    }
    if (samples < 1U)
    {
        samples = 1U;
    }
    if (samples > ADC_MAX_SAMPLES)
    {
        samples = ADC_MAX_SAMPLES;
    }
    if (retries > 3U)
    {
        retries = 3U;
    }

    static scan_stat_t stats[EIT_RECON_ROUTES];
    static float amp_v[EIT_RECON_ROUTES];
    static bool valid[EIT_RECON_ROUTES];
    static float ds_node[EIT_RECON_NODES];
    uint32_t retry_count = 0U;
    eit_recon_summary_t summary;

    g_frame_id++;
    if (!capture_recon_stats(samples, rate, settle_ms, pp_limit, retries, stats))
    {
        eit_mux_all_off();
        uart9_write_string("ERR: recon capture failed\r\n");
        return;
    }

    recon_stats_to_vectors(stats, amp_v, valid, &retry_count);
    eit_recon_solve(amp_v, valid, retry_count, ds_node, &summary);

    /* Display reconstruction result on LCD */
    eit_ui_show_recon_frame(ds_node, &summary, g_frame_id);

    write_reconfast_binary_frame(g_frame_id, &summary, ds_node);
}

static void command_recon_common(char * p, bool fast)
{
    uint32_t electrodes = EIT_RECON_ELECTRODES;
    uint32_t samples = 256U;
    uint32_t settle_ms = 20U;
    uint32_t rate = 200000U;
    uint32_t pp_limit = SCAN_DEFAULT_PP_LIMIT;
    uint32_t retries = SCAN_DEFAULT_RETRIES;
    (void) parse_u32(&p, &electrodes);
    (void) parse_u32(&p, &samples);
    (void) parse_u32(&p, &settle_ms);
    (void) parse_u32(&p, &rate);
    (void) parse_u32(&p, &pp_limit);
    (void) parse_u32(&p, &retries);

    if (electrodes != EIT_RECON_ELECTRODES)
    {
        uart9_write_string("ERR: recon model supports exactly 8 electrodes\r\n");
        return;
    }
    if (samples < 1U)
    {
        samples = 1U;
    }
    if (samples > ADC_MAX_SAMPLES)
    {
        samples = ADC_MAX_SAMPLES;
    }
    if (retries > 3U)
    {
        retries = 3U;
    }

    static scan_stat_t stats[EIT_RECON_ROUTES];
    static float amp_v[EIT_RECON_ROUTES];
    static bool valid[EIT_RECON_ROUTES];
    static float ds_node[EIT_RECON_NODES];
    uint32_t retry_count = 0U;
    eit_recon_summary_t summary;

    g_frame_id++;
    if (!capture_recon_stats(samples, rate, settle_ms, pp_limit, retries, stats))
    {
        eit_mux_all_off();
        uart9_write_string("ERR: recon capture failed\r\n");
        return;
    }

    recon_stats_to_vectors(stats, amp_v, valid, &retry_count);
    eit_recon_solve(amp_v, valid, retry_count, ds_node, &summary);

    /* Display reconstruction result on LCD */
    eit_ui_show_recon_frame(ds_node, &summary, g_frame_id);

    uart9_write_string(fast ? "RECONFAST_BEGIN," : "RECON_BEGIN,");
    uart9_write_u32(g_frame_id);
    uart9_write_string(",");
    uart9_write_u32(EIT_RECON_ELECTRODES);
    uart9_write_string(",");
    uart9_write_u32(EIT_RECON_ROUTES);
    uart9_write_string(",");
    uart9_write_u32(EIT_RECON_NODES);
    uart9_write_string("\r\n");

    uart9_write_string("RECON_SUMMARY,");
    uart9_write_u32(summary.valid_count);
    uart9_write_string(",");
    uart9_write_u32(summary.invalid_count);
    uart9_write_string(",");
    uart9_write_u32(summary.retry_count);
    uart9_write_string(",");
    uart9_write_float(summary.ds_min);
    uart9_write_string(",");
    uart9_write_float(summary.ds_max);
    uart9_write_string(",");
    uart9_write_float(summary.ds_abs_p98);
    uart9_write_string(",");
    uart9_write_float(summary.rel_l2);
    uart9_write_string("\r\n");

    if (fast)
    {
        uart9_write_string("RECONFAST_DS");
        for (uint32_t node = 0U; node < EIT_RECON_NODES; node++)
        {
            uart9_write_string(",");
            uart9_write_float(ds_node[node]);
        }
        uart9_write_string("\r\n");
        uart9_write_string("RECONFAST_DONE\r\n");
    }
    else
    {
        uart9_write_string("node,x,y,ds\r\n");
        for (uint32_t node = 0U; node < EIT_RECON_NODES; node++)
        {
            uart9_write_u32(node);
            uart9_write_string(",");
            uart9_write_float(g_eit_recon_node_xy[node][0]);
            uart9_write_string(",");
            uart9_write_float(g_eit_recon_node_xy[node][1]);
            uart9_write_string(",");
            uart9_write_float(ds_node[node]);
            uart9_write_string("\r\n");
            led_heartbeat();
        }
        uart9_write_string("RECON_DONE\r\n");
    }
}

static void command_reconbase(char * p)
{
    uint32_t electrodes = EIT_RECON_ELECTRODES;
    uint32_t frames = 5U;
    uint32_t samples = 256U;
    uint32_t settle_ms = 20U;
    uint32_t rate = 200000U;
    uint32_t pp_limit = SCAN_DEFAULT_PP_LIMIT;
    uint32_t retries = SCAN_DEFAULT_RETRIES;
    (void) parse_u32(&p, &electrodes);
    (void) parse_u32(&p, &frames);
    (void) parse_u32(&p, &samples);
    (void) parse_u32(&p, &settle_ms);
    (void) parse_u32(&p, &rate);
    (void) parse_u32(&p, &pp_limit);
    (void) parse_u32(&p, &retries);

    if (electrodes != EIT_RECON_ELECTRODES)
    {
        uart9_write_string("ERR: recon model supports exactly 8 electrodes\r\n");
        return;
    }
    if (frames < 1U)
    {
        frames = 1U;
    }
    if (samples < 1U)
    {
        samples = 1U;
    }
    if (samples > ADC_MAX_SAMPLES)
    {
        samples = ADC_MAX_SAMPLES;
    }
    if (retries > 3U)
    {
        retries = 3U;
    }

    static scan_stat_t stats[EIT_RECON_ROUTES];
    static float amp_v[EIT_RECON_ROUTES];
    static bool valid[EIT_RECON_ROUTES];
    eit_recon_baseline_accum_clear();

    uart9_write_string("RECONBASE_BEGIN,");
    uart9_write_u32(frames);
    uart9_write_string(",");
    uart9_write_u32(EIT_RECON_ROUTES);
    uart9_write_string("\r\n");

    for (uint32_t frame = 0U; frame < frames; frame++)
    {
        uint32_t retry_count = 0U;
        uint32_t valid_count = 0U;
        uint32_t invalid_count = 0U;
        g_frame_id++;
        if (!capture_recon_stats(samples, rate, settle_ms, pp_limit, retries, stats))
        {
            eit_mux_all_off();
            uart9_write_string("ERR: reconbase capture failed\r\n");
            return;
        }

        recon_stats_to_vectors(stats, amp_v, valid, &retry_count);
        eit_recon_baseline_accum_add(amp_v, valid);
        for (uint32_t route = 0U; route < EIT_RECON_ROUTES; route++)
        {
            if (valid[route])
            {
                valid_count++;
            }
            else
            {
                invalid_count++;
            }
        }

        uart9_write_string("RECONBASE_FRAME,");
        uart9_write_u32(g_frame_id);
        uart9_write_string(",");
        uart9_write_u32(valid_count);
        uart9_write_string(",");
        uart9_write_u32(invalid_count);
        uart9_write_string(",");
        uart9_write_u32(retry_count);
        uart9_write_string("\r\n");
    }

    uint32_t updated_routes = eit_recon_baseline_accum_valid_routes();
    bool complete = eit_recon_baseline_accum_commit();
    if (0U == updated_routes)
    {
        uart9_write_string("ERR: reconbase no valid routes; keep previous baseline\r\n");
        return;
    }

    uart9_write_string("RECONBASE_DONE,");
    uart9_write_u32(frames);
    uart9_write_string(complete ? ",ram," : ",ram_partial,");
    uart9_write_u32(updated_routes);
    uart9_write_string(",");
    uart9_write_u32(EIT_RECON_ROUTES - updated_routes);
    uart9_write_string("\r\n");
}

static void command_lcdtest(char * p)
{
    uint32_t color = 0U;
    (void) parse_u32(&p, &color);

    if (0U == color)
    {
        /* Cycle through RGB test screens */
        uart9_write_string("lcd test: RED\r\n");
        eit_ui_test_color(EIT_LCD_COLOR_RED);
        R_BSP_SoftwareDelay(2000U, BSP_DELAY_UNITS_MILLISECONDS);

        uart9_write_string("lcd test: GREEN\r\n");
        eit_ui_test_color(EIT_LCD_COLOR_GREEN);
        R_BSP_SoftwareDelay(2000U, BSP_DELAY_UNITS_MILLISECONDS);

        uart9_write_string("lcd test: BLUE\r\n");
        eit_ui_test_color(EIT_LCD_COLOR_BLUE);
        R_BSP_SoftwareDelay(2000U, BSP_DELAY_UNITS_MILLISECONDS);

        uart9_write_string("lcd test: BLACK\r\n");
        eit_ui_test_color(EIT_LCD_COLOR_BLACK);
        R_BSP_SoftwareDelay(1000U, BSP_DELAY_UNITS_MILLISECONDS);

        eit_ui_init();
        uart9_write_string("lcd test: done\r\n");
    }
    else
    {
        uart9_write_string("lcd fill: 0x");
        uart9_write_hex2((uint8_t) ((color >> 8) & 0xFFU));
        uart9_write_hex2((uint8_t) (color & 0xFFU));
        uart9_write_string("\r\n");
        eit_ui_test_color((uint16_t) color);
    }
}

static void command_recondump(void)
{
    uart9_write_string("RECONDUMP,");
    uart9_write_string(EIT_RECON_MODEL_VERSION);
    uart9_write_string(",");
    uart9_write_u32(EIT_RECON_ELECTRODES);
    uart9_write_string(",");
    uart9_write_u32(EIT_RECON_ROUTES);
    uart9_write_string(",");
    uart9_write_u32(EIT_RECON_NODES);
    uart9_write_string(",");
    uart9_write_u32(EIT_RECON_ELEMENTS);
    uart9_write_string(",");
    uart9_write_string(eit_recon_baseline_is_ram() ? "ram" : "flash");
    uart9_write_string(",");
    uart9_write_string(eit_recon_active_baseline_source());
    uart9_write_string("\r\n");
}

static bool capture_recon_stats(uint32_t samples,
                                uint32_t rate,
                                uint32_t settle_ms,
                                uint32_t pp_limit,
                                uint32_t retries,
                                scan_stat_t stats[EIT_RECON_ROUTES])
{
    static uint16_t decoded[ADC_MAX_SAMPLES];
    uint32_t route_index = 0U;

    for (uint32_t src = 0U; src < EIT_RECON_ELECTRODES; src++)
    {
        uint32_t sink = (src + 1U) % EIT_RECON_ELECTRODES;
        for (uint32_t vp = 0U; vp < EIT_RECON_ELECTRODES; vp++)
        {
            uint32_t vn = (vp + 1U) % EIT_RECON_ELECTRODES;
            if ((vp == src) || (vp == sink) || (vn == src) || (vn == sink))
            {
                continue;
            }
            if ((route_index >= EIT_RECON_ROUTES) ||
                !eit_recon_route_matches(route_index, src, sink, vp, vn))
            {
                uart9_write_string("ERR: recon model route order mismatch\r\n");
                return false;
            }

            scan_stat_t * p_stat = &stats[route_index];
            memset(p_stat, 0, sizeof(*p_stat));
            p_stat->route_index = route_index;
            p_stat->src = src;
            p_stat->sink = sink;
            p_stat->vp = vp;
            p_stat->vn = vn;

            if (!capture_scan_stat(decoded, samples, rate, settle_ms, pp_limit, retries, p_stat))
            {
                return false;
            }
            eit_mux_all_off();
            route_index++;
            led_heartbeat();
        }
    }

    if (route_index != EIT_RECON_ROUTES)
    {
        uart9_write_string("ERR: recon route count mismatch\r\n");
        return false;
    }

    apply_frame_outlier_filter(stats, EIT_RECON_ROUTES);
    return true;
}

static void recon_stats_to_vectors(scan_stat_t const stats[EIT_RECON_ROUTES],
                                   float amp_v[EIT_RECON_ROUTES],
                                   bool valid[EIT_RECON_ROUTES],
                                   uint32_t * p_retry_count)
{
    uint32_t retry_count = 0U;
    for (uint32_t route = 0U; route < EIT_RECON_ROUTES; route++)
    {
        amp_v[route] = scan_stat_amp_v(&stats[route]);
        valid[route] = route_is_recon_valid(&stats[route]);
        retry_count += stats[route].retry_count;
    }
    *p_retry_count = retry_count;
}

static bool capture_scan_stat(uint16_t * p_samples,
                              uint32_t samples,
                              uint32_t rate,
                              uint32_t settle_ms,
                              uint32_t pp_limit,
                              uint32_t retries,
                              scan_stat_t * p_stat)
{
    if (!eit_route((uint8_t) p_stat->src, (uint8_t) p_stat->sink, (uint8_t) p_stat->vp, (uint8_t) p_stat->vn))
    {
        return false;
    }
    R_BSP_SoftwareDelay(settle_ms, BSP_DELAY_UNITS_MILLISECONDS);

    if (!eit_adc_capture(p_samples, samples, rate))
    {
        return false;
    }
    compute_scan_stat(p_samples, samples, rate, pp_limit, p_stat);
    p_stat->raw_flags = p_stat->flags;
    p_stat->retry_count = 0U;

    uint32_t retry_settle_ms = settle_ms;
    if (retry_settle_ms < SCAN_RETRY_MIN_SETTLE_MS)
    {
        retry_settle_ms = SCAN_RETRY_MIN_SETTLE_MS;
    }

    while ((p_stat->flags != 0U) && (p_stat->retry_count < retries))
    {
        if (!eit_route((uint8_t) p_stat->src, (uint8_t) p_stat->sink, (uint8_t) p_stat->vp, (uint8_t) p_stat->vn))
        {
            return false;
        }
        R_BSP_SoftwareDelay(retry_settle_ms, BSP_DELAY_UNITS_MILLISECONDS);
        if (!eit_adc_capture(p_samples, samples, rate))
        {
            return false;
        }
        p_stat->retry_count++;
        compute_scan_stat(p_samples, samples, rate, pp_limit, p_stat);
    }

    return true;
}

static void compute_scan_stat(uint16_t const * p_samples,
                              uint32_t samples,
                              uint32_t rate,
                              uint32_t pp_limit,
                              scan_stat_t * p_stat)
{
    uint16_t * hist = g_scan_hist;
    double sum = 0.0;
    double sum_sq = 0.0;
    uint32_t overrange_count = 0U;
    uint32_t valid_count = 0U;

    memset(hist, 0, sizeof(g_scan_hist));
    for (uint32_t i = 0U; i < samples; i++)
    {
        uint16_t value = p_samples[i];
        if (value < 1024U)
        {
            hist[value]++;
        }
        if ((value <= 2U) || (value >= 1021U))
        {
            overrange_count++;
            continue;
        }
        valid_count++;
        sum += (double) value;
        sum_sq += (double) value * (double) value;
    }

    uint32_t trim = valid_count / 100U;
    if ((trim < 1U) && (valid_count > 2U))
    {
        trim = 1U;
    }

    uint16_t min_code = 0U;
    uint16_t max_code = 0U;
    if (valid_count > 0U)
    {
        uint32_t skipped = 0U;
        for (uint16_t code = 3U; code <= 1020U; code++)
        {
            uint32_t count = hist[code];
            if ((skipped + count) > trim)
            {
                min_code = code;
                break;
            }
            skipped += count;
        }

        skipped = 0U;
        for (int32_t code = 1020; code >= 3; code--)
        {
            uint32_t count = hist[code];
            if ((skipped + count) > trim)
            {
                max_code = (uint16_t) code;
                break;
            }
            skipped += count;
        }
    }

    double mean = valid_count ? (sum / (double) valid_count) : 0.0;
    double variance = valid_count ? ((sum_sq / (double) valid_count) - (mean * mean)) : 0.0;
    if (variance < 0.0)
    {
        variance = 0.0;
    }
    double rms = sqrt(variance);

    (void) rate;
    p_stat->mean_milli = (uint32_t) ((mean * 1000.0) + 0.5);
    p_stat->rms_milli = (uint32_t) ((rms * 1000.0) + 0.5);
    p_stat->min_code = min_code;
    p_stat->max_code = max_code;
    p_stat->pp_code = (max_code >= min_code) ? (uint32_t) (max_code - min_code) : 0U;
    p_stat->overrange_count = overrange_count;
    p_stat->valid_count = valid_count;
    p_stat->flags = 0U;

    if (overrange_count > 0U)
    {
        p_stat->flags |= SCAN_FLAG_OVERRANGE;
    }
    if (valid_count < ((samples * 3U) / 4U))
    {
        p_stat->flags |= SCAN_FLAG_LOW_VALID;
    }
    (void) pp_limit;
}

static void apply_frame_outlier_filter(scan_stat_t * p_stats, uint32_t count)
{
    static uint32_t pp_values[SCAN_MAX_ROUTES];
    static uint32_t rms_values[SCAN_MAX_ROUTES];
    uint32_t pp_count = 0U;
    uint32_t rms_count = 0U;

    for (uint32_t i = 0U; i < count; i++)
    {
        if (route_is_valid(&p_stats[i]))
        {
            pp_values[pp_count++] = p_stats[i].pp_code;
            rms_values[rms_count++] = p_stats[i].rms_milli;
        }
    }
    if ((pp_count < 4U) || (rms_count < 4U))
    {
        return;
    }

    qsort(pp_values, pp_count, sizeof(pp_values[0]), compare_u32);
    qsort(rms_values, rms_count, sizeof(rms_values[0]), compare_u32);
    uint32_t median_pp = pp_values[pp_count / 2U];
    uint32_t median_rms = rms_values[rms_count / 2U];
    uint32_t frame_limit = median_pp * SCAN_FRAME_PP_MULT;
    if (frame_limit < SCAN_FRAME_PP_FLOOR)
    {
        frame_limit = SCAN_FRAME_PP_FLOOR;
    }
    uint32_t rms_frame_limit = median_rms * SCAN_RMS_FRAME_MULT;
    if (rms_frame_limit < (SCAN_RMS_FRAME_FLOOR * 1000U))
    {
        rms_frame_limit = SCAN_RMS_FRAME_FLOOR * 1000U;
    }

    for (uint32_t i = 0U; i < count; i++)
    {
        if ((p_stats[i].flags == 0U) && (p_stats[i].pp_code > frame_limit))
        {
            p_stats[i].flags |= SCAN_FLAG_PP_FRAME;
        }
        if ((p_stats[i].flags == 0U) && (p_stats[i].rms_milli > rms_frame_limit))
        {
            p_stats[i].flags |= SCAN_FLAG_RMS_FRAME;
        }
    }
}

static bool route_is_valid(scan_stat_t const * p_stat)
{
    return (p_stat->flags == 0U);
}

static bool route_is_recon_valid(scan_stat_t const * p_stat)
{
    uint32_t hard_flags = SCAN_FLAG_OVERRANGE | SCAN_FLAG_LOW_VALID;
    return ((p_stat->flags & hard_flags) == 0U) && (p_stat->overrange_count == 0U);
}

static float scan_stat_amp_v(scan_stat_t const * p_stat)
{
    return (float) p_stat->rms_milli * SCAN_RMS_MILLI_TO_AMP_V;
}

static int compare_u32(const void * p_a, const void * p_b)
{
    uint32_t a = *(uint32_t const *) p_a;
    uint32_t b = *(uint32_t const *) p_b;
    if (a < b)
    {
        return -1;
    }
    if (a > b)
    {
        return 1;
    }
    return 0;
}

static void skip_spaces(char ** pp_text)
{
    while ((**pp_text == ' ') || (**pp_text == '\t'))
    {
        (*pp_text)++;
    }
}

static bool parse_u32(char ** pp_text, uint32_t * p_value)
{
    skip_spaces(pp_text);
    if ('\0' == **pp_text)
    {
        return false;
    }

    char * p_end = NULL;
    unsigned long value = strtoul(*pp_text, &p_end, 0);
    if (p_end == *pp_text)
    {
        return false;
    }

    *pp_text = p_end;
    *p_value = (uint32_t) value;
    return true;
}

static bool parse_role(char ** pp_text, eit_mux_t * p_mux)
{
    skip_spaces(pp_text);

    char role[8] = {0};
    uint32_t len = 0U;
    while ((isalnum((unsigned char) **pp_text)) && (len < (sizeof(role) - 1U)))
    {
        role[len++] = (char) tolower((unsigned char) **pp_text);
        (*pp_text)++;
    }

    if (0 == strcmp(role, "src"))
    {
        *p_mux = EIT_MUX_SRC;
        return true;
    }
    if (0 == strcmp(role, "sink"))
    {
        *p_mux = EIT_MUX_SINK;
        return true;
    }
    if (0 == strcmp(role, "vp"))
    {
        *p_mux = EIT_MUX_VP;
        return true;
    }
    if (0 == strcmp(role, "vn"))
    {
        *p_mux = EIT_MUX_VN;
        return true;
    }
    return false;
}

static void led_heartbeat(void)
{
    static uint32_t elapsed_ms = 0U;
    static bool led_on = false;

    elapsed_ms++;
    if (elapsed_ms >= LED_BLINK_DELAY_MS)
    {
        elapsed_ms = 0U;
        led_on = !led_on;
        R_IOPORT_PinWrite(&g_ioport_ctrl, LED1_PIN, led_on ? BSP_IO_LEVEL_LOW : BSP_IO_LEVEL_HIGH);
    }
}

static void uart9_write_u32(uint32_t value)
{
    char reversed[10];
    char text[11];
    uint8_t pos = 0U;

    do
    {
        reversed[pos++] = (char) ('0' + (value % 10U));
        value /= 10U;
    } while ((value > 0U) && (pos < sizeof(reversed)));

    for (uint8_t i = 0U; i < pos; i++)
    {
        text[i] = reversed[pos - 1U - i];
    }
    text[pos] = '\0';
    uart9_write_string(text);
}

static void uart9_write_i32(int32_t value)
{
    if (value < 0)
    {
        uart9_write_string("-");
        uart9_write_u32((uint32_t) (-value));
    }
    else
    {
        uart9_write_u32((uint32_t) value);
    }
}

static void uart9_write_u32_padded(uint32_t value, uint32_t width)
{
    uint32_t divisor = 1U;
    for (uint32_t i = 1U; i < width; i++)
    {
        divisor *= 10U;
    }

    while ((divisor > 1U) && (value < divisor))
    {
        uart9_write_string("0");
        divisor /= 10U;
    }
    uart9_write_u32(value);
}

static void uart9_write_fixed3(uint32_t milli)
{
    uart9_write_u32(milli / 1000U);
    uart9_write_string(".");
    uint32_t frac = milli % 1000U;
    uart9_write_string((frac < 100U) ? "0" : "");
    uart9_write_string((frac < 10U) ? "0" : "");
    uart9_write_u32(frac);
}

static void uart9_write_float(float value)
{
    if (isnan(value))
    {
        uart9_write_string("nan");
        return;
    }
    if (isinf(value))
    {
        uart9_write_string((value < 0.0f) ? "-inf" : "inf");
        return;
    }

    if (value < 0.0f)
    {
        uart9_write_string("-");
        value = -value;
    }

    if (fabsf(value) < 1.0e-38f)
    {
        uart9_write_string("0.0000000e+00");
        return;
    }

    int32_t exponent = 0;
    while (value >= 10.0f)
    {
        value *= 0.1f;
        exponent++;
    }
    while (value < 1.0f)
    {
        value *= 10.0f;
        exponent--;
    }

    uint32_t mantissa = (uint32_t) ((value * 10000000.0f) + 0.5f);
    if (mantissa >= 100000000U)
    {
        mantissa /= 10U;
        exponent++;
    }

    uart9_write_u32(mantissa / 10000000U);
    uart9_write_string(".");
    uart9_write_u32_padded(mantissa % 10000000U, 7U);
    uart9_write_string((exponent < 0) ? "e-" : "e+");
    if (exponent < 0)
    {
        exponent = -exponent;
    }
    if (exponent < 100)
    {
        uart9_write_u32_padded((uint32_t) exponent, 2U);
    }
    else
    {
        uart9_write_i32(exponent);
    }
}

static void uart9_write_hex2(uint8_t value)
{
    static char const hex[] = "0123456789abcdef";
    char text[3] = {
        hex[(value >> 4) & 0x0FU],
        hex[value & 0x0FU],
        '\0',
    };
    uart9_write_string(text);
}

static void uart9_write_string(char const * p_text)
{
    uint32_t length = 0U;

    while ('\0' != p_text[length])
    {
        length++;
    }

    if (0U == length)
    {
        return;
    }

    g_uart9_tx_complete = false;
    g_uart9_error = false;

    fsp_err_t err = FSP_ERR_IN_USE;
    uint32_t wait_ms = UART9_TX_TIMEOUT_MS;

    while ((FSP_ERR_IN_USE == err) && (wait_ms > 0U))
    {
        err = g_uart9.p_api->write(g_uart9.p_ctrl, (uint8_t const *) p_text, length);
        if (FSP_ERR_IN_USE == err)
        {
            R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MILLISECONDS);
            wait_ms--;
        }
    }

    if (FSP_SUCCESS != err)
    {
        (void) g_uart9.p_api->communicationAbort(g_uart9.p_ctrl, UART_DIR_TX);
        return;
    }

    uint32_t tx_time_ms = (((length * 10U) * 1000U) + (UART9_BAUD - 1U)) / UART9_BAUD;
    R_BSP_SoftwareDelay(tx_time_ms + 2U, BSP_DELAY_UNITS_MILLISECONDS);
}

static void uart9_write_bytes(uint8_t const * p_data, uint32_t length)
{
    if (0U == length)
    {
        return;
    }

    g_uart9_tx_complete = false;
    g_uart9_error = false;

    fsp_err_t err = FSP_ERR_IN_USE;
    uint32_t wait_ms = UART9_TX_TIMEOUT_MS;

    while ((FSP_ERR_IN_USE == err) && (wait_ms > 0U))
    {
        err = g_uart9.p_api->write(g_uart9.p_ctrl, p_data, length);
        if (FSP_ERR_IN_USE == err)
        {
            R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MILLISECONDS);
            wait_ms--;
        }
    }

    if (FSP_SUCCESS != err)
    {
        (void) g_uart9.p_api->communicationAbort(g_uart9.p_ctrl, UART_DIR_TX);
        return;
    }

    uint32_t tx_time_ms = (((length * 10U) * 1000U) + (UART9_BAUD - 1U)) / UART9_BAUD;
    R_BSP_SoftwareDelay(tx_time_ms + 2U, BSP_DELAY_UNITS_MILLISECONDS);
}

static void uart9_configure_baud(void)
{
    sci_b_baud_setting_t baud_setting;
    fsp_err_t err = R_SCI_B_UART_BaudCalculate(UART9_BAUD,
                                               false,
                                               UART9_BAUD_MAX_ERROR_X1000,
                                               &baud_setting);
    if (FSP_SUCCESS != err)
    {
        error_blink();
    }

    err = g_uart9.p_api->baudSet(g_uart9.p_ctrl, &baud_setting);
    if (FSP_SUCCESS != err)
    {
        error_blink();
    }
}

static void write_scanstat_binary_frame(uint32_t frame_id,
                                        uint32_t electrodes,
                                        uint32_t samples,
                                        uint32_t rate,
                                        scan_stat_t const * p_stats,
                                        uint32_t route_count)
{
    uint32_t payload_len = route_count * EIT_BIN_SCANSTAT_ROW_SIZE;
    uint16_t crc = 0xFFFFU;
    uint8_t row[EIT_BIN_SCANSTAT_ROW_SIZE];

    for (uint32_t i = 0U; i < route_count; i++)
    {
        pack_scanstat_binary_row(&p_stats[i], row);
        for (uint32_t j = 0U; j < EIT_BIN_SCANSTAT_ROW_SIZE; j++)
        {
            crc = crc16_ccitt_update(crc, row[j]);
        }
    }

    uint8_t header[EIT_BIN_HEADER_SIZE] = {0};
    header[0] = (uint8_t) EIT_BIN_MAGIC_0;
    header[1] = (uint8_t) EIT_BIN_MAGIC_1;
    header[2] = (uint8_t) EIT_BIN_MAGIC_2;
    header[3] = (uint8_t) EIT_BIN_MAGIC_3;
    header[4] = (uint8_t) EIT_BIN_VERSION;
    header[5] = (uint8_t) EIT_BIN_TYPE_SCANSTAT;
    put_le16(&header[6], (uint16_t) EIT_BIN_HEADER_SIZE);
    put_le32(&header[8], payload_len);
    put_le32(&header[12], frame_id);
    put_le16(&header[16], (uint16_t) electrodes);
    put_le16(&header[18], (uint16_t) samples);
    put_le32(&header[20], rate);
    put_le16(&header[24], (uint16_t) route_count);
    put_le16(&header[26], (uint16_t) EIT_BIN_SCANSTAT_ROW_SIZE);
    put_le16(&header[28], crc);

    uart9_write_bytes(header, EIT_BIN_HEADER_SIZE);

    static uint8_t chunk[EIT_BIN_SCANSTAT_ROWS_PER_CHUNK * EIT_BIN_SCANSTAT_ROW_SIZE];
    uint32_t chunk_rows = 0U;
    for (uint32_t i = 0U; i < route_count; i++)
    {
        pack_scanstat_binary_row(&p_stats[i], &chunk[chunk_rows * EIT_BIN_SCANSTAT_ROW_SIZE]);
        chunk_rows++;
        if (chunk_rows >= EIT_BIN_SCANSTAT_ROWS_PER_CHUNK)
        {
            uart9_write_bytes(chunk, chunk_rows * EIT_BIN_SCANSTAT_ROW_SIZE);
            chunk_rows = 0U;
        }
    }
    if (chunk_rows > 0U)
    {
        uart9_write_bytes(chunk, chunk_rows * EIT_BIN_SCANSTAT_ROW_SIZE);
    }
}

static void write_reconfast_binary_frame(uint32_t frame_id,
                                         eit_recon_summary_t const * p_summary,
                                         float const ds_node[EIT_RECON_NODES])
{
    uint32_t payload_len = EIT_BIN_RECONFAST_SUMMARY_SIZE +
                           (EIT_RECON_NODES * EIT_BIN_RECONFAST_NODE_STRIDE);
    uint16_t crc = 0xFFFFU;
    uint8_t summary[EIT_BIN_RECONFAST_SUMMARY_SIZE];
    uint8_t value_bytes[EIT_BIN_RECONFAST_NODE_STRIDE];

    pack_reconfast_summary(p_summary, summary);
    for (uint32_t i = 0U; i < EIT_BIN_RECONFAST_SUMMARY_SIZE; i++)
    {
        crc = crc16_ccitt_update(crc, summary[i]);
    }
    for (uint32_t node = 0U; node < EIT_RECON_NODES; node++)
    {
        put_le_float32(value_bytes, ds_node[node]);
        for (uint32_t i = 0U; i < EIT_BIN_RECONFAST_NODE_STRIDE; i++)
        {
            crc = crc16_ccitt_update(crc, value_bytes[i]);
        }
    }

    uint8_t header[EIT_BIN_HEADER_SIZE] = {0};
    header[0] = (uint8_t) EIT_BIN_MAGIC_0;
    header[1] = (uint8_t) EIT_BIN_MAGIC_1;
    header[2] = (uint8_t) EIT_BIN_MAGIC_2;
    header[3] = (uint8_t) EIT_BIN_MAGIC_3;
    header[4] = (uint8_t) EIT_BIN_VERSION;
    header[5] = (uint8_t) EIT_BIN_TYPE_RECONFAST;
    put_le16(&header[6], (uint16_t) EIT_BIN_HEADER_SIZE);
    put_le32(&header[8], payload_len);
    put_le32(&header[12], frame_id);
    put_le16(&header[16], (uint16_t) EIT_RECON_ELECTRODES);
    put_le16(&header[18], (uint16_t) EIT_RECON_NODES);
    put_le32(&header[20], (uint32_t) EIT_RECON_ROUTES);
    put_le16(&header[24], (uint16_t) EIT_RECON_NODES);
    put_le16(&header[26], (uint16_t) EIT_BIN_RECONFAST_NODE_STRIDE);
    put_le16(&header[28], crc);

    uart9_write_bytes(header, EIT_BIN_HEADER_SIZE);
    uart9_write_bytes(summary, EIT_BIN_RECONFAST_SUMMARY_SIZE);

    static uint8_t chunk[32U * EIT_BIN_RECONFAST_NODE_STRIDE];
    uint32_t chunk_nodes = 0U;
    for (uint32_t node = 0U; node < EIT_RECON_NODES; node++)
    {
        put_le_float32(&chunk[chunk_nodes * EIT_BIN_RECONFAST_NODE_STRIDE], ds_node[node]);
        chunk_nodes++;
        if (chunk_nodes >= 32U)
        {
            uart9_write_bytes(chunk, chunk_nodes * EIT_BIN_RECONFAST_NODE_STRIDE);
            chunk_nodes = 0U;
        }
    }
    if (chunk_nodes > 0U)
    {
        uart9_write_bytes(chunk, chunk_nodes * EIT_BIN_RECONFAST_NODE_STRIDE);
    }
}

static void pack_scanstat_binary_row(scan_stat_t const * p_stat, uint8_t row[EIT_BIN_SCANSTAT_ROW_SIZE])
{
    memset(row, 0, EIT_BIN_SCANSTAT_ROW_SIZE);
    put_le16(&row[0], (uint16_t) p_stat->route_index);
    row[2] = (uint8_t) p_stat->src;
    row[3] = (uint8_t) p_stat->sink;
    row[4] = (uint8_t) p_stat->vp;
    row[5] = (uint8_t) p_stat->vn;
    put_le32(&row[6], p_stat->mean_milli);
    put_le32(&row[10], p_stat->rms_milli);
    put_le16(&row[14], p_stat->min_code);
    put_le16(&row[16], p_stat->max_code);
    put_le16(&row[18], (uint16_t) p_stat->pp_code);
    put_le16(&row[20], (uint16_t) p_stat->overrange_count);
    put_le16(&row[22], (uint16_t) p_stat->valid_count);
    put_le16(&row[24], (uint16_t) p_stat->flags);
    put_le16(&row[26], (uint16_t) p_stat->raw_flags);
    row[28] = (uint8_t) p_stat->retry_count;
}

static void pack_reconfast_summary(eit_recon_summary_t const * p_summary,
                                   uint8_t summary[EIT_BIN_RECONFAST_SUMMARY_SIZE])
{
    memset(summary, 0, EIT_BIN_RECONFAST_SUMMARY_SIZE);
    put_le16(&summary[0], (uint16_t) p_summary->valid_count);
    put_le16(&summary[2], (uint16_t) p_summary->invalid_count);
    put_le16(&summary[4], (uint16_t) p_summary->retry_count);
    put_le_float32(&summary[8], p_summary->ds_min);
    put_le_float32(&summary[12], p_summary->ds_max);
    put_le_float32(&summary[16], p_summary->ds_abs_p98);
    put_le_float32(&summary[20], p_summary->rel_l2);
}

static void put_le16(uint8_t * p_dst, uint16_t value)
{
    p_dst[0] = (uint8_t) (value & 0xFFU);
    p_dst[1] = (uint8_t) ((value >> 8) & 0xFFU);
}

static void put_le32(uint8_t * p_dst, uint32_t value)
{
    p_dst[0] = (uint8_t) (value & 0xFFU);
    p_dst[1] = (uint8_t) ((value >> 8) & 0xFFU);
    p_dst[2] = (uint8_t) ((value >> 16) & 0xFFU);
    p_dst[3] = (uint8_t) ((value >> 24) & 0xFFU);
}

static void put_le_float32(uint8_t * p_dst, float value)
{
    uint32_t bits;
    memcpy(&bits, &value, sizeof(bits));
    put_le32(p_dst, bits);
}

static uint16_t crc16_ccitt_update(uint16_t crc, uint8_t byte)
{
    crc ^= (uint16_t) byte << 8;
    for (uint32_t i = 0U; i < 8U; i++)
    {
        if (0U != (crc & 0x8000U))
        {
            crc = (uint16_t) ((crc << 1) ^ 0x1021U);
        }
        else
        {
            crc = (uint16_t) (crc << 1);
        }
    }
    return crc;
}

void uart9_callback(uart_callback_args_t * p_args)
{
    if ((UART_EVENT_TX_COMPLETE == p_args->event) || (UART_EVENT_TX_DATA_EMPTY == p_args->event))
    {
        g_uart9_tx_complete = true;
    }
    else if (UART_EVENT_RX_CHAR == p_args->event)
    {
        uint16_t next = (uint16_t) ((g_uart9_rx_head + 1U) % UART9_RX_BUF_SIZE);
        if (next != g_uart9_rx_tail)
        {
            g_uart9_rx_buf[g_uart9_rx_head] = (uint8_t) p_args->data;
            g_uart9_rx_head = next;
        }
        else
        {
            g_uart9_error = true;
        }
    }
    else if ((UART_EVENT_ERR_PARITY == p_args->event) ||
             (UART_EVENT_ERR_FRAMING == p_args->event) ||
             (UART_EVENT_ERR_OVERFLOW == p_args->event) ||
             (UART_EVENT_BREAK_DETECT == p_args->event))
    {
        g_uart9_error = true;
    }
}

static void error_blink(void)
{
    while (1)
    {
        R_IOPORT_PinWrite(&g_ioport_ctrl, LED1_PIN, BSP_IO_LEVEL_LOW);
        R_BSP_SoftwareDelay(100U, BSP_DELAY_UNITS_MILLISECONDS);
        R_IOPORT_PinWrite(&g_ioport_ctrl, LED1_PIN, BSP_IO_LEVEL_HIGH);
        R_BSP_SoftwareDelay(100U, BSP_DELAY_UNITS_MILLISECONDS);
    }
}

#if BSP_TZ_SECURE_BUILD

FSP_CPP_HEADER
BSP_CMSE_NONSECURE_ENTRY void template_nonsecure_callable ();

BSP_CMSE_NONSECURE_ENTRY void template_nonsecure_callable ()
{

}
FSP_CPP_FOOTER

#endif
