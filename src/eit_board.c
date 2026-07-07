#include "eit_board.h"

#include <stddef.h>

#define EIT_CMD_WR_RDAC  (0x01U)
#define EIT_CMD_WR_CTRL  (0x07U)
#define EIT_CMD_SHTDN    (0x09U)
#define EIT_MUX_GAP_US   (10U)
#define EIT_SPI_SETUP_US (5U)
#define EIT_SPI_HOLD_US  (5U)
#define EIT_SPI_GAP_US   (10U)
#define EIT_SPI_TIMEOUT_MS (20U)
#define EIT_ADC_CAPTURE_TIMEOUT_EXTRA_MS (1000U)
#define EIT_ADC_SPIKE_DELTA (128U)
#define EIT_ADC_SPIKE_NEIGHBOR_DELTA (64U)

typedef struct st_eit_pin_desc
{
    char const * p_signal;
    char const * p_rpi;
    char const * p_ra;
    bsp_io_port_pin_t pin;
} eit_pin_desc_t;

typedef struct st_eit_serial_bus
{
    bsp_io_port_pin_t data;
    bsp_io_port_pin_t sclk;
    bool use_hw_spi;
} eit_serial_bus_t;

typedef struct st_eit_mux_desc
{
    char const * p_name;
    bsp_io_port_pin_t cs;
} eit_mux_desc_t;

static void pin_write(bsp_io_port_pin_t pin, bsp_io_level_t level);
static bool spi_write(eit_serial_bus_t const * p_bus, bsp_io_port_pin_t cs, uint8_t bits, uint32_t value);
static void print_pin(eit_text_writer_t writer, eit_pin_desc_t const * p_pin);
static bool hw_spi_write(uint8_t const * p_data, uint32_t length);
static uint16_t adc_reverse10(uint16_t value);
static uint16_t adc_decode_pidr(uint16_t pidr);
static void adc_filter_single_sample_spikes(uint16_t * p_samples, uint32_t samples);
static uint16_t adc_abs_diff(uint16_t a, uint16_t b);
static bsp_io_port_pin_t mux_cs_pin(uint32_t mux);

/*
 * Initial wiring plan for the Vision Board RPI GPIO connector.
 * RPI-Pxxx labels are kept in the table because they are what should be used
 * at the adapter cable; the RA pins are what the firmware drives.
 */
static eit_pin_desc_t const g_control_pins[] =
{
    { "EN",       "RPI-P510",  "P510",  BSP_IO_PORT_05_PIN_10 },
    { "PWR",      "RPI-RXD2",  "P801",  BSP_IO_PORT_08_PIN_01 },
    { "OE_N",     "RPI-TXD2",  "P802",  BSP_IO_PORT_08_PIN_02 },
    { "CS_DRIVE", "RPI-P505",  "P505",  BSP_IO_PORT_05_PIN_05 },
    { "CS_MEAS",  "RPI-P804",  "P804",  BSP_IO_PORT_08_PIN_04 },
    { "V_DAT",    "RPI-SDA0",  "P409",  BSP_IO_PORT_04_PIN_09 },
    { "V_SCLK",   "RPI-SCL0",  "P408",  BSP_IO_PORT_04_PIN_08 },
};

static eit_pin_desc_t const g_mux_pins[] =
{
    { "CS1/SINK U7/D1",  "RPI-CS2",  "PA05", BSP_IO_PORT_10_PIN_05 },
    { "CS2/VP U5/D2",    "RPI-P507", "P507", BSP_IO_PORT_05_PIN_07 },
    { "CS3/SRC U8/D3",   "RPI-P508", "P508", BSP_IO_PORT_05_PIN_08 },
    { "CS4/VN U6/D4",    "RPI-P509", "P509", BSP_IO_PORT_05_PIN_09 },
};

