/* generated vector header file - do not edit */
#ifndef VECTOR_DATA_H
#define VECTOR_DATA_H
#ifdef __cplusplus
        extern "C" {
        #endif
/* Number of interrupts allocated */
#ifndef VECTOR_DATA_IRQ_COUNT
#define VECTOR_DATA_IRQ_COUNT    (19)
#endif
/* ISR prototypes */
void sci_b_uart_rxi_isr(void);
void sci_b_uart_txi_isr(void);
void sci_b_uart_tei_isr(void);
void sci_b_uart_eri_isr(void);
void sci_b_spi_rxi_isr(void);
void sci_b_spi_txi_isr(void);
void sci_b_spi_tei_isr(void);
void sci_b_spi_eri_isr(void);
void dmac_int_isr(void);
void glcdc_line_detect_isr(void);
void mipi_dsi_seq0(void);
void mipi_dsi_seq1(void);
void mipi_dsi_vin1(void);
void mipi_dsi_rcv(void);
void mipi_dsi_ferr(void);
void mipi_dsi_ppi(void);
void drw_int_isr(void);
void sci_b_i2c_txi_isr(void);
void sci_b_i2c_tei_isr(void);

/* Vector table allocations */
#define VECTOR_NUMBER_SCI9_RXI ((IRQn_Type) 0) /* SCI9 RXI (Receive data full) */
#define SCI9_RXI_IRQn          ((IRQn_Type) 0) /* SCI9 RXI (Receive data full) */
#define VECTOR_NUMBER_SCI9_TXI ((IRQn_Type) 1) /* SCI9 TXI (Transmit data empty) */
#define SCI9_TXI_IRQn          ((IRQn_Type) 1) /* SCI9 TXI (Transmit data empty) */
#define VECTOR_NUMBER_SCI9_TEI ((IRQn_Type) 2) /* SCI9 TEI (Transmit end) */
#define SCI9_TEI_IRQn          ((IRQn_Type) 2) /* SCI9 TEI (Transmit end) */
#define VECTOR_NUMBER_SCI9_ERI ((IRQn_Type) 3) /* SCI9 ERI (Receive error) */
#define SCI9_ERI_IRQn          ((IRQn_Type) 3) /* SCI9 ERI (Receive error) */
#define VECTOR_NUMBER_SCI2_RXI ((IRQn_Type) 4) /* SCI2 RXI (Receive data full) */
#define SCI2_RXI_IRQn          ((IRQn_Type) 4) /* SCI2 RXI (Receive data full) */
#define VECTOR_NUMBER_SCI2_TXI ((IRQn_Type) 5) /* SCI2 TXI (Transmit data empty) */
#define SCI2_TXI_IRQn          ((IRQn_Type) 5) /* SCI2 TXI (Transmit data empty) */
#define VECTOR_NUMBER_SCI2_TEI ((IRQn_Type) 6) /* SCI2 TEI (Transmit end) */
#define SCI2_TEI_IRQn          ((IRQn_Type) 6) /* SCI2 TEI (Transmit end) */
#define VECTOR_NUMBER_SCI2_ERI ((IRQn_Type) 7) /* SCI2 ERI (Receive error) */
#define SCI2_ERI_IRQn          ((IRQn_Type) 7) /* SCI2 ERI (Receive error) */
#define VECTOR_NUMBER_DMAC0_INT ((IRQn_Type) 8) /* DMAC0 INT (DMAC0 transfer end) */
#define DMAC0_INT_IRQn          ((IRQn_Type) 8) /* DMAC0 INT (DMAC0 transfer end) */
#define VECTOR_NUMBER_GLCDC_LINE_DETECT ((IRQn_Type) 9) /* GLCDC LINE DETECT (Specified line) */
#define GLCDC_LINE_DETECT_IRQn          ((IRQn_Type) 9) /* GLCDC LINE DETECT (Specified line) */
#define VECTOR_NUMBER_MIPIDSI_SEQ0 ((IRQn_Type) 10) /* MIPIDSI SEQ0 (Sequence operation channel 0 interrupt) */
#define MIPIDSI_SEQ0_IRQn          ((IRQn_Type) 10) /* MIPIDSI SEQ0 (Sequence operation channel 0 interrupt) */
#define VECTOR_NUMBER_MIPIDSI_SEQ1 ((IRQn_Type) 11) /* MIPIDSI SEQ1 (Sequence operation channel 1 interrupt) */
#define MIPIDSI_SEQ1_IRQn          ((IRQn_Type) 11) /* MIPIDSI SEQ1 (Sequence operation channel 1 interrupt) */
#define VECTOR_NUMBER_MIPIDSI_VIN1 ((IRQn_Type) 12) /* MIPIDSI VIN1 (Video-Input operation channel1 interrupt) */
#define MIPIDSI_VIN1_IRQn          ((IRQn_Type) 12) /* MIPIDSI VIN1 (Video-Input operation channel1 interrupt) */
#define VECTOR_NUMBER_MIPIDSI_RCV ((IRQn_Type) 13) /* MIPIDSI RCV (DSI packet receive interrupt) */
#define MIPIDSI_RCV_IRQn          ((IRQn_Type) 13) /* MIPIDSI RCV (DSI packet receive interrupt) */
#define VECTOR_NUMBER_MIPIDSI_FERR ((IRQn_Type) 14) /* MIPIDSI FERR (DSI fatal error interrupt) */
#define MIPIDSI_FERR_IRQn          ((IRQn_Type) 14) /* MIPIDSI FERR (DSI fatal error interrupt) */
#define VECTOR_NUMBER_MIPIDSI_PPI ((IRQn_Type) 15) /* MIPIDSI PPI (DSI D-PHY PPI interrupt) */
#define MIPIDSI_PPI_IRQn          ((IRQn_Type) 15) /* MIPIDSI PPI (DSI D-PHY PPI interrupt) */
#define VECTOR_NUMBER_DRW_INT ((IRQn_Type) 16) /* DRW INT (DRW interrupt) */
#define DRW_INT_IRQn          ((IRQn_Type) 16) /* DRW INT (DRW interrupt) */
#define VECTOR_NUMBER_SCI3_TXI ((IRQn_Type) 17) /* SCI3 TXI (Transmit data empty) */
#define SCI3_TXI_IRQn          ((IRQn_Type) 17) /* SCI3 TXI (Transmit data empty) */
#define VECTOR_NUMBER_SCI3_TEI ((IRQn_Type) 18) /* SCI3 TEI (Transmit end) */
#define SCI3_TEI_IRQn          ((IRQn_Type) 18) /* SCI3 TEI (Transmit end) */
/* The number of entries required for the ICU vector table. */
#define BSP_ICU_VECTOR_NUM_ENTRIES (19)

#ifdef __cplusplus
        }
        #endif
#endif /* VECTOR_DATA_H */
