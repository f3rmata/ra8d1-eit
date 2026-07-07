#ifndef EIT_RECON_H
#define EIT_RECON_H

#include "eit_recon_model.h"

#include <stdbool.h>
#include <stdint.h>

typedef struct st_eit_recon_summary
{
    uint32_t valid_count;
    uint32_t invalid_count;
    uint32_t retry_count;
    float ds_min;
    float ds_max;
    float ds_abs_p98;
    float rel_l2;
} eit_recon_summary_t;

void eit_recon_init(void);
void eit_recon_reset_baseline(void);
bool eit_recon_baseline_is_ram(void);
char const * eit_recon_active_baseline_source(void);
bool eit_recon_route_matches(uint32_t route_index, uint32_t src, uint32_t sink, uint32_t vp, uint32_t vn);
void eit_recon_baseline_accum_clear(void);
void eit_recon_baseline_accum_add(float const amp_v[EIT_RECON_ROUTES],
                                  bool const valid[EIT_RECON_ROUTES]);
bool eit_recon_baseline_accum_commit(void);
void eit_recon_solve(float const amp_v[EIT_RECON_ROUTES],
                     bool const valid[EIT_RECON_ROUTES],
                     uint32_t retry_count,
                     float ds_node[EIT_RECON_NODES],
                     eit_recon_summary_t * p_summary);

#endif
