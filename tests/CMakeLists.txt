if(NOT TARGET framegen::core)
    message(FATAL_ERROR "framegen-tests requires FRAMEGEN_BUILD_LIBRARY=ON")
endif()

add_executable(framegen-tests
    core_tests.cpp
)

target_link_libraries(framegen-tests PRIVATE framegen::core)
target_compile_features(framegen-tests PRIVATE cxx_std_23)

if(CMAKE_CXX_COMPILER_ID MATCHES "AppleClang|Clang|GNU")
    target_compile_options(framegen-tests PRIVATE -Wall -Wextra -Wpedantic)
endif()

add_test(NAME framegen-tests COMMAND framegen-tests)

add_executable(framegen-c-api-tests
    c_api_tests.cpp
)
target_link_libraries(framegen-c-api-tests PRIVATE framegen::core)
target_include_directories(framegen-c-api-tests PRIVATE
    ${PROJECT_SOURCE_DIR}/src/core
)
target_compile_features(framegen-c-api-tests PRIVATE cxx_std_23)
if(CMAKE_CXX_COMPILER_ID MATCHES "AppleClang|Clang|GNU")
    target_compile_options(framegen-c-api-tests PRIVATE -Wall -Wextra -Wpedantic)
endif()
add_test(NAME framegen-c-api-tests COMMAND framegen-c-api-tests)

add_executable(framegen-c-api-header-smoke
    c_api_header_smoke.c
)
target_link_libraries(framegen-c-api-header-smoke PRIVATE framegen::core)
set_target_properties(framegen-c-api-header-smoke PROPERTIES
    LINKER_LANGUAGE CXX
)
add_test(NAME framegen-c-api-header-smoke COMMAND framegen-c-api-header-smoke)

if(TARGET framegen::metal)
    add_executable(framegen-metal-c-api-tests
        metal_c_api_tests.mm
    )
    target_link_libraries(framegen-metal-c-api-tests PRIVATE framegen::metal)
    target_compile_features(framegen-metal-c-api-tests PRIVATE cxx_std_23)
    target_compile_options(framegen-metal-c-api-tests PRIVATE
        -fobjc-arc -Wall -Wextra -Wpedantic
    )
    set_target_properties(framegen-metal-c-api-tests PROPERTIES
        OBJCXX_STANDARD 23
        OBJCXX_STANDARD_REQUIRED YES
        OBJCXX_EXTENSIONS NO
    )
    add_test(NAME framegen-metal-c-api-tests COMMAND framegen-metal-c-api-tests)
endif()
