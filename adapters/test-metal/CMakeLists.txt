if(NOT APPLE)
    message(FATAL_ERROR "The Metal test host requires macOS.")
endif()

add_executable(framegen-test-metal
    main.mm
)
target_compile_features(framegen-test-metal PRIVATE cxx_std_23)
target_link_libraries(framegen-test-metal
    PRIVATE
        framegen::metal
        "-framework AppKit"
        "-framework Foundation"
        "-framework Metal"
        "-framework MetalKit"
        "-framework QuartzCore"
)
target_compile_options(framegen-test-metal PRIVATE
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-fobjc-arc>"
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-Wall>"
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-Wextra>"
    "$<$<COMPILE_LANGUAGE:OBJCXX>:-Wpedantic>"
)
set_target_properties(framegen-test-metal PROPERTIES
    MACOSX_BUNDLE YES
    MACOSX_BUNDLE_GUI_IDENTIFIER "org.framegen.test-metal"
    MACOSX_BUNDLE_BUNDLE_NAME "FrameGen Metal Test"
    OBJCXX_STANDARD 23
    OBJCXX_STANDARD_REQUIRED YES
    OBJCXX_EXTENSIONS NO
)
