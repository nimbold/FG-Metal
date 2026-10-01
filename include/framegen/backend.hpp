#pragma once

#include "framegen/result.hpp"
#include "framegen/texture.hpp"

#include <cstdint>
#include <memory>
#include <optional>
#include <string_view>
#include <vector>

namespace framegen {

// A device-bound GPU synchronization point. Adapters may derive backend
// specific event/fence holders from this type; the core only checks ownership
// and forwards points to the backend. The shared object owns the native sync
// primitive for as long as it is needed by queued GPU work.
class GpuSyncPoint {
public:
    virtual ~GpuSyncPoint() = default;

    [[nodiscard]] virtual std::string_view backend_id() const noexcept = 0;
    [[nodiscard]] virtual std::uint64_t device_id() const noexcept = 0;
};

// A completion point describes output GPU work submitted by a backend.
// Waiting is explicitly opt-in because a renderer can compose this point into
// its own GPU queue without a CPU wait.
class GpuCompletion : public GpuSyncPoint {
public:
    ~GpuCompletion() override = default;

    [[nodiscard]] virtual bool is_complete() const noexcept = 0;
    [[nodiscard]] virtual Result<void> wait() = 0;

    // GPU execution time for diagnostics when the backend exposes hardware
    // timestamps. This excludes CPU submission and host-side readback. A
    // backend without trustworthy timestamps returns std::nullopt.
    [[nodiscard]] virtual std::optional<std::uint64_t>
    gpu_execution_time_ns() const noexcept {
        return std::nullopt;
    }
};

struct GeneratedFrame {
    Texture texture;
    std::shared_ptr<GpuCompletion> completion;
};

// All timestamps are signed nanoseconds in the producer-defined clock domain.
// A clock_domain value identifies both the clock and timestamp epoch; it must
// be non-zero and identical for both frames in one submission. sequence is a
// monotonically increasing source-frame index within an uninterrupted stream
// (it may skip when source frames are dropped). When a stream seeks or resets,
// set FrameSubmission::reset_history and assign a new domain if its clock epoch
// also changed.
struct FrameTiming {
    std::uint64_t sequence{};
    std::int64_t timestamp_ns{};
    std::uint64_t clock_domain{};
};

struct FrameInput {
    Texture texture;
    FrameTiming timing;
};

struct FrameSubmission {
    FrameInput previous;
    FrameInput current;
    // Normalized position between previous (0) and current (1).
    float interpolation{0.5F};
    // Set when the backend must discard temporal history before processing
    // this pair, for example after a scene cut, stream seek, or device reset.
    bool reset_history{};
    // GPU-native dependencies for outstanding writes to either input texture.
    // If absent, the caller asserts both textures are ready for GPU reads.
    // Adapters encode queue waits for supplied points; the core never waits on
    // them or reads image data back to CPU.
    std::vector<std::shared_ptr<const GpuSyncPoint>> gpu_dependencies;
};

// Backend SPI implemented by Metal or another renderer adapter. submit()
// must enqueue interpolation work without reading pixels to CPU memory, keep
// both input resources and dependencies alive until GPU use completes, honor
// all GPU dependencies before reading the input textures, and return a distinct
// output texture plus a completion handle. The caller must ensure producer
// writes are complete before backend reads: either serialize producer and
// backend work, or include GPU dependencies for every outstanding input write.
// Omitting a dependency asserts that the corresponding texture is already
// ready for GPU reads. If reset_history is set, prior temporal state must be discarded.
// The output may still be in flight; consumers compose the completion point
// into their GPU queue or explicitly wait when CPU blocking is acceptable.
class FrameGenerationBackend {
public:
    virtual ~FrameGenerationBackend() = default;

    // Stable backend identifier; it must match the identifier on its textures
    // and synchronization points for the lifetime of this backend.
    [[nodiscard]] virtual std::string_view backend_id() const noexcept = 0;
    [[nodiscard]] virtual std::uint64_t device_id() const noexcept = 0;

    // interpolation is the normalized time between previous (0) and current
    // (1); interpolated outputs usually use a value strictly between them.
    [[nodiscard]] virtual Result<GeneratedFrame> submit(
        const FrameSubmission& submission) = 0;
};

} // namespace framegen