static eit_pin_desc_t const g_adc_pins[] =
{
    { "ADC_0", "RPI-P001",  "P001", BSP_IO_PORT_00_PIN_01 },
    { "ADC_1", "RPI-AN102", "P002", BSP_IO_PORT_00_PIN_02 },
    { "ADC_2", "RPI-P003",  "P003", BSP_IO_PORT_00_PIN_03 },
    { "ADC_3", "RPI-AN000", "P004", BSP_IO_PORT_00_PIN_04 },
    { "ADC_4", "RPI-AN001", "P005", BSP_IO_PORT_00_PIN_05 },
    { "ADC_5", "RPI-P006",  "P006", BSP_IO_PORT_00_PIN_06 },
    { "ADC_6", "RPI-P007",  "P007", BSP_IO_PORT_00_PIN_07 },
    { "ADC_7", "RPI-P008",  "P008", BSP_IO_PORT_00_PIN_08 },
    { "ADC_8", "RPI-P009",  "P009", BSP_IO_PORT_00_PIN_09 },
    { "ADC_9", "RPI-P011",  "P011", BSP_IO_PORT_00_PIN_11 },
};

static eit_serial_bus_t const g_v_bus =
{
    .data = BSP_IO_PORT_04_PIN_09,
    .sclk = BSP_IO_PORT_04_PIN_08,
    .use_hw_spi = false,
};

static eit_serial_bus_t const g_h_bus =
{
    .data = BSP_IO_PORT_10_PIN_03,
    .sclk = BSP_IO_PORT_10_PIN_04,
    .use_hw_spi = true,
};

static eit_mux_desc_t const g_muxes[] =
{
    /*
     * Sub-board netlist:
     * CS1 -> U7/D1, CS2 -> U5/D2, CS3 -> U8/D3, CS4 -> U6/D4.
     * All four ADG731 parts share the same S1..S32 electrode nets, so only
     * the role CS changes here; the channel-to-electrode mapping stays direct.
     * Functional roles confirmed by signal amplitude:
     * D1=SINK, D2=VP, D3=SRC, D4=VN.
     */
    { "src",  BSP_IO_PORT_05_PIN_08 },
    { "sink", BSP_IO_PORT_10_PIN_05 },
    { "vp",   BSP_IO_PORT_05_PIN_07 },
    { "vn",   BSP_IO_PORT_05_PIN_09 },
};

static bool g_en;
static bool g_pwr;
static bool g_oe;
static volatile bool g_spi_done;
static volatile bool g_spi_error;
static volatile bool g_adc_dma_done;

fsp_err_t eit_board_init(void)
{
    fsp_err_t err;

    err = g_sci_spi_h.p_api->open(g_sci_spi_h.p_ctrl, g_sci_spi_h.p_cfg);
    if ((FSP_SUCCESS != err) && (FSP_ERR_ALREADY_OPEN != err))
    {
        return err;
    }

    err = g_adc_port_dma.p_api->open(g_adc_port_dma.p_ctrl, g_adc_port_dma.p_cfg);
    if ((FSP_SUCCESS != err) && (FSP_ERR_ALREADY_OPEN != err))
    {
        return err;
    }

    err = g_adc_sample_timer.p_api->open(g_adc_sample_timer.p_ctrl, g_adc_sample_timer.p_cfg);
    if ((FSP_SUCCESS != err) && (FSP_ERR_ALREADY_OPEN != err))
    {
        return err;
    }

    for (uint32_t i = 0; i < (sizeof(g_control_pins) / sizeof(g_control_pins[0])); i++)
    {
        uint32_t cfg = (uint32_t) IOPORT_CFG_PORT_DIRECTION_OUTPUT;
        if ((g_control_pins[i].pin == BSP_IO_PORT_05_PIN_05) ||
            (g_control_pins[i].pin == BSP_IO_PORT_08_PIN_04))
        {
            cfg |= (uint32_t) IOPORT_CFG_PORT_OUTPUT_HIGH;
        }
        else
        {
            cfg |= (uint32_t) IOPORT_CFG_PORT_OUTPUT_LOW;
        }

        err = R_IOPORT_PinCfg(&g_ioport_ctrl, g_control_pins[i].pin, cfg);
        if (FSP_SUCCESS != err)
        {
            return err;
        }
    }

    for (uint32_t i = 0; i < (sizeof(g_mux_pins) / sizeof(g_mux_pins[0])); i++)
    {
        err = R_IOPORT_PinCfg(&g_ioport_ctrl,
                              g_mux_pins[i].pin,
                              (uint32_t) IOPORT_CFG_PORT_DIRECTION_OUTPUT |
                              (uint32_t) IOPORT_CFG_PORT_OUTPUT_HIGH);
        if (FSP_SUCCESS != err)
        {
            return err;
        }
    }

    for (uint32_t i = 0; i < (sizeof(g_adc_pins) / sizeof(g_adc_pins[0])); i++)
    {
        err = R_IOPORT_PinCfg(&g_ioport_ctrl,
                              g_adc_pins[i].pin,
                              (uint32_t) IOPORT_CFG_PORT_DIRECTION_INPUT);
        if (FSP_SUCCESS != err)
        {
            return err;
        }
    }

    g_en = false;
    g_pwr = false;
    g_oe = false;

    eit_mux_write(EIT_MUX_SRC, 0U, false);
    eit_mux_write(EIT_MUX_SINK, 0U, false);
    eit_mux_write(EIT_MUX_VP, 0U, false);
    eit_mux_write(EIT_MUX_VN, 0U, false);
    eit_ad5270_unlock(EIT_RHEO_DRIVE);
    eit_ad5270_unlock(EIT_RHEO_MEAS);

    return FSP_SUCCESS;
}

