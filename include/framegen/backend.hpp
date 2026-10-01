#pragma once

#include "framegen/result.hpp"
#include "framegen/texture.hpp"

#include <array>
#include <cstdint>
#include <limits>
#include <memory>
#include <optional>
#include <string_view>
#include <vector>

namespace framegen {

inline constexpr std::int64_t unknown_timestamp_ns =
    std::numeric_limits<std::int64_t>::min();

// A device-bound GPU synchronization point. Adapters may derive backend
// specific event/fence holders from this type; the core only checks ownership
// and forwards points to the backend. The shared object owns the native sync
// primitive for as long as it is needed by queued GPU work.
class GpuSyncPoint {
public:
    virtual ~GpuSyncPoint() = default;

    [[nodiscard]] virtual std::string_view backend_id() const noexcept = 0;
    [[nodiscard]] virtual std::uint64_t device_id() const noexcept = 0;

    // Thread-safe, nonblocking signal query. nullopt means the backend cannot
    // query this point; asynchronous consumer use then cannot be retired
    // automatically.
    [[nodiscard]] virtual std::optional<bool> is_signaled() const noexcept {
        return std::nullopt;
    }
};

enum class GpuCompletionStatus : std::uint8_t { pending, complete, failed };

// A completion point describes output GPU work submitted by a backend.
// Waiting is explicitly opt-in because a renderer can compose this point into
// its own GPU queue without a CPU wait.
class GpuCompletion : public GpuSyncPoint {
public:
    ~GpuCompletion() override = default;

    // Nonblocking completion query. Implementations must make this safe for
    // concurrent polling; wait() is the explicit blocking operation.
    [[nodiscard]] virtual bool is_complete() const noexcept = 0;
    [[nodiscard]] virtual Result<void> wait() = 0;

    // Nonblocking query that can distinguish failure from successful
    // completion. Backends with no separate failure state may use the default.
    [[nodiscard]] virtual GpuCompletionStatus poll_status() const noexcept {
        return is_complete() ? GpuCompletionStatus::complete
                             : GpuCompletionStatus::pending;
    }

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
    // C++ callers keep texture alive through consumer use. The C host API
    // couples this lifetime to its ticket and consumer-completion report.
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
    std::int64_t duration_ns{};
    // Diagnostic only; use unknown_timestamp_ns when unavailable.
    std::int64_t render_completion_timestamp_ns{};
    std::int64_t desired_presentation_timestamp_ns{unknown_timestamp_ns};
};

enum class TransferFunction : std::uint8_t { unknown, linear, srgb, pq, hlg };
enum class DynamicRange : std::uint8_t { unknown, sdr, hdr };

struct FrameColorMetadata {
    TransferFunction transfer_function{TransferFunction::unknown};
    DynamicRange dynamic_range{DynamicRange::unknown};
};

struct FrameAuxiliaryInputs {
    Texture motion_vectors;
    Texture depth;
    Texture ui_texture;
    FrameColorMetadata ui_color_metadata;
    Texture ui_mask;
    Texture reactive_mask;
    std::optional<std::array<float, 16>> camera_to_world;
    std::optional<std::array<float, 16>> projection;
    std::optional<std::array<float, 2>> jitter_xy;
    std::optional<float> exposure;
    std::uint32_t motion_vector_encoding{};
    std::uint32_t motion_vector_units{};
    std::uint32_t motion_vector_direction{};
    std::uint32_t depth_encoding{};
};

// HUD handling is an explicit host/backend contract. In explicit_ui_plane
// mode, FrameInput::texture is scene-only and ui_texture is composited after
// scene interpolation. automatic_protection is for already-composited source
// frames and uses a conservative, temporally stabilized confidence mask.
enum class HudMode : std::uint8_t {
    no_hud_knowledge,
    explicit_ui_plane,
    automatic_protection,
};

enum class UiTemporalSource : std::uint8_t {
    previous_frame,
    current_frame,
    nearest_presentation,
};

enum class HudDebugVisualization : std::uint8_t {
    disabled,
    raw_mask,
    stabilized_mask,
    protected_regions,
    interpolation_confidence,
    final_composite,
};

struct HudOptions {
    HudMode mode{HudMode::no_hud_knowledge};
    UiTemporalSource ui_source{UiTemporalSource::nearest_presentation};
    HudDebugVisualization debug_visualization{HudDebugVisualization::disabled};
};

// Backends may keep temporal resources here to isolate separate generators
// that share one device/backend instance. A FrameGenerator creates one state
// for its lifetime and forwards it with every submission.
class BackendStreamState {
public:
    virtual ~BackendStreamState() = default;
};

struct FrameInput {
    Texture texture;
    FrameTiming timing;
    FrameAuxiliaryInputs optional_inputs;
    // Color metadata not represented by a native texture descriptor, such as
    // the transfer function and SDR/HDR range.
    FrameColorMetadata color_metadata;
};

struct FrameSubmission {
    FrameInput previous;
    FrameInput current;
    // Normalized position between previous (0) and current (1).
    float interpolation{0.5F};
    // Set when the backend must discard temporal history before processing
    // this pair, for example after a scene cut, stream seek, or device reset.
    // Source sequence and timestamp must still increase within this pair;
    // reset_history permits discontinuity from the preceding submitted pair.
    bool reset_history{};
    // GPU-native dependencies for outstanding writes to either input texture.
    // If absent, the caller asserts both textures are ready for GPU reads.
    // Adapters encode queue waits for supplied points; the core never waits on
    // them or reads image data back to CPU.
    std::vector<std::shared_ptr<const GpuSyncPoint>> gpu_dependencies;
    // Sample time bracketed by the two source timestamps; determines the
    // interpolation fraction.
    std::int64_t interpolation_timestamp_ns{unknown_timestamp_ns};
    // Renderer scheduling target and deadline, independent of sample time.
    std::int64_t desired_presentation_timestamp_ns{unknown_timestamp_ns};
    std::int64_t presentation_deadline_ns{};
    HudOptions hud_options;
    // Assigned by FrameGenerator; direct backend callers may provide their own
    // backend-created state when they need temporal continuity.
    std::shared_ptr<BackendStreamState> backend_stream_state;
};

// Backend SPI implemented by Metal or another renderer adapter. submit()
// must enqueue work promptly without waiting for GPU completion or reading
// pixels to CPU memory, keep both input resources and dependencies alive until
// GPU use completes, honor all dependencies before reading the input textures,
// and return a distinct output texture plus a completion handle. The caller
// must ensure producer writes are complete before backend reads: either
// serialize producer and backend work, or include GPU dependencies for every
// outstanding input write.
// Omitting a dependency asserts that the corresponding texture is already
// ready for GPU reads. If reset_history is set, prior temporal state must be
// discarded. Auxiliary textures sampled by queued work must be retained until
// GPU completion, just like the two primary color textures.
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

    // Optional per-generator temporal state. Stateless backends can retain the
    // default implementation; stateful backends should return a fresh object
    // so generators sharing this backend cannot exchange history.
    [[nodiscard]] virtual std::shared_ptr<BackendStreamState>
    create_stream_state() { return {}; }
};

} // namespace framegen
