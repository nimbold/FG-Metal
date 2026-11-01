// R116 shared Metal gate kernels.
//
// This header is the single source of truth for the frozen R110 fast-gate
// metric reduction and the R116 single-struct second stage. Both the in-process
// observer and the offline gate-equivalence harness compile from this exact
// text, so the harness cannot silently drift from the shipped kernel.
//
// The first stage is byte-identical to the R110/R115 kernel: same 16x16 thread
// groups, same 4x4 pixel footprint per lane, same 5-point color sampling, same
// threshold-independent accumulation. The second stage only changes how the
// per-group records are folded together.

#ifndef R116_GATE_KERNELS_H
#define R116_GATE_KERNELS_H

#include <stdint.h>

// Host-side mirror of the MSL struct above. Kept in this header so the observer
// and the harness cannot disagree about the record layout.
#ifndef R116_GATE_GROUP_STATS_HOST_DEFINED
#define R116_GATE_GROUP_STATS_HOST_DEFINED
typedef struct {
    uint32_t finiteMotion;
    uint32_t activeMotion;
    uint32_t motionSamples;
    uint32_t finiteOutput;
    uint32_t outputSamples;
    uint32_t colorSamples;
    float maxMagnitude;
    float diffPrevious;
    float diffCurrent;
} R110GateGroupStats;
#endif

