#include "eit_recon.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define EIT_RECON_BASELINE_EPS (1.0e-12f)

static float g_active_baseline[EIT_RECON_ROUTES];
static float g_accum_sum[EIT_RECON_ROUTES];
static uint16_t g_accum_count[EIT_RECON_ROUTES];
static float g_abs_scratch[EIT_RECON_NODES];
static bool g_initialized = false;
static bool g_baseline_is_ram = false;

static int compare_float_ascending(const void * p_a, const void * p_b);
static float percentile_sorted(float const * p_sorted, uint32_t count, float percentile);

void eit_recon_init(void)
{
    if (!g_initialized)
    {
        eit_recon_reset_baseline();
        g_initialized = true;
    }
}

void eit_recon_reset_baseline(void)
{
    memcpy(g_active_baseline, g_eit_recon_baseline_amp_v, sizeof(g_active_baseline));
    memset(g_accum_sum, 0, sizeof(g_accum_sum));
    memset(g_accum_count, 0, sizeof(g_accum_count));
    g_baseline_is_ram = false;
}

bool eit_recon_baseline_is_ram(void)
{
    eit_recon_init();
    return g_baseline_is_ram;
}

char const * eit_recon_active_baseline_source(void)
{
    eit_recon_init();
    return g_baseline_is_ram ? "ram" : g_eit_recon_baseline_source;
}

bool eit_recon_route_matches(uint32_t route_index, uint32_t src, uint32_t sink, uint32_t vp, uint32_t vn)
{
    if (route_index >= EIT_RECON_ROUTES)
    {
        return false;
    }

    return (g_eit_recon_routes[route_index][0] == src) &&
           (g_eit_recon_routes[route_index][1] == sink) &&
           (g_eit_recon_routes[route_index][2] == vp) &&
           (g_eit_recon_routes[route_index][3] == vn);
}

void eit_recon_baseline_accum_clear(void)
{
    memset(g_accum_sum, 0, sizeof(g_accum_sum));
    memset(g_accum_count, 0, sizeof(g_accum_count));
}

void eit_recon_baseline_accum_add(float const amp_v[EIT_RECON_ROUTES],
                                  bool const valid[EIT_RECON_ROUTES])
{
    for (uint32_t route = 0U; route < EIT_RECON_ROUTES; route++)
    {
        if (valid[route] && isfinite(amp_v[route]) && (fabsf(amp_v[route]) > EIT_RECON_BASELINE_EPS))
        {
            g_accum_sum[route] += amp_v[route];
            if (g_accum_count[route] < UINT16_MAX)
            {
                g_accum_count[route]++;
            }
        }
    }
}

bool eit_recon_baseline_accum_commit(void)
{
    for (uint32_t route = 0U; route < EIT_RECON_ROUTES; route++)
    {
        if (0U == g_accum_count[route])
        {
            return false;
        }
    }

    for (uint32_t route = 0U; route < EIT_RECON_ROUTES; route++)
    {
        g_active_baseline[route] = g_accum_sum[route] / (float) g_accum_count[route];
    }
    g_baseline_is_ram = true;
    return true;
}

void eit_recon_solve(float const amp_v[EIT_RECON_ROUTES],
                     bool const valid[EIT_RECON_ROUTES],
                     uint32_t retry_count,
                     float ds_node[EIT_RECON_NODES],
                     eit_recon_summary_t * p_summary)
{
    float dv[EIT_RECON_ROUTES];
    float diff_sq_sum = 0.0f;
    float baseline_sq_sum = 0.0f;
    uint32_t valid_count = 0U;
    uint32_t invalid_count = 0U;

    eit_recon_init();

    for (uint32_t route = 0U; route < EIT_RECON_ROUTES; route++)
    {
        float baseline = g_active_baseline[route];
        float value = amp_v[route];
        bool route_valid = valid[route] && isfinite(value) && isfinite(baseline) && (fabsf(baseline) > EIT_RECON_BASELINE_EPS);
        if (route_valid)
        {
            valid_count++;
        }
        else
        {
            value = baseline;
            invalid_count++;
        }

        if (!isfinite(baseline) || (fabsf(baseline) <= EIT_RECON_BASELINE_EPS))
        {
            baseline = 1.0f;
            value = 1.0f;
        }

        dv[route] = (value - baseline) / fabsf(baseline);
        diff_sq_sum += (value - baseline) * (value - baseline);
        baseline_sq_sum += baseline * baseline;
    }

    for (uint32_t node = 0U; node < EIT_RECON_NODES; node++)
    {
        float acc = 0.0f;
        for (uint32_t route = 0U; route < EIT_RECON_ROUTES; route++)
        {
            acc += g_eit_recon_matrix[node][route] * dv[route];
        }
        ds_node[node] = acc;
        g_abs_scratch[node] = fabsf(acc);
    }

    if (NULL != p_summary)
    {
        float ds_min = ds_node[0];
        float ds_max = ds_node[0];
        for (uint32_t node = 1U; node < EIT_RECON_NODES; node++)
        {
            if (ds_node[node] < ds_min)
            {
                ds_min = ds_node[node];
            }
            if (ds_node[node] > ds_max)
            {
                ds_max = ds_node[node];
            }
        }

        qsort(g_abs_scratch, EIT_RECON_NODES, sizeof(g_abs_scratch[0]), compare_float_ascending);
        p_summary->valid_count = valid_count;
        p_summary->invalid_count = invalid_count;
        p_summary->retry_count = retry_count;
        p_summary->ds_min = ds_min;
        p_summary->ds_max = ds_max;
        p_summary->ds_abs_p98 = percentile_sorted(g_abs_scratch, EIT_RECON_NODES, 0.98f);
        p_summary->rel_l2 = (baseline_sq_sum > EIT_RECON_BASELINE_EPS) ? sqrtf(diff_sq_sum / baseline_sq_sum) : NAN;
    }
}

static int compare_float_ascending(const void * p_a, const void * p_b)
{
    float a = *(float const *) p_a;
    float b = *(float const *) p_b;

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

static float percentile_sorted(float const * p_sorted, uint32_t count, float percentile)
{
    if (0U == count)
    {
        return NAN;
    }
    if (1U == count)
    {
        return p_sorted[0];
    }

    float position = percentile * (float) (count - 1U);
    uint32_t low = (uint32_t) floorf(position);
    uint32_t high = low + 1U;
    if (high >= count)
    {
        return p_sorted[count - 1U];
    }

    float frac = position - (float) low;
    return p_sorted[low] + ((p_sorted[high] - p_sorted[low]) * frac);
}
