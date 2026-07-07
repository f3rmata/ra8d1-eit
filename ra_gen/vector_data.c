/* generated vector source file - do not edit */
#include "bsp_api.h"
/* Do not build these data structures if no interrupts are currently allocated because IAR will have build errors. */
#if VECTOR_DATA_IRQ_COUNT > 0
        BSP_DONT_REMOVE const fsp_vector_t g_vector_table[BSP_ICU_VECTOR_NUM_ENTRIES] BSP_PLACE_IN_SECTION(BSP_SECTION_APPLICATION_VECTORS) =
        {
                        [0] = sci_b_uart_rxi_isr, /* SCI9 RXI (Receive data full) */
            [1] = sci_b_uart_txi_isr, /* SCI9 TXI (Transmit data empty) */
            [2] = sci_b_uart_tei_isr, /* SCI9 TEI (Transmit end) */
            [3] = sci_b_uart_eri_isr, /* SCI9 ERI (Receive error) */
            [4] = sci_b_spi_rxi_isr, /* SCI2 RXI (Receive data full) */
            [5] = sci_b_spi_txi_isr, /* SCI2 TXI (Transmit data empty) */
            [6] = sci_b_spi_tei_isr, /* SCI2 TEI (Transmit end) */
            [7] = sci_b_spi_eri_isr, /* SCI2 ERI (Receive error) */
            [8] = dmac_int_isr, /* DMAC0 INT (DMAC0 transfer end) */
            [9] = glcdc_line_detect_isr, /* GLCDC LINE DETECT (Specified line) */
            [10] = mipi_dsi_seq0, /* MIPIDSI SEQ0 (Sequence operation channel 0 interrupt) */
            [11] = mipi_dsi_seq1, /* MIPIDSI SEQ1 (Sequence operation channel 1 interrupt) */
            [12] = mipi_dsi_vin1, /* MIPIDSI VIN1 (Video-Input operation channel1 interrupt) */
            [13] = mipi_dsi_rcv, /* MIPIDSI RCV (DSI packet receive interrupt) */
            [14] = mipi_dsi_ferr, /* MIPIDSI FERR (DSI fatal error interrupt) */
            [15] = mipi_dsi_ppi, /* MIPIDSI PPI (DSI D-PHY PPI interrupt) */
            [16] = drw_int_isr, /* DRW INT (DRW interrupt) */
            [17] = sci_b_i2c_txi_isr, /* SCI3 TXI (Transmit data empty) */
            [18] = sci_b_i2c_tei_isr, /* SCI3 TEI (Transmit end) */
        };
        #if BSP_FEATURE_ICU_HAS_IELSR
        const bsp_interrupt_event_t g_interrupt_event_link_select[BSP_ICU_VECTOR_NUM_ENTRIES] =
        {
            [0] = BSP_PRV_VECT_ENUM(EVENT_SCI9_RXI,GROUP0), /* SCI9 RXI (Receive data full) */
            [1] = BSP_PRV_VECT_ENUM(EVENT_SCI9_TXI,GROUP1), /* SCI9 TXI (Transmit data empty) */
            [2] = BSP_PRV_VECT_ENUM(EVENT_SCI9_TEI,GROUP2), /* SCI9 TEI (Transmit end) */
            [3] = BSP_PRV_VECT_ENUM(EVENT_SCI9_ERI,GROUP3), /* SCI9 ERI (Receive error) */
            [4] = BSP_PRV_VECT_ENUM(EVENT_SCI2_RXI,GROUP4), /* SCI2 RXI (Receive data full) */
            [5] = BSP_PRV_VECT_ENUM(EVENT_SCI2_TXI,GROUP5), /* SCI2 TXI (Transmit data empty) */
            [6] = BSP_PRV_VECT_ENUM(EVENT_SCI2_TEI,GROUP6), /* SCI2 TEI (Transmit end) */
            [7] = BSP_PRV_VECT_ENUM(EVENT_SCI2_ERI,GROUP7), /* SCI2 ERI (Receive error) */
            [8] = BSP_PRV_VECT_ENUM(EVENT_DMAC0_INT,GROUP0), /* DMAC0 INT (DMAC0 transfer end) */
            [9] = BSP_PRV_VECT_ENUM(EVENT_GLCDC_LINE_DETECT,GROUP1), /* GLCDC LINE DETECT (Specified line) */
            [10] = BSP_PRV_VECT_ENUM(EVENT_MIPIDSI_SEQ0,GROUP2), /* MIPIDSI SEQ0 (Sequence operation channel 0 interrupt) */
            [11] = BSP_PRV_VECT_ENUM(EVENT_MIPIDSI_SEQ1,GROUP3), /* MIPIDSI SEQ1 (Sequence operation channel 1 interrupt) */
            [12] = BSP_PRV_VECT_ENUM(EVENT_MIPIDSI_VIN1,GROUP4), /* MIPIDSI VIN1 (Video-Input operation channel1 interrupt) */
            [13] = BSP_PRV_VECT_ENUM(EVENT_MIPIDSI_RCV,GROUP5), /* MIPIDSI RCV (DSI packet receive interrupt) */
            [14] = BSP_PRV_VECT_ENUM(EVENT_MIPIDSI_FERR,GROUP6), /* MIPIDSI FERR (DSI fatal error interrupt) */
            [15] = BSP_PRV_VECT_ENUM(EVENT_MIPIDSI_PPI,GROUP7), /* MIPIDSI PPI (DSI D-PHY PPI interrupt) */
            [16] = BSP_PRV_VECT_ENUM(EVENT_DRW_INT,GROUP0), /* DRW INT (DRW interrupt) */
            [17] = BSP_PRV_VECT_ENUM(EVENT_SCI3_TXI,GROUP1), /* SCI3 TXI (Transmit data empty) */
            [18] = BSP_PRV_VECT_ENUM(EVENT_SCI3_TEI,GROUP2), /* SCI3 TEI (Transmit end) */
        };
        #endif
        #endif