#define R116_GATE_KERNEL_SOURCE \
    "struct R110GateGroupStats { uint finiteMotion; uint activeMotion; uint motionSamples; uint finiteOutput; uint outputSamples; uint colorSamples; float maxMagnitude; float diffPrevious; float diffCurrent; };\n" \
    "kernel void r110FastGate(texture2d<half, access::read> motion [[texture(0)]], texture2d<float, access::read> previous [[texture(1)]], texture2d<float, access::read> current [[texture(2)]], texture2d<float, access::read> midpoint [[texture(3)]], device R110GateGroupStats *groups [[buffer(0)]], uint2 groupId [[threadgroup_position_in_grid]], uint lane [[thread_index_in_threadgroup]]) {\n" \
    "  threadgroup uint finiteMotion[256]; threadgroup uint activeMotion[256]; threadgroup uint motionSamples[256]; threadgroup uint finiteOutput[256]; threadgroup uint outputSamples[256]; threadgroup uint colorSamples[256]; threadgroup uint maxMagnitudeBits[256]; threadgroup float diffPrevious[256]; threadgroup float diffCurrent[256];\n" \
    "  uint laneX=lane%16; uint laneY=lane/16; uint blockX=(groupId.x*16+laneX)*4; uint blockY=(groupId.y*16+laneY)*4; uint w=motion.get_width(); uint h=motion.get_height();\n" \
    "  uint fm=0,am=0,ms=0,fo=0,os=0,cs=0; float maxMag=0.0f,da=0.0f,db=0.0f;\n" \
    "  for(uint oy=0;oy<4;oy++) for(uint ox=0;ox<4;ox++){ uint x=blockX+ox,y=blockY+oy; if(x>=w||y>=h) continue; uint2 p=uint2(x,y); float2 v=float2(motion.read(p).xy); ms++; if(all(isfinite(v))){ fm++; float m=length(v); maxMag=max(maxMag,m); if(m>0.5f) am++; } float4 midpointColor=midpoint.read(p); os++; if(all(isfinite(midpointColor))) fo++; if(ox==2&&oy==2){ float4 a=previous.read(p),b=current.read(p); if(all(isfinite(a))&&all(isfinite(b))&&all(isfinite(midpointColor))){ cs++; da+=(abs(midpointColor.x-a.x)+abs(midpointColor.y-a.y)+abs(midpointColor.z-a.z))/3.0f; db+=(abs(midpointColor.x-b.x)+abs(midpointColor.y-b.y)+abs(midpointColor.z-b.z))/3.0f; } } }\n" \
    "  finiteMotion[lane]=fm; activeMotion[lane]=am; motionSamples[lane]=ms; finiteOutput[lane]=fo; outputSamples[lane]=os; colorSamples[lane]=cs; maxMagnitudeBits[lane]=as_type<uint>(maxMag); diffPrevious[lane]=da; diffCurrent[lane]=db; threadgroup_barrier(mem_flags::mem_threadgroup);\n" \
    "  for(uint stride=128;stride>0;stride>>=1){ if(lane<stride){ finiteMotion[lane]+=finiteMotion[lane+stride]; activeMotion[lane]+=activeMotion[lane+stride]; motionSamples[lane]+=motionSamples[lane+stride]; finiteOutput[lane]+=finiteOutput[lane+stride]; outputSamples[lane]+=outputSamples[lane+stride]; colorSamples[lane]+=colorSamples[lane+stride]; maxMagnitudeBits[lane]=max(maxMagnitudeBits[lane],maxMagnitudeBits[lane+stride]); diffPrevious[lane]+=diffPrevious[lane+stride]; diffCurrent[lane]+=diffCurrent[lane+stride]; } threadgroup_barrier(mem_flags::mem_threadgroup); }\n" \
    "  if(lane==0){ uint groupCountX=(w+63)/64; uint index=groupId.y*groupCountX+groupId.x; groups[index].finiteMotion=finiteMotion[0]; groups[index].activeMotion=activeMotion[0]; groups[index].motionSamples=motionSamples[0]; groups[index].finiteOutput=finiteOutput[0]; groups[index].outputSamples=outputSamples[0]; groups[index].colorSamples=colorSamples[0]; groups[index].maxMagnitude=as_type<float>(maxMagnitudeBits[0]); groups[index].diffPrevious=diffPrevious[0]; groups[index].diffCurrent=diffCurrent[0]; }\n" \
    "}\n" \
    "\n" \
    "struct R110GateResult { uint finiteMotion; uint activeMotion; uint motionSamples; uint finiteOutput; uint outputSamples; uint colorSamples; uint groupCount; uint frameTag; float maxMagnitude; float diffPrevious; float diffCurrent; };\n" \
    "kernel void r110FastGateReduce(device const R110GateGroupStats *groups [[buffer(0)]], device R110GateResult *result [[buffer(1)]], constant uint &groupTotal [[buffer(2)]], constant uint &frameTag [[buffer(3)]], uint lane [[thread_index_in_threadgroup]]) {\n" \
    "  threadgroup uint finiteMotion[256]; threadgroup uint activeMotion[256]; threadgroup uint motionSamples[256]; threadgroup uint finiteOutput[256]; threadgroup uint outputSamples[256]; threadgroup uint colorSamples[256]; threadgroup uint maxMagnitudeBits[256]; threadgroup float diffPrevious[256]; threadgroup float diffCurrent[256];\n" \
    "  uint fm=0,am=0,ms=0,fo=0,os=0,cs=0,mb=0u; float da=0.0f,db=0.0f;\n" \
    "  for(uint i=lane;i<groupTotal;i+=256){ R110GateGroupStats g=groups[i]; fm+=g.finiteMotion; am+=g.activeMotion; ms+=g.motionSamples; fo+=g.finiteOutput; os+=g.outputSamples; cs+=g.colorSamples; mb=max(mb,as_type<uint>(g.maxMagnitude)); da+=g.diffPrevious; db+=g.diffCurrent; }\n" \
    "  finiteMotion[lane]=fm; activeMotion[lane]=am; motionSamples[lane]=ms; finiteOutput[lane]=fo; outputSamples[lane]=os; colorSamples[lane]=cs; maxMagnitudeBits[lane]=mb; diffPrevious[lane]=da; diffCurrent[lane]=db; threadgroup_barrier(mem_flags::mem_threadgroup);\n" \
    "  for(uint stride=128;stride>0;stride>>=1){ if(lane<stride){ finiteMotion[lane]+=finiteMotion[lane+stride]; activeMotion[lane]+=activeMotion[lane+stride]; motionSamples[lane]+=motionSamples[lane+stride]; finiteOutput[lane]+=finiteOutput[lane+stride]; outputSamples[lane]+=outputSamples[lane+stride]; colorSamples[lane]+=colorSamples[lane+stride]; maxMagnitudeBits[lane]=max(maxMagnitudeBits[lane],maxMagnitudeBits[lane+stride]); diffPrevious[lane]+=diffPrevious[lane+stride]; diffCurrent[lane]+=diffCurrent[lane+stride]; } threadgroup_barrier(mem_flags::mem_threadgroup); }\n" \
    "  if(lane==0){ result[0].finiteMotion=finiteMotion[0]; result[0].activeMotion=activeMotion[0]; result[0].motionSamples=motionSamples[0]; result[0].finiteOutput=finiteOutput[0]; result[0].outputSamples=outputSamples[0]; result[0].colorSamples=colorSamples[0]; result[0].groupCount=groupTotal; result[0].frameTag=frameTag; result[0].maxMagnitude=as_type<float>(maxMagnitudeBits[0]); result[0].diffPrevious=diffPrevious[0]; result[0].diffCurrent=diffCurrent[0]; }\n" \
    "}\n"

#endif
