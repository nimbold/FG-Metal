#version 450
// TEST ONLY: source-independent diagnostic; selected only by explicit environment.
layout(location = 0) out vec4 o_color;
void main() { o_color = vec4(1.0, 0.0, 1.0, 1.0); }