void eit_board_print_signals(eit_text_writer_t writer)
{
    writer("Signals on Vision Board RPI GPIO:\r\n");
    writer("  Power: 3V3, GND; EIT analog +5V must come from the EIT board supply.\r\n");
    writer("  Main H12/H11 controls:\r\n");
    for (uint32_t i = 0; i < (sizeof(g_control_pins) / sizeof(g_control_pins[0])); i++)
    {
        print_pin(writer, &g_control_pins[i]);
    }

    writer("  ADG731 hardware SPI on SCI2:\r\n");
    writer("    H_DAT <- RPI-MOSI2 / PA03 / SCI2 TXD2\r\n");
    writer("    H_SCLK <- RPI-SCK2 / PA04 / SCI2 SCK2\r\n");

    writer("  Sub-board ADG731 chip selects:\r\n");
    for (uint32_t i = 0; i < (sizeof(g_mux_pins) / sizeof(g_mux_pins[0])); i++)
    {
        print_pin(writer, &g_mux_pins[i]);
    }

    writer("  ADS901E parallel bus:\r\n");
    for (uint32_t i = 0; i < (sizeof(g_adc_pins) / sizeof(g_adc_pins[0])); i++)
    {
        print_pin(writer, &g_adc_pins[i]);
    }
}

void eit_set_power_controls(bool en, bool pwr, bool oe)
{
    g_en = en;
    g_pwr = pwr;
    g_oe = oe;

    pin_write(BSP_IO_PORT_05_PIN_10, en ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW);
    pin_write(BSP_IO_PORT_08_PIN_01, pwr ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW);
    pin_write(BSP_IO_PORT_08_PIN_02, oe ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW);
}

void eit_get_power_controls(bool * p_en, bool * p_pwr, bool * p_oe)
{
    if (NULL != p_en)
    {
        *p_en = g_en;
    }
    if (NULL != p_pwr)
    {
        *p_pwr = g_pwr;
    }
    if (NULL != p_oe)
    {
        *p_oe = g_oe;
    }
}

void eit_ad5270_write(eit_rheo_t rheo, uint8_t command, uint16_t data)
{
    uint16_t word = (uint16_t) (((uint16_t) (command & 0x0FU) << 10) | (data & 0x03FFU));
    bsp_io_port_pin_t cs = (EIT_RHEO_DRIVE == rheo) ? BSP_IO_PORT_05_PIN_05 : BSP_IO_PORT_08_PIN_04;

    spi_write(&g_v_bus, cs, 16U, word);
}

void eit_ad5270_unlock(eit_rheo_t rheo)
{
    eit_ad5270_write(rheo, EIT_CMD_WR_CTRL, 0x002U);
}

