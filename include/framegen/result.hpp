#pragma once

#include <expected>
#include <string>

namespace framegen {

enum class ErrorCode {
    invalid_argument,
    incompatible_resource,
    unsupported_format,
    allocation_failure,
    backend_failure,
    internal_error,
};

struct Error {
    ErrorCode code{ErrorCode::internal_error};
    std::string message;
};

template <class T>
using Result = std::expected<T, Error>;

} // namespace framegen
