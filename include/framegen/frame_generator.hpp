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
    // Copies share the backend but start an independent temporal history;
    // moves transfer the existing history.
    FrameGenerator(const FrameGenerator& other);
    FrameGenerator& operator=(const FrameGenerator& other);
    FrameGenerator(FrameGenerator&&) noexcept = default;
    FrameGenerator& operator=(FrameGenerator&&) noexcept = default;

    [[nodiscard]] static Result<FrameGenerator> create(
        std::shared_ptr<FrameGenerationBackend> backend);

    [[nodiscard]] Result<GeneratedFrame> submit(const FrameSubmission& submission);

    [[nodiscard]] std::string_view backend_id() const noexcept;
    [[nodiscard]] std::uint64_t device_id() const noexcept;

private:
    explicit FrameGenerator(std::shared_ptr<FrameGenerationBackend> backend,
                            std::shared_ptr<BackendStreamState> stream_state)
        : backend_(std::move(backend)), stream_state_(std::move(stream_state)) {}

    std::shared_ptr<FrameGenerationBackend> backend_;
    std::shared_ptr<BackendStreamState> stream_state_;
};

} // namespace framegen