void eit_ad5270_set(eit_rheo_t rheo, uint16_t value)
{
    eit_ad5270_write(rheo, EIT_CMD_WR_CTRL, 0x002U);
    eit_ad5270_write(rheo, EIT_CMD_SHTDN, 0x000U);
    eit_ad5270_write(rheo, EIT_CMD_WR_RDAC, value & 0x03FFU);
}

void eit_ad5270_shutdown(eit_rheo_t rheo, bool shutdown)
{
    eit_ad5270_write(rheo, EIT_CMD_SHTDN, shutdown ? 1U : 0U);
}

uint8_t eit_mux_command(uint8_t channel, bool enable)
{
    uint8_t mapped = eit_electrode_to_mux(channel);
    return enable ? (mapped & 0x1FU) : 0x80U;
}

uint8_t eit_electrode_to_mux(uint8_t electrode)
{
    /*
     * ADG731 Table II maps address 0..31 to switch S1..S32. The sub-board
     * netlist connects those switch pins directly to nets S1..S32:
     * channel 0 -> S1, channel 1 -> S2, ... channel 31 -> S32.
     */
    return electrode & 0x1FU;
}

bool eit_mux_write(eit_mux_t mux, uint8_t channel, bool enable)
{
    if ((uint32_t) mux >= (sizeof(g_muxes) / sizeof(g_muxes[0])))
    {
        return false;
    }

    for (uint32_t i = 0U; i < (sizeof(g_muxes) / sizeof(g_muxes[0])); i++)
    {
        pin_write(mux_cs_pin(i), BSP_IO_LEVEL_HIGH);
    }
    R_BSP_SoftwareDelay(EIT_MUX_GAP_US, BSP_DELAY_UNITS_MICROSECONDS);

    return spi_write(&g_h_bus, mux_cs_pin((uint32_t) mux), 8U, eit_mux_command(channel, enable));
}

bool eit_mux_all_off(void)
{
    bool ok = true;
    for (uint32_t i = 0U; i < (sizeof(g_muxes) / sizeof(g_muxes[0])); i++)
    {
        if (!eit_mux_write((eit_mux_t) i, 0U, false))
        {
            ok = false;
        }
    }
    return ok;
}

bool eit_route(uint8_t src, uint8_t sink, uint8_t vp, uint8_t vn)
{
    bool ok = eit_mux_all_off();
    R_BSP_SoftwareDelay(EIT_MUX_GAP_US, BSP_DELAY_UNITS_MICROSECONDS);
    if (!eit_mux_write(EIT_MUX_SRC, src, true))
    {
        ok = false;
    }
    if (!eit_mux_write(EIT_MUX_SINK, sink, true))
    {
        ok = false;
    }
    if (!eit_mux_write(EIT_MUX_VP, vp, true))
    {
        ok = false;
    }
    if (!eit_mux_write(EIT_MUX_VN, vn, true))
    {
        ok = false;
    }
    return ok;
}

uint16_t eit_adc_read(void)
{
    uint16_t pidr = R_PORT0->PIDR;
    return adc_decode_pidr(pidr);
}

