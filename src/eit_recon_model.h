#ifndef EIT_RECON_MODEL_H
#define EIT_RECON_MODEL_H

#include <stdint.h>

#define EIT_RECON_MODEL_VERSION "jac8-h0.12-kotre-p0.5-lambda0.01-v1"
#define EIT_RECON_ELECTRODES (8U)
#define EIT_RECON_ROUTES (40U)
#define EIT_RECON_NODES (261U)
#define EIT_RECON_ELEMENTS (467U)

extern const uint8_t g_eit_recon_routes[EIT_RECON_ROUTES][4];
extern const float g_eit_recon_baseline_amp_v[EIT_RECON_ROUTES];
extern const float g_eit_recon_node_xy[EIT_RECON_NODES][2];
extern const uint16_t g_eit_recon_elements[EIT_RECON_ELEMENTS][3];
extern const float g_eit_recon_matrix[EIT_RECON_NODES][EIT_RECON_ROUTES];
extern const char g_eit_recon_baseline_source[];

#endif
