// The ground grid's plane: hands each fragment its point in the scene, where the grid lines are measured.
VARYING vec3 worldPosition;

void MAIN()
{
    worldPosition = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz;
    POSITION = MODELVIEWPROJECTION_MATRIX * vec4(VERTEX, 1.0);
}