bool eit_adc_capture(uint16_t * p_out, uint32_t samples, uint32_t rate_hz)
{
    if ((NULL == p_out) || (0U == samples))
    {
        return false;
    }
    if (rate_hz < 1000U)
    {
        rate_hz = 1000U;
    }

    uint32_t pclk_hz = R_FSP_SystemClockHzGet(FSP_PRIV_CLOCK_PCLKD);
    if (0U == pclk_hz)
    {
        return false;
    }

    uint32_t period_counts = (pclk_hz + (rate_hz / 2U)) / rate_hz;
    if (period_counts < 1U)
    {
        period_counts = 1U;
    }

    (void) g_adc_sample_timer.p_api->stop(g_adc_sample_timer.p_ctrl);
    (void) g_adc_sample_timer.p_api->reset(g_adc_sample_timer.p_ctrl);
    fsp_err_t err = g_adc_sample_timer.p_api->periodSet(g_adc_sample_timer.p_ctrl, period_counts);
    if (FSP_SUCCESS != err)
    {
        return false;
    }

    g_adc_dma_done = false;
    err = g_adc_port_dma.p_api->reset(g_adc_port_dma.p_ctrl,
                                      (void const *) &R_PORT0->PIDR,
                                      p_out,
                                      (uint16_t) samples);
    if (FSP_SUCCESS != err)
    {
        return false;
    }

    err = g_adc_sample_timer.p_api->start(g_adc_sample_timer.p_ctrl);
    if (FSP_SUCCESS != err)
    {
        (void) g_adc_port_dma.p_api->disable(g_adc_port_dma.p_ctrl);
        return false;
    }

    uint32_t wait_ms = EIT_ADC_CAPTURE_TIMEOUT_EXTRA_MS + ((samples * 3000U) / rate_hz);
    while (!g_adc_dma_done && (wait_ms > 0U))
    {
        R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MILLISECONDS);
        wait_ms--;
    }

    (void) g_adc_sample_timer.p_api->stop(g_adc_sample_timer.p_ctrl);
    (void) g_adc_port_dma.p_api->disable(g_adc_port_dma.p_ctrl);

    if (!g_adc_dma_done)
    {
        return false;
    }

    for (uint32_t i = 0U; i < samples; i++)
    {
        p_out[i] = adc_decode_pidr(p_out[i]);
    }
    adc_filter_single_sample_spikes(p_out, samples);
    return true;
}

static uint16_t adc_decode_pidr(uint16_t pidr)
{
    uint16_t raw = (uint16_t) (((pidr >> 1) & 0x01FFU) | ((pidr >> 2) & 0x0200U));
    return adc_reverse10(raw);
}

static void adc_filter_single_sample_spikes(uint16_t * p_samples, uint32_t samples)
{
    if ((NULL == p_samples) || (samples < 3U))
    {
        return;
    }

    for (uint32_t i = 1U; i < (samples - 1U); i++)
    {
        uint16_t prev = p_samples[i - 1U];
        uint16_t cur = p_samples[i];
        uint16_t next = p_samples[i + 1U];
        bool neighbors_valid = (prev > 2U) && (prev < 1021U) && (next > 2U) && (next < 1021U);
        if (!neighbors_valid)
        {
            continue;
        }
        if (adc_abs_diff(prev, next) > EIT_ADC_SPIKE_NEIGHBOR_DELTA)
        {
            continue;
        }

        bool rail_spike = (cur <= 2U) || (cur >= 1021U);
        bool jump_spike = (adc_abs_diff(cur, prev) > EIT_ADC_SPIKE_DELTA) &&
                          (adc_abs_diff(cur, next) > EIT_ADC_SPIKE_DELTA);
        if (rail_spike || jump_spike)
        {
            p_samples[i] = (uint16_t) (((uint32_t) prev + (uint32_t) next + 1U) / 2U);
        }
    }
}

static uint16_t adc_abs_diff(uint16_t a, uint16_t b)
{
    return (a >= b) ? (uint16_t) (a - b) : (uint16_t) (b - a);
}

static bsp_io_port_pin_t mux_cs_pin(uint32_t mux)
{
    return g_muxes[mux].cs;
}

static uint16_t adc_reverse10(uint16_t value)
{
    uint16_t result = 0U;
    for (uint32_t bit = 0U; bit < 10U; bit++)
    {
        result = (uint16_t) ((result << 1U) | ((value >> bit) & 1U));
    }
    return result;
}

void eit_adc_sample(eit_adc_stats_t * p_stats, uint16_t samples, uint32_t delay_us)
{
    if (NULL == p_stats)
    {
        return;
    }

    p_stats->min = 0x03FFU;
    p_stats->max = 0U;
    p_stats->sum = 0U;
    p_stats->last = 0U;
    p_stats->samples = samples;

    if (0U == samples)
    {
        return;
    }

    for (uint16_t i = 0; i < samples; i++)
    {
        uint16_t value = eit_adc_read();
        p_stats->last = value;
        p_stats->sum += value;
        if (value < p_stats->min)
        {
            p_stats->min = value;
        }
        if (value > p_stats->max)
        {
            p_stats->max = value;
        }
        if (delay_us > 0U)
        {
            R_BSP_SoftwareDelay(delay_us, BSP_DELAY_UNITS_MICROSECONDS);
        }
    }
}

