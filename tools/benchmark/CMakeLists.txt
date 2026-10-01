if(NOT APPLE)
    message(FATAL_ERROR "The Metal benchmark runner requires macOS; the Python analyzer remains usable on other hosts.")
endif()

if(NOT FRAMEGEN_BUILD_METAL)
    message(FATAL_ERROR "The Metal benchmark runner requires FRAMEGEN_BUILD_METAL=ON.")
endif()

add_executable(framegen-benchmark-metal
    metal_runner.mm
)
target_compile_features(framegen-benchmark-metal PRIVATE cxx_std_23)
target_include_directories(framegen-benchmark-metal PRIVATE
    "${PROJECT_SOURCE_DIR}/src/metal"
)
target_link_libraries(framegen-benchmark-metal
    PRIVATE
        framegen::metal
        "-framework Foundation"
        "-framework Metal"
)
target_compile_options(framegen-benchmark-metal PRIVATE
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-fobjc-arc>"
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-Wall>"
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-Wextra>"
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-Wpedantic>"
)
set_target_properties(framegen-benchmark-metal PROPERTIES
    OBJCXX_STANDARD 23
    OBJCXX_STANDARD_REQUIRED YES
    OBJCXX_EXTENSIONS NO
)
