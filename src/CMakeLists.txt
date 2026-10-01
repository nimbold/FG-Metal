add_library(framegen STATIC
    core/backend_registry.cpp
    core/c_api.cpp
    core/frame_generator.cpp
    core/texture.cpp
)
add_library(framegen::core ALIAS framegen)
add_library(framegen::framegen ALIAS framegen)

target_compile_features(framegen PUBLIC cxx_std_23)
target_include_directories(framegen
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/../include>
    PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/core
)
target_compile_definitions(framegen PRIVATE FRAMEGEN_BUILDING_LIBRARY=1)

if(CMAKE_CXX_COMPILER_ID MATCHES "AppleClang|Clang|GNU")
    target_compile_options(framegen PRIVATE -Wall -Wextra -Wpedantic)
endif()

# Optional platform backends live outside the renderer-neutral core.
if(FRAMEGEN_BUILD_METAL AND APPLE)
    add_subdirectory(metal)
endif()
