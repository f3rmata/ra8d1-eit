#ifndef EIT_LCD_PANEL_H_
#define EIT_LCD_PANEL_H_

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Initialize the MIPI DSI LCD panel by sending the DCS command sequence.
 * Must be called after GLCDC is opened but before GLCDC is started.
 */
void eit_lcd_panel_init(void);

#ifdef __cplusplus
}
#endif

#endif /* EIT_LCD_PANEL_H_ */
