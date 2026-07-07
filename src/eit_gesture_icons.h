#ifndef EIT_GESTURE_ICONS_H_
#define EIT_GESTURE_ICONS_H_

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Draw rock/fist SVG-derived icon on the LCD. */
void eit_gesture_draw_rock(void);

/* Draw scissors SVG-derived icon on the LCD. */
void eit_gesture_draw_scissors(void);

/* Draw paper/open-hand SVG-derived icon on the LCD. */
void eit_gesture_draw_paper(void);

#ifdef __cplusplus
}
#endif

#endif /* EIT_GESTURE_ICONS_H_ */
