#include <metal_stdlib>
using namespace metal;
inline float r108PatchCost(texture2d<float, access::read> previous, texture2d<float, access::read> current, int2 center, float3 targetColor, int2 shift, int sampleStep) {
  float sum = 0.0f; float weightSum = 0.0f;
  for (int y=-96; y<=96; y+=sampleStep) for (int x=-96; x<=96; x+=sampleStep) {
    int2 p=center+int2(x,y);
    float3 a=current.read(uint2(clamp(p,int2(0),int2(int(current.get_width())-1,int(current.get_height())-1)))).rgb;
    float targetY=dot(targetColor,float3(0.2126f,0.7152f,0.0722f));
    float sampleY=dot(a,float3(0.2126f,0.7152f,0.0722f));
    float colorDifference=length(a-targetColor);
    if(targetY>0.15f && sampleY>0.15f && colorDifference>0.15f) continue;
    float3 b=previous.read(uint2(clamp(p+shift,int2(0),int2(int(previous.get_width())-1,int(previous.get_height())-1)))).rgb;
    float weight=(targetY>0.15f && sampleY>0.15f && colorDifference<=0.15f)?4.0f:1.0f;
    sum+=weight*abs(dot(a,float3(0.2126f,0.7152f,0.0722f))-dot(b,float3(0.2126f,0.7152f,0.0722f))); weightSum+=weight;
  }
  return sum/max(weightSum,1e-5f);
}
kernel void r108BlockMatch(texture2d<float, access::read> previous [[texture(0)]], texture2d<float, access::read> current [[texture(1)]], texture2d<float, access::write> grid [[texture(2)]], uint2 gid [[thread_position_in_grid]]) {
  if(gid.x>=grid.get_width()||gid.y>=grid.get_height()) return;
  int2 center=int2(float(gid.x)*float(current.get_width())/float(grid.get_width()),float(gid.y)*float(current.get_height())/float(grid.get_height()));
  float3 target=current.read(uint2(center)).rgb; int bestX=0; float bestXCost=INFINITY;
  for(int dx=-20;dx<=20;dx+=2){float c=r108PatchCost(previous,current,center,target,int2(dx,0),8)+0.000001f*float(abs(dx));if(c<bestXCost){bestXCost=c;bestX=dx;}}
  bestXCost=INFINITY; for(int dx=max(-20,bestX-4);dx<=min(20,bestX+4);++dx){float c=r108PatchCost(previous,current,center,target,int2(dx,0),4)+0.000001f*float(abs(dx));if(c<bestXCost){bestXCost=c;bestX=dx;}}
  int bestY=0; float bestYCost=INFINITY; for(int dy=-20;dy<=20;dy+=2){float c=r108PatchCost(previous,current,center,target,int2(0,dy),8)+0.000001f*float(abs(dy));if(c<bestYCost){bestYCost=c;bestY=dy;}}
  bestYCost=INFINITY; for(int dy=max(-20,bestY-4);dy<=min(20,bestY+4);++dy){float c=r108PatchCost(previous,current,center,target,int2(0,dy),4)+0.000001f*float(abs(dy));if(c<bestYCost){bestYCost=c;bestY=dy;}}
  float cx=r108PatchCost(previous,current,center,target,int2(bestX,0),4); float cl=r108PatchCost(previous,current,center,target,int2(bestX-1,0),4); float cr=r108PatchCost(previous,current,center,target,int2(bestX+1,0),4);
  float cy=r108PatchCost(previous,current,center,target,int2(0,bestY),4); float cu=r108PatchCost(previous,current,center,target,int2(0,bestY-1),4); float cd=r108PatchCost(previous,current,center,target,int2(0,bestY+1),4);
  float rx=abs(cl-2.0f*cx+cr)>1e-6f?clamp(0.5f*(cl-cr)/(cl-2.0f*cx+cr),-0.5f,0.5f):0.0f; float ry=abs(cu-2.0f*cy+cd)>1e-6f?clamp(0.5f*(cu-cd)/(cu-2.0f*cy+cd),-0.5f,0.5f):0.0f;
  float farX=INFINITY,farY=INFINITY; for(int d=-20;d<=20;d+=2){if(abs(d-bestX)>10){float c=r108PatchCost(previous,current,center,target,int2(d,0),8);if(c<farX)farX=c;}if(abs(d-bestY)>10){float c=r108PatchCost(previous,current,center,target,int2(0,d),8);if(c<farY)farY=c;}}
  float bx=r108PatchCost(previous,current,center,target,int2(bestX,0),8); float by=r108PatchCost(previous,current,center,target,int2(0,bestY),8);
  float qx=isfinite(farX)&&farX>1e-5f?clamp((farX-bx)/farX,0.0f,1.0f):0.0f; float qy=isfinite(farY)&&farY>1e-5f?clamp((farY-by)/farY,0.0f,1.0f):0.0f;
  grid.write(float4(qx>0.05f?float(bestX)+rx:0.0f,qy>0.05f?float(bestY)+ry:0.0f,max(qx,qy),min(bx,by)),gid);
}
kernel void r108Propagate(texture2d<float, access::read> current [[texture(0)]],texture2d<float, access::read> input [[texture(1)]],texture2d<float, access::write> output [[texture(2)]],uint2 gid [[thread_position_in_grid]]) {
  if(gid.x>=input.get_width()||gid.y>=input.get_height())return; float4 base=input.read(gid);if(base.z>=0.05f){output.write(base,gid);return;}
  int2 center=int2(float(gid.x)*float(current.get_width())/float(input.get_width()),float(gid.y)*float(current.get_height())/float(input.get_height()));float3 c=current.read(uint2(center)).rgb;if(dot(c,float3(0.2126f,0.7152f,0.0722f))<0.05f){output.write(float4(0.0f),gid);return;}
  float scoreBest=0.0f;float4 best=base;for(int y=-3;y<=3;++y)for(int x=-3;x<=3;++x){if(x==0&&y==0)continue;int2 q=int2(gid)+int2(x,y);if(q.x<0||q.y<0||q.x>=int(input.get_width())||q.y>=int(input.get_height()))continue;float4 f=input.read(uint2(q));if(f.z<=0.05f)continue;int2 qc=int2(float(q.x)*float(current.get_width())/float(input.get_width()),float(q.y)*float(current.get_height())/float(input.get_height()));float3 color=current.read(uint2(qc)).rgb;if(length(c-color)>0.12f)continue;float score=f.z/(1.0f+float(abs(x)+abs(y)));if(score>scoreBest){scoreBest=score;best=f;}}
  if(scoreBest>0.0f){best.z*=0.75f;output.write(best,gid);}else output.write(float4(0.0f),gid);
}
kernel void r108Upsample(texture2d<float, access::read> grid [[texture(0)]],texture2d<half, access::write> motion [[texture(1)]],uint2 gid [[thread_position_in_grid]]) {
  if(gid.x>=motion.get_width()||gid.y>=motion.get_height())return;uint2 p=uint2(min(grid.get_width()-1,uint((float(gid.x)+float(motion.get_width())/float(grid.get_width())*0.5f)*float(grid.get_width())/float(motion.get_width()))),min(grid.get_height()-1,uint((float(gid.y)+float(motion.get_height())/float(grid.get_height())*0.5f)*float(grid.get_height())/float(motion.get_height()))));float2 v=grid.read(p).xy;motion.write(half4(half(v.x),half(v.y),half(0.0f),half(0.0f)),gid);
}
kernel void r108Prepare(texture2d<float, access::read> source [[texture(0)]],texture2d<half, access::write> color [[texture(1)]],texture2d<float, access::write> depth [[texture(2)]],uint2 gid [[thread_position_in_grid]]) {
  if(gid.x>=source.get_width()||gid.y>=source.get_height())return;float4 c=source.read(gid);color.write(half4(half3(c.rgb),half(c.a)),gid);depth.write(float4(0.25f,0.0f,0.0f,0.0f),gid);
}
kernel void r108ClearMotion(texture2d<half, access::write> motion [[texture(0)]],uint2 gid [[thread_position_in_grid]]) { if(gid.x<motion.get_width()&&gid.y<motion.get_height())motion.write(half4(0.0h),gid); }
