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
