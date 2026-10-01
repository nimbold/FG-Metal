if(NOT APPLE)
    message(FATAL_ERROR "The Metal backend requires macOS.")
endif()

add_library(framegen_metal STATIC
    metal_c_api.mm
    metal_backend.mm
    rife_model.mm
)
add_library(framegen::metal ALIAS framegen_metal)

target_compile_features(framegen_metal PUBLIC cxx_std_23)
target_include_directories(framegen_metal
    PUBLIC
        $<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/include>
)
target_link_libraries(framegen_metal
    PUBLIC
        framegen::core
        "-framework Foundation"
        "-framework Metal"
        "-framework MetalPerformanceShaders"
        "-framework MetalPerformanceShadersGraph"
)
target_include_directories(framegen_metal PRIVATE
    ${PROJECT_SOURCE_DIR}/src/core
    ${CMAKE_CURRENT_BINARY_DIR}
)

# Compile the model's purpose-built Metal kernels from a source literal at
# runtime. The generated header keeps the kernels inside the static library,
# so the standalone host and consumers do not depend on a loose shader file.
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/rife_model.metal" FRAMEGEN_RIFE_METAL_SOURCE)
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/rife_model_shader.hpp.in"
    "${CMAKE_CURRENT_BINARY_DIR}/rife_model_shader.hpp"
    @ONLY
)
target_compile_options(framegen_metal PRIVATE
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-fobjc-arc>"
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-Wall>"
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-Wextra>"
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-Wpedantic>"
)
set_target_properties(framegen_metal PROPERTIES
    OBJCXX_STANDARD 23
    OBJCXX_STANDARD_REQUIRED YES
    OBJCXX_EXTENSIONS NO
)
