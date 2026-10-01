cmake_minimum_required(VERSION 3.25)

# For Ninja builds, resolve the active macOS SDK before project() so compiler
# detection and every subproject use the same SDK. `xcrun --sdk macosx` selects
# the newest macOS SDK exposed by the active Xcode/Command Line Tools install.
# A caller-provided CMAKE_OSX_SYSROOT remains authoritative.
if(CMAKE_HOST_APPLE AND NOT DEFINED CMAKE_OSX_SYSROOT)
  find_program(FRAMEGEN_XCRUN_EXECUTABLE NAMES xcrun)
  if(FRAMEGEN_XCRUN_EXECUTABLE)
    execute_process(
      COMMAND "${FRAMEGEN_XCRUN_EXECUTABLE}" --sdk macosx --show-sdk-path
      RESULT_VARIABLE _framegen_xcrun_result
      OUTPUT_VARIABLE _framegen_macos_sdk
      ERROR_QUIET
      OUTPUT_STRIP_TRAILING_WHITESPACE
    )
    if(_framegen_xcrun_result EQUAL 0 AND IS_DIRECTORY "${_framegen_macos_sdk}")
      set(CMAKE_OSX_SYSROOT "${_framegen_macos_sdk}" CACHE PATH
          "Newest macOS SDK from the active Apple developer toolchain")
    endif()
  endif()
endif()

project(FrameGen VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(FRAMEGEN_BUILD_LIBRARY "Build the renderer-independent frame-generation library" ON)
option(FRAMEGEN_BUILD_TESTS "Build and register the test suite" ON)
option(FRAMEGEN_BUILD_TOOLS "Build optional benchmark tools" OFF)

if(APPLE)
  set(_framegen_metal_default ON)
  set(_framegen_test_metal_default ON)
else()
  set(_framegen_metal_default OFF)
  set(_framegen_test_metal_default OFF)
endif()
option(FRAMEGEN_BUILD_METAL "Build the optional Metal backend" ${_framegen_metal_default})
option(FRAMEGEN_BUILD_TEST_METAL "Build the macOS Metal test host" ${_framegen_test_metal_default})

if(FRAMEGEN_BUILD_METAL)
  if(NOT FRAMEGEN_BUILD_LIBRARY)
    message(FATAL_ERROR "FRAMEGEN_BUILD_METAL requires FRAMEGEN_BUILD_LIBRARY=ON.")
  endif()
  if(NOT APPLE)
    message(FATAL_ERROR "FRAMEGEN_BUILD_METAL requires a macOS host.")
  endif()
  # Enable Apple framework interop at the project root so CMake initializes
  # Objective-C++ compile rules before generating backend targets.
  enable_language(OBJCXX)
endif()

function(framegen_add_required_subdirectory option_name relative_path description)
  if(${option_name})
    set(_path "${CMAKE_CURRENT_SOURCE_DIR}/${relative_path}")
    if(NOT EXISTS "${_path}/CMakeLists.txt")
      message(FATAL_ERROR
        "${option_name}=ON requires ${relative_path}/CMakeLists.txt (${description}).")
    endif()
    add_subdirectory("${relative_path}")
  endif()
endfunction()

if(FRAMEGEN_BUILD_LIBRARY)
  framegen_add_required_subdirectory(FRAMEGEN_BUILD_LIBRARY src "the core library")
endif()

if(FRAMEGEN_BUILD_TESTS)
  enable_testing()
  framegen_add_required_subdirectory(FRAMEGEN_BUILD_TESTS tests "the test suite")
endif()

if(FRAMEGEN_BUILD_TEST_METAL)
  if(NOT APPLE)
    message(FATAL_ERROR "FRAMEGEN_BUILD_TEST_METAL requires a macOS host.")
  endif()
  if(NOT FRAMEGEN_BUILD_LIBRARY OR NOT FRAMEGEN_BUILD_METAL)
    message(FATAL_ERROR "FRAMEGEN_BUILD_TEST_METAL requires FRAMEGEN_BUILD_LIBRARY=ON and FRAMEGEN_BUILD_METAL=ON.")
  endif()
  framegen_add_required_subdirectory(FRAMEGEN_BUILD_TEST_METAL adapters/test-metal "the Metal test host")
endif()

if(FRAMEGEN_BUILD_TOOLS)
  framegen_add_required_subdirectory(FRAMEGEN_BUILD_TOOLS tools/benchmark "the benchmark tool")
endif()