static void pin_write(bsp_io_port_pin_t pin, bsp_io_level_t level)
{
    (void) R_IOPORT_PinWrite(&g_ioport_ctrl, pin, level);
}

static bool spi_write(eit_serial_bus_t const * p_bus, bsp_io_port_pin_t cs, uint8_t bits, uint32_t value)
{
    uint8_t buf[2];
    uint32_t length;
    bool ok = true;
    if (bits <= 8U)
    {
        buf[0] = (uint8_t) value;
        length = 1U;
    }
    else
    {
        buf[0] = (uint8_t) (value >> 8);
        buf[1] = (uint8_t) value;
        length = 2U;
    }

    R_BSP_SoftwareDelay(EIT_SPI_GAP_US, BSP_DELAY_UNITS_MICROSECONDS);
    pin_write(cs, BSP_IO_LEVEL_LOW);
    R_BSP_SoftwareDelay(EIT_SPI_SETUP_US, BSP_DELAY_UNITS_MICROSECONDS);
    if (p_bus->use_hw_spi)
    {
        ok = hw_spi_write(buf, length);
    }
    else
    {
        pin_write(p_bus->sclk, BSP_IO_LEVEL_LOW);
        for (uint8_t i = 0U; i < bits; i++)
        {
            uint8_t shift = (uint8_t) (bits - 1U - i);
            bool bit_high = 0U != ((value >> shift) & 1U);
            pin_write(p_bus->data, bit_high ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW);
            R_BSP_SoftwareDelay(2U, BSP_DELAY_UNITS_MICROSECONDS);
            pin_write(p_bus->sclk, BSP_IO_LEVEL_HIGH);
            R_BSP_SoftwareDelay(2U, BSP_DELAY_UNITS_MICROSECONDS);
            pin_write(p_bus->sclk, BSP_IO_LEVEL_LOW);
        }
    }
    R_BSP_SoftwareDelay(EIT_SPI_HOLD_US, BSP_DELAY_UNITS_MICROSECONDS);
    pin_write(cs, BSP_IO_LEVEL_HIGH);
    R_BSP_SoftwareDelay(EIT_SPI_GAP_US, BSP_DELAY_UNITS_MICROSECONDS);
    return ok;
}

static bool hw_spi_write(uint8_t const * p_data, uint32_t length)
{
    if ((NULL == p_data) || (0U == length))
    {
        return false;
    }

    g_spi_done = false;
    g_spi_error = false;
    fsp_err_t err = g_sci_spi_h.p_api->write(g_sci_spi_h.p_ctrl, p_data, length, SPI_BIT_WIDTH_8_BITS);
    if (FSP_SUCCESS != err)
    {
        return false;
    }

    uint32_t wait_ms = EIT_SPI_TIMEOUT_MS;
    while (!g_spi_done && !g_spi_error && (wait_ms > 0U))
    {
        R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MILLISECONDS);
        wait_ms--;
    }

    return g_spi_done && !g_spi_error;
}

void sci_b_spi_h_callback(spi_callback_args_t * p_args)
{
    if (SPI_EVENT_TRANSFER_COMPLETE == p_args->event)
    {
        g_spi_done = true;
    }
    else
    {
        g_spi_error = true;
    }
}

void adc_dma_callback(transfer_callback_args_t * p_args)
{
    (void) p_args;
    g_adc_dma_done = true;
}

static void print_pin(eit_text_writer_t writer, eit_pin_desc_t const * p_pin)
{
    writer("    ");
    writer(p_pin->p_signal);
    writer(" <- ");
    writer(p_pin->p_rpi);
    writer(" / ");
    writer(p_pin->p_ra);
    writer("\r\n");
}
