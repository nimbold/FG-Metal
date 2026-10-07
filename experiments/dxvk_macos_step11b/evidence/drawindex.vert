#version 450
#extension GL_ARB_shader_draw_parameters : require
void main() {
  vec2 p = vec2(float(gl_VertexIndex & 2), float(gl_VertexIndex & 1)*2.0);
  gl_Position = vec4(p*2.0-1.0, float(gl_DrawIDARB)*0.001, 1.0);
}
