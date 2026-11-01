#import <Foundation/Foundation.h>
#import <math.h>
#import <stdint.h>
#import <stdio.h>
#import <stdlib.h>

typedef struct {
    const char *name;
    float expectedX, expectedY;
    float *x, *y, *magnitude, *error, *delta;
    NSUInteger count, zero, withinOne, withinTwo;
} Region;

static float halfToFloat(uint16_t h) {
    uint16_t sign = (h >> 15) & 1, exponent = (h >> 10) & 31, mantissa = h & 1023;
    if (exponent == 0) return sign ? -ldexpf((float)mantissa, -24) : ldexpf((float)mantissa, -24);
    if (exponent == 31) return mantissa ? NAN : (sign ? -INFINITY : INFINITY);
    float value = ldexpf(1.0f + (float)mantissa / 1024.0f, (int)exponent - 15);
    return sign ? -value : value;
}

static int compareFloat(const void *a, const void *b) {
    float x = *(const float *)a, y = *(const float *)b;
    return (x > y) - (x < y);
}

static float median(float *values, NSUInteger count) {
    qsort(values, count, sizeof(float), compareFloat);
    return count ? values[count / 2] : NAN;
}

static void printRegion(Region *region) {
    float mx = median(region->x, region->count);
    float my = median(region->y, region->count);
    float mm = median(region->magnitude, region->count);
    float me = median(region->error, region->count);
    float md = median(region->delta, region->count);
    printf("R110_HARNESS_REGION name=%s pixels=%lu expected=(%.5f,%.5f) median=(%.5f,%.5f) medianMagnitude=%.5f medianError=%.5f candidateVsR109MedianVectorDelta=%.5f within1px=%lu within2px=%lu zeroVectors=%lu\n",
        region->name, (unsigned long)region->count, region->expectedX, region->expectedY,
        mx, my, mm, me, md, (unsigned long)region->withinOne,
        (unsigned long)region->withinTwo, (unsigned long)region->zero);
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 7) {
            fprintf(stderr, "usage: %s sourceB.raw candidate.rg16f baseline.rg16f width height output.log\n", argv[0]);
            return 2;
        }
        NSUInteger width = strtoul(argv[4], NULL, 10), height = strtoul(argv[5], NULL, 10);
        NSData *source = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:argv[1]]];
        NSData *candidate = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:argv[2]]];
        NSData *baseline = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:argv[3]]];
        NSUInteger pixels = width * height;
        if (!width || !height || !source || !candidate || !baseline ||
            source.length != pixels * 4 || candidate.length != pixels * 4 || baseline.length != pixels * 4) {
            fprintf(stderr, "INPUT_INVALID source=%lu candidate=%lu baseline=%lu expected=%lu\n",
                (unsigned long)source.length, (unsigned long)candidate.length,
                (unsigned long)baseline.length, (unsigned long)(pixels * 4));
            return 3;
        }
        freopen(argv[6], "w", stdout);
        const uint8_t *b = source.bytes;
        const uint16_t *flow = candidate.bytes, *reference = baseline.bytes;
        Region regions[] = {
            {.name="main-shape", .expectedX=-4.38679f, .expectedY=0.0f},
            {.name="fast-marker", .expectedX=-15.0f, .expectedY=0.0f},
            {.name="static-background", .expectedX=0.0f, .expectedY=0.0f},
        };
        for (NSUInteger r = 0; r < 3; ++r) {
            regions[r].x = calloc(pixels, sizeof(float));
            regions[r].y = calloc(pixels, sizeof(float));
            regions[r].magnitude = calloc(pixels, sizeof(float));
            regions[r].error = calloc(pixels, sizeof(float));
            regions[r].delta = calloc(pixels, sizeof(float));
            if (!regions[r].x || !regions[r].y || !regions[r].magnitude ||
                !regions[r].error || !regions[r].delta) return 4;
        }
        NSUInteger candidateNonfinite = 0, baselineNonfinite = 0;
        for (NSUInteger y = 0; y < height; ++y) for (NSUInteger x = 0; x < width; ++x) {
            NSUInteger index = y * width + x, offset = index * 4;
            const uint8_t *pixel = b + offset;
            BOOL mainShape = pixel[0] > 220 && pixel[1] > 160 && pixel[2] < 80;
            BOOL marker = pixel[0] < 80 && pixel[1] > 100 && pixel[2] > 220;
            BOOL background = x > 500 && y > 100 && y + 100 < height && !mainShape && !marker;
            float dx = halfToFloat(flow[index * 2]), dy = halfToFloat(flow[index * 2 + 1]);
            float bx = halfToFloat(reference[index * 2]), by = halfToFloat(reference[index * 2 + 1]);
            if (!isfinite(dx) || !isfinite(dy)) { ++candidateNonfinite; continue; }
            if (!isfinite(bx) || !isfinite(by)) ++baselineNonfinite;
            BOOL selected[] = {mainShape, marker, background};
            for (NSUInteger r = 0; r < 3; ++r) {
                if (!selected[r]) continue;
                Region *region = &regions[r];
                NSUInteger n = region->count++;
                float magnitude = hypotf(dx, dy);
                float error = hypotf(dx - region->expectedX, dy - region->expectedY);
                region->x[n] = dx; region->y[n] = dy;
                region->magnitude[n] = magnitude; region->error[n] = error;
                region->delta[n] = hypotf(dx - bx, dy - by);
                region->zero += dx == 0.0f && dy == 0.0f;
                region->withinOne += error <= 1.0f;
                region->withinTwo += error <= 2.0f;
            }
        }
        printf("R110_HARNESS_QUALITY pair=3->4 dimensions=%lux%lu candidateNonfinite=%lu baselineNonfinite=%lu baseline=R109_R108_BASELINE\n",
            (unsigned long)width, (unsigned long)height,
            (unsigned long)candidateNonfinite, (unsigned long)baselineNonfinite);
        for (NSUInteger r = 0; r < 3; ++r) printRegion(&regions[r]);
        return 0;
    }
}
