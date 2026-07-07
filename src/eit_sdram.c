/*
 * SDRAM initialization for RA8D1 Vision Board.
 * Ported from RT-Thread SDK (drv_sdram.c) to bare-metal FSP.
 *
 * Configures the external 32MB SDRAM (16-bit bus, 120MHz SDCLK)
 * mapped at 0x68000000 for use as GLCDC framebuffer storage.
 *
 * Uses only R_BUS register writes — no RT-Thread or FSP stack dependency.
 */

#include "hal_data.h"
#include "eit_sdram.h"

/*
 * Set ACTIVE-to-PRECHARGE command (tRAS) timing
 * tRAS = 42ns -> 6 cycles at SDCLK 120MHz
 */
#define BSP_PRV_SDRAM_TRAS                     (6U)

/*
 * Set ACTIVE-to-READ or WRITE delay tRCD
 * tRCD = 18ns -> 3 cycles at SDCLK 120MHz
 */
#define BSP_PRV_SDRAM_TRCD                     (3U)

/*
 * Set PRECHARGE command period (tRP) timing
 * tRP = 18ns -> 3 cycles at SDCLK 120MHz
 */
#define BSP_PRV_SDRAM_TRP                      (3U)

/*
 * Set WRITE recovery time (tWR) timing
 * tWR = 1CLK + 6ns -> 2 cycles at SDCLK 120MHz
 */
#define BSP_PRV_SDRAM_TWR                      (2U)

/*
 * Set CAS (READ) latency (CL) timing
 * CL = 18ns -> 3 cycles at SDCLK 120MHz
 */
#define BSP_PRV_SDRAM_CL                       (3U)

/*
 * Set AUTO REFRESH period (tRFC) timing
 * tRFC = 60ns -> 8 cycles at SDCLK 120MHz
 */
#define BSP_PRV_SDRAM_TRFC                     (8U)

/*
 * Set Average Refresh period
 * tREF = 64ms/8192rows -> 7.8125us each row
 * 937 cycles at SDCLK 120MHz
 */
#define BSP_PRV_SDRAM_REF_CMD_INTERVAL         (937U)

/*
 * Set Auto-Refresh issue times in initialization sequence
 * Typical SDR SDRAM needs twice
 */
#define BSP_PRV_SDRAM_SDIR_REF_TIMES           (2U)

/*
 * Set RAW address offset: 9-bit
 */
#define BSP_PRV_SDRAM_SDADR_ROW_ADDR_OFFSET    (9U)

/*
 * Endian mode: same as operating mode
 */
#define BSP_PRV_SDRAM_ENDIAN_MODE              (0U)

/*
 * Continuous access: enabled for better bandwidth
 */
#define BSP_PRV_SDRAM_CONTINUOUS_ACCESSMODE    (1U)

/*
 * Bus width: 16-bit
 */
#define BSP_PRV_SDRAM_BUS_WIDTH                (0U)

/* Mode Register bits */
#define BSP_PRV_SDRAM_MR_WB_SINGLE_LOC_ACC    (1U) /* MR.M9: Single Location Access */
#define BSP_PRV_SDRAM_MR_OP_MODE              (0U) /* MR.M8:M7: Standard Operation */
#define BSP_PRV_SDRAM_MR_BT_SEQUENCTIAL       (0U) /* MR.M3 Burst Type: Sequential */
#define BSP_PRV_SDRAM_MR_BURST_LENGTH         (0U) /* MR.M2:M0 Burst Length: 1 burst */

static void drv_sdram_init(void);

void eit_sdram_init(void)
{
    /*
     * According to the RA8D1 hardware manual, the SDRAM clock (SDCLK)
     * must be enabled before accessing SDRAM registers. On this BSP
     * SDCLK is driven by PCLKB which is already running after reset.
     *
     * The BUS peripheral registers are always accessible.
     */
    drv_sdram_init();
}

