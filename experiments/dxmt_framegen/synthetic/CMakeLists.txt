cmake_minimum_required(VERSION 3.25)

# Standalone, downstream experiment. Keep Framegen's public API and normal
# Metal/RIFE target out of this model-free test host.
project(FramegenDxmtSyntheticMetal LANGUAGES C CXX OBJCXX)

if(NOT APPLE)
    message(FATAL_ERROR "The synthetic Metal host requires macOS.")
endif()

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(FRAMEGEN_BUILD_LIBRARY ON CACHE BOOL "Build Framegen core" FORCE)
set(FRAMEGEN_BUILD_TESTS OFF CACHE BOOL "Skip Framegen's unrelated test suite" FORCE)
set(FRAMEGEN_BUILD_TOOLS OFF CACHE BOOL "Skip Framegen benchmark tools" FORCE)
set(FRAMEGEN_BUILD_METAL OFF CACHE BOOL "Do not build the RIFE Metal backend" FORCE)
set(FRAMEGEN_BUILD_TEST_METAL OFF CACHE BOOL "Do not build the RIFE Metal host" FORCE)

get_filename_component(FRAMEGEN_SOURCE_ROOT "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)
add_subdirectory("${FRAMEGEN_SOURCE_ROOT}" framegen-core EXCLUDE_FROM_ALL)

add_executable(framegen-dxmt-synthetic-metal
    main.mm
    synthetic_metal_backend.mm
)
target_link_libraries(framegen-dxmt-synthetic-metal PRIVATE
    framegen::core
    "-framework AppKit"
    "-framework Foundation"
    "-framework Metal"
    "-framework MetalKit"
    "-framework QuartzCore"
)
target_compile_options(framegen-dxmt-synthetic-metal PRIVATE
    -fobjc-arc
    -Wall
    -Wextra
    -Wpedantic
)
set_target_properties(framegen-dxmt-synthetic-metal PROPERTIES
    OBJCXX_STANDARD 23
    OBJCXX_STANDARD_REQUIRED YES
    OBJCXX_EXTENSIONS NO
    MACOSX_BUNDLE YES
    MACOSX_BUNDLE_GUI_IDENTIFIER "org.framegen.dxmt.synthetic-metal"
    MACOSX_BUNDLE_BUNDLE_NAME "Framegen Synthetic Metal"
)
