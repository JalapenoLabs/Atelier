// The ground grid on the plane Y = 0 (Unreal Z = 0): a line every `interval` cm at half strength, every ten
// intervals at full strength, and the Unreal X and Y axes (Qt's X and Z) in their own colours.
//
// Lines are measured in screen pixels (fwidth), so they stay about a pixel wide at any distance. A set of lines fades
// out where it grows denser than a few pixels apart, and the whole grid fades towards `fadeDistance`, so it never
// shimmers towards the horizon.
//
// Qt hands colour properties to the shader in linear light but writes an unshaded material's output as it is, so
// the colour is encoded back to sRGB here. The output is premultiplied, to match the material's One /
// OneMinusSrcAlpha blending.
VARYING vec3 worldPosition;

vec3 toSrgb(vec3 linear)
{
    vec3 low = linear * 12.92;
    vec3 high = 1.055 * pow(linear, vec3(1.0 / 2.4)) - 0.055;
    return mix(high, low, vec3(lessThanEqual(linear, vec3(0.0031308))));
}

float lineCoverage(vec2 coordinate)
{
    vec2 cellsPerPixel = fwidth(coordinate);
    vec2 pixelsToLine = abs(fract(coordinate - 0.5) - 0.5) / cellsPerPixel;
    float coverage = 1.0 - min(min(pixelsToLine.x, pixelsToLine.y), 1.0);
    float density = max(cellsPerPixel.x, cellsPerPixel.y);
    return coverage * (1.0 - smoothstep(0.15, 0.4, density));
}

float axisCoverage(float coordinate)
{
    return 1.0 - clamp(abs(coordinate) / fwidth(coordinate) - 0.5, 0.0, 1.0);
}

void MAIN()
{
    vec2 ground = worldPosition.xz;
    float minor = lineCoverage(ground / interval);
    float major = lineCoverage(ground / (interval * 10.0));
    vec4 color = vec4(gridColor.rgb, max(minor * 0.5, major));
    color = mix(color, vec4(axisXColor.rgb, 1.0), axisCoverage(ground.y));
    color = mix(color, vec4(axisYColor.rgb, 1.0), axisCoverage(ground.x));

    float fade = 1.0 - smoothstep(fadeDistance * 0.5, fadeDistance, distance(worldPosition, CAMERA_POSITION));
    float alpha = color.a * fade;
    FRAGCOLOR = vec4(toSrgb(color.rgb) * alpha, alpha);
}
