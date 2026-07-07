#ifndef EIT_UI_H_
#define EIT_UI_H_

#include "eit_recon.h"
#include "eit_recon_model.h"

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Initialize the EIT display UI.
 * Draws the initial screen layout (background, labels).
 */
void eit_ui_init(void);

/*
 * Render one reconstruction frame on the LCD.
 * Shows: frame ID, route stats, ds heatmap, summary statistics.
 *
 * ds_node: EIT_RECON_NODES float array of conductivity changes
 * summary:  reconstruction summary (valid/invalid, ds_min/max, etc.)
 * frame_id: monotonically increasing frame counter
 */
void eit_ui_show_recon_frame(const float ds_node[EIT_RECON_NODES],
                             const eit_recon_summary_t *summary,
                             uint32_t frame_id);

/*
 * Show a simple status message (e.g. "EIT Ready", "Baseline captured").
 */
void eit_ui_show_status(const char *message);

/*
 * Fill screen with a solid color test pattern.
 * Used for hardware verification (red/green/blue screen).
 */
void eit_ui_test_color(uint16_t color);

#ifdef __cplusplus
}
#endif

#endif /* EIT_UI_H_ */
