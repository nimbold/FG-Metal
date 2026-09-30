if(NOT APPLE)
    message(FATAL_ERROR "The Metal backend requires macOS.")
endif()

add_library(framegen_metal STATIC
    metal_backend.mm
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
