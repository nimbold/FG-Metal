#include <metal_stdlib>
using namespace metal;

inline int2 r110ClampPoint(int2 p, texture2d<float, access::read> image) {
  return clamp(p, int2(0), int2(int(image.get_width()) - 1, int(image.get_height()) - 1));
}

inline float r110Luma(float3 c) {
  return dot(c, float3(0.2126f, 0.7152f, 0.0722f));
}

kernel void r110Downsample(texture2d<float, access::read> previous [[texture(0)]],
                           texture2d<float, access::read> current [[texture(1)]],
                           texture2d<half, access::write> previousQuarter [[texture(2)]],
                           texture2d<half, access::write> currentQuarter [[texture(3)]],
                           uint2 gid [[thread_position_in_grid]]) {
  if (gid.x >= previousQuarter.get_width() || gid.y >= previousQuarter.get_height()) return;
  int2 base = int2(gid) * 4;
  float4 a = 0.0f;
  float4 b = 0.0f;
  for (int y = 0; y < 4; ++y) for (int x = 0; x < 4; ++x) {
    int2 p = base + int2(x, y);
    p.x = min(p.x, int(previous.get_width()) - 1);
    p.y = min(p.y, int(previous.get_height()) - 1);
    a += previous.read(uint2(p));
    b += current.read(uint2(p));
  }
  previousQuarter.write(half4(a * (1.0f / 16.0f)), gid);
  currentQuarter.write(half4(b * (1.0f / 16.0f)), gid);
}

inline float r110CoarsePatchCost(texture2d<half, access::read> previous,
                                 texture2d<half, access::read> current,
                                 int2 center, float3 targetColor, int2 shift) {
  float sum = 0.0f;
  float weightSum = 0.0f;
  const int maxX = int(current.get_width()) - 1;
  const int maxY = int(current.get_height()) - 1;
  for (int y = -24; y <= 24; y += 4) for (int x = -24; x <= 24; x += 4) {
    int2 p = clamp(center + int2(x, y), int2(0), int2(maxX, maxY));
    int2 q = clamp(p + shift, int2(0), int2(int(previous.get_width()) - 1,
                                             int(previous.get_height()) - 1));
    float3 a = float3(current.read(uint2(p)).rgb);
    float3 b = float3(previous.read(uint2(q)).rgb);
    float targetY = r110Luma(targetColor);
    float sampleY = r110Luma(a);
    float colorDifference = length(a - targetColor);
    if (targetY > 0.15f && sampleY > 0.15f && colorDifference > 0.15f) continue;
    float weight = (targetY > 0.15f && sampleY > 0.15f && colorDifference <= 0.15f)
        ? 4.0f : 1.0f;
    sum += weight * abs(r110Luma(a) - r110Luma(b));
    weightSum += weight;
  }
  return sum / max(weightSum, 1.0e-5f);
}

kernel void r110CoarseMatch(texture2d<half, access::read> previousQuarter [[texture(0)]],
                            texture2d<half, access::read> currentQuarter [[texture(1)]],
                            texture2d<float, access::write> coarseGrid [[texture(2)]],
                            uint2 gid [[thread_position_in_grid]]) {
  if (gid.x >= coarseGrid.get_width() || gid.y >= coarseGrid.get_height()) return;
  int2 center = int2(float(gid.x) * float(currentQuarter.get_width()) / float(coarseGrid.get_width()),
                     float(gid.y) * float(currentQuarter.get_height()) / float(coarseGrid.get_height()));
  float3 target = float3(currentQuarter.read(uint2(center)).rgb);
  float costs[49];
  int2 offsets[49];
  int bestIndex = 0;
  float bestCost = INFINITY;
  int index = 0;
  for (int dy = -6; dy <= 6; dy += 2) for (int dx = -6; dx <= 6; dx += 2) {
    float cost = r110CoarsePatchCost(previousQuarter, currentQuarter, center, target, int2(dx, dy));
    cost += 0.000001f * float(abs(dx) + abs(dy));
    costs[index] = cost;
    offsets[index] = int2(dx, dy);
    if (cost < bestCost) { bestCost = cost; bestIndex = index; }
    ++index;
  }
  float farCost = INFINITY;
  for (int i = 0; i < 49; ++i) {
    int2 delta = offsets[i] - offsets[bestIndex];
    if (abs(delta.x) <= 2 && abs(delta.y) <= 2) continue;
    farCost = min(farCost, costs[i]);
  }
  float confidence = isfinite(farCost) && farCost > 1.0e-5f
      ? clamp((farCost - bestCost) / farCost, 0.0f, 1.0f) : 0.0f;
  int2 fullVector = offsets[bestIndex] * 4;
  coarseGrid.write(float4(float2(fullVector), confidence, bestCost), gid);
}

inline float r110FinePatchCost(texture2d<float, access::read> previous,
                               texture2d<float, access::read> current,
                               int2 center, float3 targetColor, int2 shift) {
  float sum = 0.0f;
  float weightSum = 0.0f;
  for (int y = -96; y <= 96; y += 12) for (int x = -96; x <= 96; x += 12) {
    int2 p = r110ClampPoint(center + int2(x, y), current);
    int2 q = r110ClampPoint(p + shift, previous);
    float3 a = current.read(uint2(p)).rgb;
    float3 b = previous.read(uint2(q)).rgb;
    float targetY = r110Luma(targetColor);
    float sampleY = r110Luma(a);
    float colorDifference = length(a - targetColor);
    if (targetY > 0.15f && sampleY > 0.15f && colorDifference > 0.15f) continue;
    float weight = (targetY > 0.15f && sampleY > 0.15f && colorDifference <= 0.15f)
        ? 4.0f : 1.0f;
    sum += weight * abs(r110Luma(a) - r110Luma(b));
    weightSum += weight;
  }
  return sum / max(weightSum, 1.0e-5f);
}