static void drv_sdram_init(void)
{
    /* Setting for SDRAM initialization sequence */
#if (BSP_PRV_SDRAM_TRP < 3)
    R_BUS->SDRAM.SDIR_b.PRC = 3U;
#else
    R_BUS->SDRAM.SDIR_b.PRC = BSP_PRV_SDRAM_TRP - 3U;
#endif

    while (R_BUS->SDRAM.SDSR)
    {
        /*
         * According to h/w manual, need to confirm that all the status
         * bits in SDSR are 0 before SDIR modification.
         */
    }

    R_BUS->SDRAM.SDIR_b.ARFC = (uint8_t) BSP_PRV_SDRAM_SDIR_REF_TIMES;

    while (R_BUS->SDRAM.SDSR)
    {
        /*
         * According to h/w manual, need to confirm that all the status
         * bits in SDSR are 0 before SDIR modification.
         */
    }

#if (BSP_PRV_SDRAM_TRFC < 3)
    R_BUS->SDRAM.SDIR_b.ARFI = 0U;
#else
    R_BUS->SDRAM.SDIR_b.ARFI = (uint8_t) (BSP_PRV_SDRAM_TRFC - 3U);
#endif

    while (R_BUS->SDRAM.SDSR)
    {
        /*
         * According to h/w manual, need to confirm that all the status
         * bits in SDSR are 0 before SDICR modification.
         */
    }

    /*
     * Start SDRAM initialization sequence.
     * Following operation is automatically done when set SDICR.INIRQ bit:
     * - Perform a PRECHARGE ALL command and wait at least tRP time
     * - Issue an AUTO REFRESH command and wait at least tRFC time
     * - Issue an AUTO REFRESH command and wait at least tRFC time
     */
    R_BUS->SDRAM.SDICR_b.INIRQ = 1U;
    while (R_BUS->SDRAM.SDSR_b.INIST)
    {
        /* Wait the end of initialization sequence. */
    }

    /* Setting for SDRAM controller */
    R_BUS->SDRAM.SDCCR_b.BSIZE  = BSP_PRV_SDRAM_BUS_WIDTH;             /* set SDRAM bus width */
    R_BUS->SDRAM.SDAMOD_b.BE    = BSP_PRV_SDRAM_CONTINUOUS_ACCESSMODE; /* enable continuous access */
    R_BUS->SDRAM.SDCMOD_b.EMODE = BSP_PRV_SDRAM_ENDIAN_MODE;           /* set endian mode for SDRAM */

    while (R_BUS->SDRAM.SDSR)
    {
        /*
         * According to h/w manual, need to confirm that all the status
         * bits in SDSR are 0 before SDMOD modification.
         */
    }

    /*
     * Using LMR command, program the mode register
     */
    R_BUS->SDRAM.SDMOD = ((((uint16_t) (BSP_PRV_SDRAM_MR_WB_SINGLE_LOC_ACC << 9) |
                            (uint16_t) (BSP_PRV_SDRAM_MR_OP_MODE << 7)) |
                           (uint16_t) (BSP_PRV_SDRAM_CL << 4)) |
                          (uint16_t) (BSP_PRV_SDRAM_MR_BT_SEQUENCTIAL << 3)) |
                         (uint16_t) (BSP_PRV_SDRAM_MR_BURST_LENGTH << 0);

    /* wait at least tMRD time */
    while (R_BUS->SDRAM.SDSR_b.MRSST)
    {
        /* Wait until Mode Register setting done. */
    }

    /* Set timing parameters for SDRAM */
    R_BUS->SDRAM.SDTR_b.RAS = (uint8_t) (BSP_PRV_SDRAM_TRAS - 1U); /* set ACTIVE-to-PRECHARGE cycles */
    R_BUS->SDRAM.SDTR_b.RCD = (uint8_t) (BSP_PRV_SDRAM_TRCD - 1U); /* set ACTIVE to READ/WRITE delay cycles */
    R_BUS->SDRAM.SDTR_b.RP  = (uint8_t) (BSP_PRV_SDRAM_TRP - 1U);  /* set PRECHARGE command period cycles */
    R_BUS->SDRAM.SDTR_b.WR  = (uint8_t) (BSP_PRV_SDRAM_TWR - 1U);  /* set write recovery cycles */
    R_BUS->SDRAM.SDTR_b.CL  = (uint8_t) BSP_PRV_SDRAM_CL;          /* set SDRAM column latency cycles */

    /* Set row address offset for target SDRAM */
    R_BUS->SDRAM.SDADR_b.MXC = (uint8_t) (BSP_PRV_SDRAM_SDADR_ROW_ADDR_OFFSET - 8U);

    R_BUS->SDRAM.SDRFCR_b.REFW = (uint16_t) (BSP_PRV_SDRAM_TRFC - 1U); /* set Auto-Refresh issuing cycle */
    R_BUS->SDRAM.SDRFCR_b.RFC  = (uint16_t) (BSP_PRV_SDRAM_REF_CMD_INTERVAL - 1U); /* set Auto-Refresh period */

    /* Start Auto-refresh */
    R_BUS->SDRAM.SDRFEN_b.RFEN = 1U;

    /* Enable SDRAM access */
    R_BUS->SDRAM.SDCCR_b.EXENB = 1U;
}
