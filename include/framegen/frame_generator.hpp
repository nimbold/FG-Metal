#pragma once

#include "framegen/backend.hpp"

#include <cstdint>
#include <memory>
#include <string_view>
#include <utility>

namespace framegen {

// Small renderer-neutral validation/orchestration layer around a GPU backend.
// The backend owns allocation, command encoding, and synchronization details.
class FrameGenerator final {
public:
    [[nodiscard]] static Result<FrameGenerator> create(
        std::shared_ptr<FrameGenerationBackend> backend);

    [[nodiscard]] Result<GeneratedFrame> submit(const FrameSubmission& submission);

    [[nodiscard]] std::string_view backend_id() const noexcept;
    [[nodiscard]] std::uint64_t device_id() const noexcept;

private:
    explicit FrameGenerator(std::shared_ptr<FrameGenerationBackend> backend)
        : backend_(std::move(backend)) {}

    std::shared_ptr<FrameGenerationBackend> backend_;
};

} // namespace framegen