kernel void r110FineRefine(texture2d<float, access::read> previous [[texture(0)]],
                           texture2d<float, access::read> current [[texture(1)]],
                           texture2d<float, access::read> coarseGrid [[texture(2)]],
                           texture2d<float, access::write> refinedGrid [[texture(3)]],
                           uint2 gid [[thread_position_in_grid]]) {
  if (gid.x >= refinedGrid.get_width() || gid.y >= refinedGrid.get_height()) return;
  float4 seed = coarseGrid.read(gid);
  int2 center = int2(float(gid.x) * float(current.get_width()) / float(refinedGrid.get_width()),
                     float(gid.y) * float(current.get_height()) / float(refinedGrid.get_height()));
  if (seed.z < 0.05f) { refinedGrid.write(float4(0.0f), gid); return; }
  float3 target = current.read(uint2(center)).rgb;
  int2 base = int2(seed.xy);
  int bestX = base.x;
  int bestY = base.y;
  float bestCost = INFINITY;
  for (int dy = base.y - 4; dy <= base.y + 4; ++dy) {
    for (int dx = base.x - 4; dx <= base.x + 4; ++dx) {
      int2 shift = clamp(int2(dx, dy), int2(-28), int2(28));
      float cost = r110FinePatchCost(previous, current, center, target, shift)
          + 0.000001f * float(abs(shift.x) + abs(shift.y));
      if (cost < bestCost) { bestCost = cost; bestX = shift.x; bestY = shift.y; }
    }
  }
  float cx = r110FinePatchCost(previous, current, center, target, int2(bestX, bestY));
  float cl = r110FinePatchCost(previous, current, center, target, int2(max(-28, bestX - 1), bestY));
  float cr = r110FinePatchCost(previous, current, center, target, int2(min(28, bestX + 1), bestY));
  float cu = r110FinePatchCost(previous, current, center, target, int2(bestX, max(-28, bestY - 1)));
  float cd = r110FinePatchCost(previous, current, center, target, int2(bestX, min(28, bestY + 1)));
  float denomX = cl - 2.0f * cx + cr;
  float denomY = cu - 2.0f * cx + cd;
  float subX = abs(denomX) > 1.0e-6f ? clamp(0.5f * (cl - cr) / denomX, -0.5f, 0.5f) : 0.0f;
  float subY = abs(denomY) > 1.0e-6f ? clamp(0.5f * (cu - cd) / denomY, -0.5f, 0.5f) : 0.0f;
  refinedGrid.write(float4(float(bestX) + subX, float(bestY) + subY, seed.z, bestCost), gid);
}

kernel void r110Propagate(texture2d<float, access::read> current [[texture(0)]],
                          texture2d<float, access::read> input [[texture(1)]],
                          texture2d<float, access::write> output [[texture(2)]],
                          uint2 gid [[thread_position_in_grid]]) {
  if (gid.x >= input.get_width() || gid.y >= input.get_height()) return;
  float4 base = input.read(gid);
  if (base.z >= 0.05f) { output.write(base, gid); return; }
  int2 center = int2(float(gid.x) * float(current.get_width()) / float(input.get_width()),
                     float(gid.y) * float(current.get_height()) / float(input.get_height()));
  float3 c = current.read(uint2(center)).rgb;
  if (r110Luma(c) < 0.05f) { output.write(float4(0.0f), gid); return; }
  float scoreBest = 0.0f;
  float4 best = base;
  for (int y = -3; y <= 3; ++y) for (int x = -3; x <= 3; ++x) {
    if (x == 0 && y == 0) continue;
    int2 q = int2(gid) + int2(x, y);
    if (q.x < 0 || q.y < 0 || q.x >= int(input.get_width()) || q.y >= int(input.get_height())) continue;
    float4 f = input.read(uint2(q));
    if (f.z <= 0.05f) continue;
    int2 qc = int2(float(q.x) * float(current.get_width()) / float(input.get_width()),
                   float(q.y) * float(current.get_height()) / float(input.get_height()));
    float3 color = current.read(uint2(qc)).rgb;
    if (length(c - color) > 0.12f) continue;
    float score = f.z / (1.0f + float(abs(x) + abs(y)));
    if (score > scoreBest) { scoreBest = score; best = f; }
  }
  if (scoreBest > 0.0f) { best.z *= 0.75f; output.write(best, gid); }
  else output.write(float4(0.0f), gid);
}

kernel void r110Upsample(texture2d<float, access::read> grid [[texture(0)]],
                         texture2d<half, access::write> motion [[texture(1)]],
                         uint2 gid [[thread_position_in_grid]]) {
  if (gid.x >= motion.get_width() || gid.y >= motion.get_height()) return;
  uint2 p = uint2(min(grid.get_width() - 1, uint((float(gid.x) + float(motion.get_width()) / float(grid.get_width()) * 0.5f) * float(grid.get_width()) / float(motion.get_width()))),
                  min(grid.get_height() - 1, uint((float(gid.y) + float(motion.get_height()) / float(grid.get_height()) * 0.5f) * float(grid.get_height()) / float(motion.get_height()))));
  float2 v = grid.read(p).xy;
  motion.write(half4(half(v.x), half(v.y), 0.0h, 0.0h), gid);
}
