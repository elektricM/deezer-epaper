/* Error diffusion, the one hot loop in a render.
 *
 * This is a transcription of _diffuse_py in panel.py, not a reimplementation.
 * It must produce byte-identical output, so the operation order, the clamp
 * placement and the truncating cast are all deliberate and must not be
 * "tidied". Build without -ffast-math: reassociation would change results.
 *
 * Every value is double, matching Python floats exactly, and both languages
 * truncate toward zero on a float-to-integer cast.
 */
#include <stddef.h>

void diffuse(double *px, const long *lut, const double *pal,
             const long *quant, long lut_bits, double lo, double hi,
             long h, long w, const long *kdx, const long *kdy,
             const double *kwt, long ntap, double divisor,
             unsigned char *out)
{
    const long stride = w * 3;
    const long shift2 = 2 * lut_bits;

    for (long y = 0; y < h; y++) {
        const int rev = (y & 1) == 1;
        const long base = y * stride;
        for (long i = 0; i < w; i++) {
            const long x = rev ? (w - 1 - i) : i;
            const long j = base + x * 3;
            const double r = px[j], g = px[j + 1], b = px[j + 2];

            long ir = r < 0 ? 0 : (r > 255 ? 255 : (long)r);
            long ig = g < 0 ? 0 : (g > 255 ? 255 : (long)g);
            long ib = b < 0 ? 0 : (b > 255 ? 255 : (long)b);

            const long k = lut[(quant[ir] << shift2)
                               | (quant[ig] << lut_bits) | quant[ib]];
            out[y * w + x] = (unsigned char)k;

            const double er = r - pal[k * 3];
            const double eg = g - pal[k * 3 + 1];
            const double eb = b - pal[k * 3 + 2];

            for (long t = 0; t < ntap; t++) {
                const long nx = x + (rev ? -kdx[t] : kdx[t]);
                const long ny = y + kdy[t];
                if (nx < 0 || nx >= w || ny < 0 || ny >= h) continue;
                const long m = ny * stride + nx * 3;
                const double f = kwt[t] / divisor;
                double v;
                v = px[m]     + er * f; px[m]     = v < lo ? lo : (v > hi ? hi : v);
                v = px[m + 1] + eg * f; px[m + 1] = v < lo ? lo : (v > hi ? hi : v);
                v = px[m + 2] + eb * f; px[m + 2] = v < lo ? lo : (v > hi ? hi : v);
            }
        }
    }
}
