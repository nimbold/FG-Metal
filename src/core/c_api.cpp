#include "framegen/api.h"

#include "backend_registry.hpp"
#include "framegen/frame_generator.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <limits>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

using namespace framegen;

constexpr framegen_capability_flags_t kOptionalInputMask =
    FRAMEGEN_CAP_MOTION_VECTORS | FRAMEGEN_CAP_DEPTH | FRAMEGEN_CAP_UI_PLANE |
    FRAMEGEN_CAP_UI_MASK | FRAMEGEN_CAP_REACTIVE_MASK |
    FRAMEGEN_CAP_CAMERA_MATRICES | FRAMEGEN_CAP_JITTER | FRAMEGEN_CAP_EXPOSURE;
constexpr framegen_capability_flags_t kKnownCapabilityMask =
    FRAMEGEN_CAP_COLOR_ONLY | kOptionalInputMask | FRAMEGEN_CAP_HDR |
    FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME |
    FRAMEGEN_CAP_AUTOMATIC_HUD_PROTECTION |
    FRAMEGEN_CAP_HUD_DEBUG_VISUALIZATION |
    FRAMEGEN_CAP_TEMPORAL_QUALITY_CONTROLLER;
constexpr framegen_capability_flags_t kKnownInputMask = kOptionalInputMask;

thread_local std::array<char, 1024> g_last_error{};
thread_local std::size_t g_last_error_length{};

framegen_status_t fail(framegen_status_t status, std::string_view message) noexcept {
    g_last_error_length = std::min(message.size(), g_last_error.size() - 1);
    if (g_last_error_length != 0) {
        std::memcpy(g_last_error.data(), message.data(), g_last_error_length);
    }
    g_last_error[g_last_error_length] = '\0';
    return status;
}

framegen_status_t succeed() noexcept {
    g_last_error_length = 0;
    g_last_error[0] = '\0';
    return FRAMEGEN_STATUS_OK;
}

template <typename T>
bool valid_input_struct(const T* value) {
    return value != nullptr && value->struct_size >= sizeof(T) &&
           value->struct_version == FRAMEGEN_ABI_VERSION;
}

template <typename T>
bool valid_output_struct(const T* value) {
    return value != nullptr && value->struct_size >= sizeof(T) &&
           value->struct_version == FRAMEGEN_ABI_VERSION;
}

template <typename F>
framegen_status_t guarded(F&& fn) noexcept {
    try {
        return fn();
    } catch (const std::bad_alloc&) {
        return fail(FRAMEGEN_STATUS_INTERNAL_ERROR, "allocation failed in Framegen API");
    } catch (const std::exception& exception) {
        return fail(FRAMEGEN_STATUS_INTERNAL_ERROR, exception.what());
    } catch (...) {
        return fail(FRAMEGEN_STATUS_INTERNAL_ERROR, "unknown exception in Framegen API");
    }
}

template <typename T>
void initialize_output(T& value) {
    const std::uint32_t size = value.struct_size;
    std::memset(&value, 0, std::min<std::size_t>(size, sizeof(T)));
    value.struct_size = size;
    value.struct_version = FRAMEGEN_ABI_VERSION;
}

bool has_paired_callbacks(framegen_handle_retain_fn retain,
                          framegen_handle_release_fn release) {
    return (retain == nullptr) == (release == nullptr);
}

class NativeLease final {
public:
    NativeLease(void* handle, void* user_data,
                framegen_handle_retain_fn retain,
                framegen_handle_release_fn release)
        : handle_(handle), user_data_(user_data), retain_(retain), release_(release) {
        if (retain_ != nullptr) {
            retain_(user_data_, handle_);
        }
    }

    ~NativeLease() {
        if (release_ != nullptr) {
            try {
                release_(user_data_, handle_);
            } catch (...) {
                // Destruction can run on a worker thread and must not throw.
            }
        }
    }

    NativeLease(const NativeLease&) = delete;
    NativeLease& operator=(const NativeLease&) = delete;

private:
    void* handle_{};
    void* user_data_{};
    framegen_handle_retain_fn retain_{};
    framegen_handle_release_fn release_{};
};

std::optional<PixelFormat> to_pixel_format(std::uint32_t format) {
    switch (format) {
    case FRAMEGEN_PIXEL_FORMAT_R8_UNORM: return PixelFormat::r8_unorm;
    case FRAMEGEN_PIXEL_FORMAT_RG8_UNORM: return PixelFormat::rg8_unorm;
    case FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM: return PixelFormat::rgba8_unorm;
    case FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM_SRGB: return PixelFormat::rgba8_unorm_srgb;
    case FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM: return PixelFormat::bgra8_unorm;
    case FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM_SRGB: return PixelFormat::bgra8_unorm_srgb;
    case FRAMEGEN_PIXEL_FORMAT_RGB10A2_UNORM: return PixelFormat::rgb10a2_unorm;
    case FRAMEGEN_PIXEL_FORMAT_BGR10A2_UNORM: return PixelFormat::bgr10a2_unorm;
    case FRAMEGEN_PIXEL_FORMAT_RG11B10_FLOAT: return PixelFormat::rg11b10_float;
    case FRAMEGEN_PIXEL_FORMAT_RGBA16_FLOAT: return PixelFormat::rgba16_float;
    case FRAMEGEN_PIXEL_FORMAT_RGBA32_FLOAT: return PixelFormat::rgba32_float;
    case FRAMEGEN_PIXEL_FORMAT_R16_FLOAT: return PixelFormat::r16_float;
    case FRAMEGEN_PIXEL_FORMAT_RG16_FLOAT: return PixelFormat::rg16_float;
    case FRAMEGEN_PIXEL_FORMAT_RG32_FLOAT: return PixelFormat::rg32_float;
    case FRAMEGEN_PIXEL_FORMAT_R32_FLOAT: return PixelFormat::r32_float;
    case FRAMEGEN_PIXEL_FORMAT_D16_UNORM: return PixelFormat::d16_unorm;
    case FRAMEGEN_PIXEL_FORMAT_D32_FLOAT: return PixelFormat::d32_float;
    case FRAMEGEN_PIXEL_FORMAT_D24_UNORM_S8_UINT: return PixelFormat::d24_unorm_s8_uint;
    default: return std::nullopt;
    }
}

std::optional<ColorSpace> to_color_space(const framegen_image_t& image) {
    switch (image.color_space) {
    case FRAMEGEN_COLOR_SPACE_SRGB:
        if (image.transfer_function == FRAMEGEN_TRANSFER_LINEAR) {
            return ColorSpace::linear_srgb;
        }
        return ColorSpace::srgb;
    case FRAMEGEN_COLOR_SPACE_DISPLAY_P3: return ColorSpace::display_p3;
    case FRAMEGEN_COLOR_SPACE_REC2020:
        if (image.transfer_function == FRAMEGEN_TRANSFER_PQ) {
            return ColorSpace::rec2020_pq;
        }
        if (image.transfer_function == FRAMEGEN_TRANSFER_HLG) {
            return ColorSpace::rec2020_hlg;
        }
        return ColorSpace::rec2020;
    default: return std::nullopt;
    }
}

std::optional<AlphaMode> to_alpha_mode(std::uint32_t mode) {
    switch (mode) {
    case FRAMEGEN_ALPHA_OPAQUE: return AlphaMode::opaque;
    case FRAMEGEN_ALPHA_STRAIGHT: return AlphaMode::straight;
    case FRAMEGEN_ALPHA_PREMULTIPLIED: return AlphaMode::premultiplied;
    default: return std::nullopt;
    }
}

TransferFunction to_transfer_function(std::uint32_t transfer) {
    switch (transfer) {
    case FRAMEGEN_TRANSFER_LINEAR: return TransferFunction::linear;
    case FRAMEGEN_TRANSFER_SRGB: return TransferFunction::srgb;
    case FRAMEGEN_TRANSFER_PQ: return TransferFunction::pq;
    case FRAMEGEN_TRANSFER_HLG: return TransferFunction::hlg;
    default: return TransferFunction::unknown;
    }
}

DynamicRange to_dynamic_range(std::uint32_t range) {
    switch (range) {
    case FRAMEGEN_DYNAMIC_RANGE_SDR: return DynamicRange::sdr;
    case FRAMEGEN_DYNAMIC_RANGE_HDR: return DynamicRange::hdr;
    default: return DynamicRange::unknown;
    }
}

std::uint64_t timestamp_distance(std::int64_t lower, std::int64_t upper) {
    // Call only after checking upper > lower. Unsigned subtraction represents
    // the complete signed timestamp range without signed overflow.
    return static_cast<std::uint64_t>(upper) - static_cast<std::uint64_t>(lower);
}

bool is_known_timestamp(std::int64_t timestamp) noexcept {
    return timestamp != FRAMEGEN_TIMESTAMP_UNKNOWN;
}

bool valid_image_metadata(const framegen_image_t& image,
                          const framegen_backend_info_t& backend_info,
                          bool color_image) {
    if (!valid_input_struct(&image) || !valid_input_struct(&image.resource) ||
        image.width == 0 || image.height == 0 ||
        image.width > backend_info.max_width || image.height > backend_info.max_height ||
        !to_pixel_format(image.pixel_format).has_value() ||
        image.resource.backend_type != backend_info.backend_type ||
        image.resource.resource_type != FRAMEGEN_RESOURCE_TEXTURE_2D ||
        image.resource.native_handle == nullptr || image.resource.device_id == 0 ||
        !has_paired_callbacks(image.resource.retain, image.resource.release)) {
        return false;
    }
    if (!color_image) {
        const bool known_color_space = image.color_space <= FRAMEGEN_COLOR_SPACE_REC2020;
        const bool known_transfer = image.transfer_function <= FRAMEGEN_TRANSFER_HLG;
        const bool known_range = image.dynamic_range <= FRAMEGEN_DYNAMIC_RANGE_HDR;
        const bool known_alpha = image.alpha_mode <= FRAMEGEN_ALPHA_PREMULTIPLIED;
        return known_color_space && known_transfer && known_range && known_alpha;
    }
    return to_color_space(image).has_value() &&
           image.transfer_function != FRAMEGEN_TRANSFER_UNKNOWN &&
           (image.dynamic_range == FRAMEGEN_DYNAMIC_RANGE_SDR ||
            image.dynamic_range == FRAMEGEN_DYNAMIC_RANGE_HDR) &&
           to_alpha_mode(image.alpha_mode).has_value();
}

bool backend_supports_format(const framegen_backend_info_t& info,
                             std::uint32_t format) {
    return format < 64 &&
           (info.supported_pixel_formats & FRAMEGEN_PIXEL_FORMAT_BIT(format)) != 0;
}

bool valid_auxiliary_format(framegen_capability_flags_t role,
                            std::uint32_t format) {
    if (role == FRAMEGEN_INPUT_MOTION_VECTORS) {
        return format == FRAMEGEN_PIXEL_FORMAT_RG8_UNORM ||
               format == FRAMEGEN_PIXEL_FORMAT_RG16_FLOAT ||
               format == FRAMEGEN_PIXEL_FORMAT_RG32_FLOAT;
    }
    if (role == FRAMEGEN_INPUT_DEPTH) {
        return format == FRAMEGEN_PIXEL_FORMAT_R16_FLOAT ||
               format == FRAMEGEN_PIXEL_FORMAT_R32_FLOAT ||
               format == FRAMEGEN_PIXEL_FORMAT_D16_UNORM ||
               format == FRAMEGEN_PIXEL_FORMAT_D32_FLOAT ||
               format == FRAMEGEN_PIXEL_FORMAT_D24_UNORM_S8_UINT;
    }
    if (role == FRAMEGEN_INPUT_UI_PLANE) {
        return format == FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM ||
               format == FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM_SRGB ||
               format == FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM ||
               format == FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM_SRGB ||
               format == FRAMEGEN_PIXEL_FORMAT_RGB10A2_UNORM ||
               format == FRAMEGEN_PIXEL_FORMAT_BGR10A2_UNORM ||
               format == FRAMEGEN_PIXEL_FORMAT_RG11B10_FLOAT ||
               format == FRAMEGEN_PIXEL_FORMAT_RGBA16_FLOAT ||
               format == FRAMEGEN_PIXEL_FORMAT_RGBA32_FLOAT;
    }
    if (role == FRAMEGEN_INPUT_UI_MASK || role == FRAMEGEN_INPUT_REACTIVE_MASK) {
        return format == FRAMEGEN_PIXEL_FORMAT_R8_UNORM ||
               format == FRAMEGEN_PIXEL_FORMAT_R16_FLOAT ||
               format == FRAMEGEN_PIXEL_FORMAT_R32_FLOAT;
    }
    return false;
}

bool same_image_metadata(const framegen_image_t& a, const framegen_image_t& b) {
    return a.width == b.width && a.height == b.height &&
           a.pixel_format == b.pixel_format && a.color_space == b.color_space &&
           a.transfer_function == b.transfer_function &&
           a.dynamic_range == b.dynamic_range && a.alpha_mode == b.alpha_mode;
}

struct FrameRecord {
    framegen_source_frame_t source{};
    FrameInput input;
    std::shared_ptr<const GpuSyncPoint> render_dependency;
    std::vector<std::shared_ptr<NativeLease>> leases;
    std::uint64_t history_generation{};
};

struct ContextImpl;

struct TicketImpl {
    std::weak_ptr<ContextImpl> context;
    std::uint64_t id{};
    std::uint64_t previous_frame_id{};
    std::uint64_t current_frame_id{};
    std::uint64_t history_generation{};
    std::uint64_t clock_domain{};
    std::int64_t interpolation_ns{};
    std::int64_t desired_presentation_ns{};
    std::int64_t deadline_ns{};
    std::shared_ptr<FrameRecord> previous_source;
    std::shared_ptr<FrameRecord> current_source;
    GeneratedFrame generated;
    std::uint32_t status{FRAMEGEN_TICKET_PENDING};
    framegen_status_t error_status{FRAMEGEN_STATUS_OK};
    bool gpu_completed{};
    bool gpu_counted_in_flight{true};
    bool client_released{};
    bool presentation_notified{};
    bool consumer_finished{true};
    std::shared_ptr<NativeLease> consumer_lease;
    std::shared_ptr<const GpuSyncPoint> consumer_completion;
};

struct ContextImpl : std::enable_shared_from_this<ContextImpl> {
    // Serializes provider/backend operations so a new request cannot enter the
    // backend between history commit and invalidation, and adapter imports,
    // exports, and callbacks are not raced by backend lifecycle hooks.
    std::mutex backend_operation_mutex;
    std::mutex mutex;
    std::shared_ptr<detail::BackendProvider> provider;
    detail::BackendInstance backend;
    framegen_backend_info_t backend_info{};
    std::unique_ptr<FrameGenerator> generator;
    std::shared_ptr<NativeLease> device_lease;
    framegen_capability_result_t capabilities{};
    framegen_context_config_t config{};
    framegen_hud_options_t hud_options{};
    framegen_temporal_quality_options_t temporal_quality_options{};
    std::deque<std::shared_ptr<FrameRecord>> frames;
    std::unordered_map<std::uint64_t, std::shared_ptr<TicketImpl>> tickets;
    framegen_statistics_t statistics{};
    std::uint64_t device_id{};
    std::uint64_t history_generation{1};
    std::uint64_t last_source_frame_id{};
    std::uint64_t next_ticket_id{1};
    std::uint64_t last_backend_history_generation{};
    std::uint64_t stream_id{};
    std::uint64_t clock_domain{};
    // Emergency lifetime hold used only if the retirement worker cannot queue
    // a state (for example, allocation failure while destroying a context).
    std::shared_ptr<ContextImpl> emergency_retirement_hold;
    std::uint32_t max_in_flight{2};
    std::uint32_t history_capacity{4};
    std::uint32_t active_in_flight{};
    bool configured{};
    bool device_lost{};
    bool destroyed{};
};

void refresh_ticket_locked(ContextImpl& state, TicketImpl& ticket) {
    if (!ticket.gpu_completed && ticket.generated.completion) {
        const auto completion = ticket.generated.completion->poll_status();
        if (completion == GpuCompletionStatus::complete ||
            completion == GpuCompletionStatus::failed) {
            ticket.gpu_completed = true;
            if (ticket.gpu_counted_in_flight) {
                ticket.gpu_counted_in_flight = false;
                if (state.active_in_flight != 0) {
                    --state.active_in_flight;
                }
            }
            if (completion == GpuCompletionStatus::failed &&
                ticket.status == FRAMEGEN_TICKET_PENDING) {
                ticket.status = FRAMEGEN_TICKET_FAILED;
                ticket.error_status = FRAMEGEN_STATUS_BACKEND_ERROR;
                ++state.statistics.failed_requests;
            }
            if (completion == GpuCompletionStatus::complete) {
                if (auto elapsed = ticket.generated.completion->gpu_execution_time_ns()) {
                    state.statistics.gpu_execution_time_ns += *elapsed;
                    ++state.statistics.gpu_execution_time_samples;
                }
            }
        }
    }
    if (!ticket.consumer_finished && ticket.consumer_completion) {
        const auto signaled = ticket.consumer_completion->is_signaled();
        ticket.consumer_finished = signaled.value_or(false);
    }
}

framegen_status_t validate_generated_presentation_locked(
    ContextImpl& state, TicketImpl& ticket,
    std::int64_t presentation_timestamp_ns) {
    refresh_ticket_locked(state, ticket);
    if (ticket.status == FRAMEGEN_TICKET_PENDING ||
        ticket.status == FRAMEGEN_TICKET_READY) {
        if (presentation_timestamp_ns >= ticket.deadline_ns) {
            ticket.status = FRAMEGEN_TICKET_DEADLINE_MISSED;
            ++state.statistics.deadline_misses;
            return fail(FRAMEGEN_STATUS_INVALID_STATE,
                        "generated output cannot be presented at or after its deadline");
        }
        return FRAMEGEN_STATUS_OK;
    }
    if (ticket.status == FRAMEGEN_TICKET_DEADLINE_MISSED) {
        return fail(FRAMEGEN_STATUS_INVALID_STATE,
                    "a deadline-missed generated output cannot be presented");
    }
    if (ticket.status == FRAMEGEN_TICKET_INVALIDATED) {
        return fail(FRAMEGEN_STATUS_STALE_FRAME,
                    "an output from invalidated history cannot be presented");
    }
    if (ticket.status == FRAMEGEN_TICKET_FAILED) {
        return fail(FRAMEGEN_STATUS_INVALID_STATE,
                    "a failed generated frame cannot be presented");
    }
    return fail(FRAMEGEN_STATUS_INVALID_STATE,
                "ticket is no longer eligible for generated presentation");
}

void retire_released_tickets_locked(ContextImpl& state) {
    for (auto it = state.tickets.begin(); it != state.tickets.end();) {
        auto& ticket = *it->second;
        refresh_ticket_locked(state, ticket);
        if (ticket.client_released && ticket.presentation_notified &&
            ticket.gpu_completed && ticket.consumer_finished) {
            it = state.tickets.erase(it);
        } else {
            ++it;
        }
    }
}

class DeferredRetirement final {
public:
    static DeferredRetirement& instance() {
        // Intentionally process-lifetime: pending GPU work must not make a
        // static destructor block application shutdown.
        static auto* value = new DeferredRetirement();
        return *value;
    }

    void enqueue(const std::shared_ptr<ContextImpl>& state) noexcept {
        try {
            {
                std::scoped_lock lock(mutex_);
                const auto already_queued = std::any_of(
                    states_.begin(), states_.end(), [&](const auto& queued) {
                        return queued.get() == state.get();
                    });
                if (!already_queued) {
                    states_.push_back(state);
                }
            }
            condition_.notify_one();
        } catch (...) {
            try {
                std::scoped_lock lock(state->mutex);
                state->emergency_retirement_hold = state;
            } catch (...) {
                // Prefer a safe leak over releasing resources still in GPU use.
            }
        }
    }

private:
    DeferredRetirement() {
        std::thread([this] { run(); }).detach();
    }

    void run() {
        for (;;) {
            std::unique_lock queue_lock(mutex_);
            while (states_.empty()) {
                condition_.wait(queue_lock);
            }
            condition_.wait_for(queue_lock, std::chrono::milliseconds(5));
            for (auto it = states_.begin(); it != states_.end();) {
                const auto& state = *it;
                bool empty{};
                {
                    std::scoped_lock state_lock(state->mutex);
                    retire_released_tickets_locked(*state);
                    empty = std::none_of(
                        state->tickets.begin(), state->tickets.end(), [](const auto& entry) {
                            const auto& ticket = *entry.second;
                            return ticket.client_released && ticket.presentation_notified &&
                                   (!ticket.gpu_completed || !ticket.consumer_finished);
                        });
                }
                if (empty) {
                    it = states_.erase(it);
                } else {
                    ++it;
                }
            }
        }
    }

    std::mutex mutex_;
    std::condition_variable condition_;
    std::vector<std::shared_ptr<ContextImpl>> states_;
};

void enqueue_for_retirement(const std::shared_ptr<ContextImpl>& state) noexcept {
    try {
        DeferredRetirement::instance().enqueue(state);
    } catch (...) {
        try {
            std::scoped_lock lock(state->mutex);
            state->emergency_retirement_hold = state;
        } catch (...) {
            // Prefer a safe leak over releasing resources still in GPU use.
        }
    }
}

void set_backend_info(framegen_backend_info_t& out,
                      const framegen_backend_info_t& info) {
    const auto size = out.struct_size;
    initialize_output(out);
    std::memcpy(&out, &info, std::min<std::size_t>(size, sizeof(info)));
    out.struct_size = size;
    out.struct_version = FRAMEGEN_ABI_VERSION;
}

void fill_capability_result(const ContextImpl& context,
                            framegen_capability_result_t& out) {
    const auto size = out.struct_size;
    initialize_output(out);
    out.supported_capabilities = context.backend_info.supported_capabilities;
    out.backend_required_capabilities = context.backend_info.required_capabilities;
    out.backend_required_input_capabilities =
        context.backend_info.required_input_capabilities;
    out.negotiated_capabilities = context.configured
        ? context.capabilities.negotiated_capabilities : 0;
    out.unmet_capabilities = context.configured
        ? context.capabilities.unmet_capabilities
        : context.backend_info.required_capabilities |
              context.backend_info.required_input_capabilities;
    out.max_in_flight_requests = context.max_in_flight;
    out.struct_size = size;
}

std::shared_ptr<FrameRecord> find_frame(const ContextImpl& context,
                                        std::uint64_t frame_id) {
    const auto it = std::find_if(context.frames.begin(), context.frames.end(),
        [frame_id](const auto& frame) {
            return frame->source.source_frame_id == frame_id;
        });
    return it == context.frames.end() ? nullptr : *it;
}

framegen_status_t map_cpp_error(const Error& error) {
    switch (error.code) {
    case ErrorCode::invalid_argument: return FRAMEGEN_STATUS_INVALID_ARGUMENT;
    case ErrorCode::incompatible_resource: return FRAMEGEN_STATUS_INVALID_FRAME;
    case ErrorCode::unsupported_format: return FRAMEGEN_STATUS_UNSUPPORTED_RESOURCE;
    case ErrorCode::allocation_failure:
    case ErrorCode::backend_failure: return FRAMEGEN_STATUS_BACKEND_ERROR;
    case ErrorCode::internal_error: return FRAMEGEN_STATUS_INTERNAL_ERROR;
    }
    return FRAMEGEN_STATUS_INTERNAL_ERROR;
}

framegen_status_t check_context(const std::shared_ptr<ContextImpl>& context) {
    if (!context) {
        return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT, "context must not be null");
    }
    if (context->destroyed) {
        return fail(FRAMEGEN_STATUS_INVALID_STATE, "context has been destroyed");
    }
    if (context->device_lost) {
        return fail(FRAMEGEN_STATUS_DEVICE_LOST, "context device has been lost");
    }
    return FRAMEGEN_STATUS_OK;
}

std::shared_ptr<NativeLease> lease_handle(void* native_handle, void* user_data,
                                         framegen_handle_retain_fn retain,
                                         framegen_handle_release_fn release) {
    if (retain == nullptr || release == nullptr) {
        return {};
    }
    return std::make_shared<NativeLease>(native_handle, user_data, retain, release);
}

std::uint32_t expected_cpp_format(std::uint32_t format) {
    const auto converted = to_pixel_format(format);
    return converted ? static_cast<std::uint32_t>(*converted) : 0;
}

bool valid_exported_image(const framegen_image_t& image,
                          const framegen_backend_info_t& backend_info,
                          std::uint64_t device_id,
                          const TextureDescriptor& expected) {
    return valid_input_struct(&image) && valid_input_struct(&image.resource) &&
           image.resource.backend_type == backend_info.backend_type &&
           image.resource.resource_type == FRAMEGEN_RESOURCE_TEXTURE_2D &&
           image.resource.device_id == device_id &&
           image.resource.native_handle != nullptr &&
           image.resource.user_data == nullptr &&
           image.resource.retain == nullptr && image.resource.release == nullptr &&
           image.width == expected.width && image.height == expected.height &&
           expected_cpp_format(image.pixel_format) ==
               static_cast<std::uint32_t>(expected.format);
}

bool valid_exported_sync(const framegen_sync_point_t& sync,
                         const framegen_backend_info_t& backend_info,
                         std::uint64_t device_id,
                         GpuCompletionStatus completion_status) {
    if (!valid_input_struct(&sync) ||
        sync.backend_type != backend_info.backend_type ||
        sync.device_id != device_id ||
        sync.user_data != nullptr || sync.retain != nullptr || sync.release != nullptr) {
        return false;
    }
    if (sync.sync_type == FRAMEGEN_SYNC_NONE_READY) {
        return completion_status == GpuCompletionStatus::complete &&
               sync.native_handle == nullptr && sync.user_data == nullptr &&
               sync.value == 0 && sync.retain == nullptr && sync.release == nullptr;
    }
    return sync.native_handle != nullptr;
}

} // namespace

struct framegen_context {
    std::shared_ptr<ContextImpl> impl;
};

struct framegen_ticket {
    std::shared_ptr<TicketImpl> impl;
};

namespace framegen::detail {

void register_backend_provider(std::shared_ptr<BackendProvider> provider);

} // namespace framegen::detail

extern "C" {

framegen_status_t framegen_get_abi_info(framegen_abi_info_t* out_info) {
    return guarded([&] {
        if (!valid_output_struct(out_info)) {
            return fail(FRAMEGEN_STATUS_ABI_MISMATCH,
                        "ABI info output has an unsupported size or version");
        }
        initialize_output(*out_info);
        out_info->abi_version = FRAMEGEN_ABI_VERSION;
        out_info->stability = FRAMEGEN_ABI_PROVISIONAL;
        return succeed();
    });
}

framegen_status_t framegen_enumerate_backends(
    std::uint32_t capacity, std::uint32_t output_stride, void* backends,
    std::uint32_t* out_count) {
    return guarded([&] {
        if (out_count == nullptr || (capacity != 0 &&
            (backends == nullptr || output_stride < sizeof(framegen_backend_info_t) ||
            capacity > std::numeric_limits<std::size_t>::max() / output_stride))) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "backend enumeration requires a count and a sufficiently-strided output array");
        }
        const auto providers = detail::backend_providers();
        *out_count = static_cast<std::uint32_t>(providers.size());
        const auto count = std::min<std::size_t>(capacity, providers.size());
        for (std::size_t index = 0; index < count; ++index) {
            auto info = providers[index]->info();
            info.struct_size = sizeof(framegen_backend_info_t);
            info.struct_version = FRAMEGEN_ABI_VERSION;
            auto* destination = static_cast<std::byte*>(backends) + index * output_stride;
            std::memcpy(destination, &info, sizeof(info));
        }
        return succeed();
    });
}

framegen_status_t framegen_query_backend(const char* backend_id,
                                        framegen_backend_info_t* out_info) {
    return guarded([&] {
        if (backend_id == nullptr || backend_id[0] == '\0' ||
            !valid_output_struct(out_info)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "backend query requires an identifier and a valid output structure");
        }
        auto provider = detail::find_backend_provider(backend_id);
        if (!provider) {
            return fail(FRAMEGEN_STATUS_BACKEND_UNAVAILABLE,
                        "requested backend is not registered");
        }
        set_backend_info(*out_info, provider->info());
        return succeed();
    });
}

framegen_status_t framegen_context_create(const framegen_context_desc_t* desc,
                                          framegen_context_t** out_context) {
    return guarded([&] {
        if (out_context != nullptr) {
            *out_context = nullptr;
        }
        if (!valid_input_struct(desc) || out_context == nullptr ||
            desc->backend_id == nullptr || desc->backend_id[0] == '\0' ||
            !valid_input_struct(&desc->device) || desc->device.native_handle == nullptr ||
            !has_paired_callbacks(desc->device.retain, desc->device.release)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "context creation descriptor is invalid");
        }
        auto provider = detail::find_backend_provider(desc->backend_id);
        if (!provider) {
            return fail(FRAMEGEN_STATUS_BACKEND_UNAVAILABLE,
                        "requested backend is not registered");
        }
        const auto info = provider->info();
        if (info.available == 0) {
            return fail(FRAMEGEN_STATUS_BACKEND_UNAVAILABLE,
                        "requested backend is registered but unavailable");
        }
        if (desc->device.backend_type != info.backend_type ||
            desc->device.resource_type != FRAMEGEN_RESOURCE_DEVICE) {
            return fail(FRAMEGEN_STATUS_UNSUPPORTED_RESOURCE,
                        "device resource type does not match the selected backend");
        }
        auto instance = provider->create(desc->device);
        if (!instance) {
            return fail(map_cpp_error(instance.error()), instance.error().message);
        }
        if (!instance->backend || instance->backend->backend_id() != desc->backend_id ||
            instance->backend->device_id() == 0 || !instance->import_image ||
            !instance->import_sync || !instance->export_image || !instance->export_sync) {
            return fail(FRAMEGEN_STATUS_BACKEND_ERROR,
                        "backend provider returned an incomplete context");
        }
        auto generator = FrameGenerator::create(instance->backend);
        if (!generator) {
            return fail(map_cpp_error(generator.error()), generator.error().message);
        }
        // Start the process-lifetime retirement worker before the context can
        // accept GPU work, so destruction never has to create a thread.
        (void)DeferredRetirement::instance();
        auto state = std::make_shared<ContextImpl>();
        state->provider = std::move(provider);
        state->backend = std::move(*instance);
        state->backend_info = info;
        state->generator = std::make_unique<FrameGenerator>(std::move(*generator));
        state->device_id = state->backend.backend->device_id();
        state->max_in_flight = info.max_in_flight_requests == 0
            ? 2 : info.max_in_flight_requests;
        state->config.struct_size = sizeof(framegen_context_config_t);
        state->config.struct_version = FRAMEGEN_ABI_VERSION;
        state->config.max_in_flight_requests = state->max_in_flight;
        state->config.history_capacity = 4;
        state->device_lease = lease_handle(desc->device.native_handle,
                                           desc->device.user_data,
                                           desc->device.retain,
                                           desc->device.release);
        auto* handle = new framegen_context{std::move(state)};
        *out_context = handle;
        return succeed();
    });
}

void framegen_context_destroy(framegen_context_t* context) {
    try {
        if (context == nullptr) {
            return;
        }
        auto state = context->impl;
        bool defer_retirement{};
        if (state) {
            std::unique_lock backend_operation_lock(state->backend_operation_mutex);
            {
                std::scoped_lock lock(state->mutex);
                state->destroyed = true;
                state->configured = false;
                state->frames.clear();
                for (auto& [id, ticket] : state->tickets) {
                    (void)id;
                    ticket->client_released = true;
                    if (!ticket->presentation_notified) {
                        // Destroying the context abandons every output not
                        // reported as consumed. Async consumers must be
                        // notified before destruction.
                        ticket->presentation_notified = true;
                        ticket->consumer_finished = true;
                    }
                    ticket->status = FRAMEGEN_TICKET_INVALIDATED;
                    ticket->error_status = FRAMEGEN_STATUS_INVALID_STATE;
                }
                retire_released_tickets_locked(*state);
                defer_retirement = !state->tickets.empty();
            }
            backend_operation_lock.unlock();
            if (defer_retirement) {
                enqueue_for_retirement(state);
            }
        }
        delete context;
    } catch (...) {
        delete context;
    }
}

framegen_status_t framegen_context_get_info(framegen_context_t* context,
                                            framegen_context_info_t* out_info) {
    return guarded([&] {
        if (context == nullptr || !valid_output_struct(out_info)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "context info query requires a context and valid output");
        }
        const auto& state = context->impl;
        std::scoped_lock lock(state->mutex);
        if (state->destroyed) {
            return fail(FRAMEGEN_STATUS_INVALID_STATE, "context has been destroyed");
        }
        initialize_output(*out_info);
        out_info->device_id = state->device_id;
        out_info->history_generation = state->history_generation;
        out_info->configured = state->configured ? 1u : 0u;
        out_info->device_lost = state->device_lost ? 1u : 0u;
        return succeed();
    });
}

framegen_status_t framegen_context_query_capabilities(
    framegen_context_t* context, framegen_capability_result_t* out_result) {
    return guarded([&] {
        if (context == nullptr || !valid_output_struct(out_result)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "capability query requires a context and valid output");
        }
        const auto& state = context->impl;
        std::scoped_lock lock(state->mutex);
        if (state->destroyed) {
            return fail(FRAMEGEN_STATUS_INVALID_STATE, "context has been destroyed");
        }
        fill_capability_result(*state, *out_result);
        return succeed();
    });
}

framegen_status_t framegen_context_configure(
    framegen_context_t* context, const framegen_context_config_t* config,
    framegen_capability_result_t* out_result) {
    return guarded([&] {
        if (context == nullptr || !valid_input_struct(config) ||
            !valid_output_struct(out_result)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "configuration requires a context and valid structures");
        }
        auto state = context->impl;
        std::unique_lock backend_operation_lock(state->backend_operation_mutex);
        std::deque<std::shared_ptr<FrameRecord>> old_frames;
        {
            std::scoped_lock lock(state->mutex);
            const auto context_status = check_context(state);
            if (context_status != FRAMEGEN_STATUS_OK) {
                return context_status;
            }
            const auto supported = state->backend_info.supported_capabilities;
            const auto required_inputs =
                state->backend_info.required_input_capabilities |
                (state->backend_info.required_capabilities & kOptionalInputMask) |
                (config->required_capabilities & kOptionalInputMask);
            const auto required = config->required_capabilities |
                                  state->backend_info.required_capabilities |
                                  required_inputs;
            const auto required_unavailable = required & ~supported;
            const auto inputs_unavailable = required_inputs &
                ~config->available_input_capabilities;
            const auto unknown_required = config->required_capabilities &
                                          ~kKnownCapabilityMask;
            const auto unknown_preferred = config->preferred_capabilities &
                                           ~kKnownCapabilityMask;
            const auto unknown_available_inputs = config->available_input_capabilities &
                                                  ~kKnownInputMask;
            if (unknown_required != 0 || unknown_preferred != 0 ||
                unknown_available_inputs != 0) {
                return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                            "configuration contains unknown capability or input bits");
            }
            if (required_unavailable != 0 || inputs_unavailable != 0) {
                return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                            "backend requirements are not satisfied by configuration");
            }
            std::uint32_t max_in_flight = config->max_in_flight_requests;
            if (max_in_flight == 0) {
                max_in_flight = state->backend_info.max_in_flight_requests;
            }
            if (max_in_flight == 0) {
                max_in_flight = 2;
            }
            if (state->backend_info.max_in_flight_requests != 0 &&
                max_in_flight > state->backend_info.max_in_flight_requests) {
                return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                            "requested in-flight limit exceeds backend capacity");
            }
            const std::uint32_t history_capacity = config->history_capacity == 0
                ? 4 : config->history_capacity;
            if (history_capacity < 2 || history_capacity > 64) {
                return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                            "history capacity must be in [2, 64]");
            }
            if (state->history_generation == UINT64_MAX) {
                return fail(FRAMEGEN_STATUS_INTERNAL_ERROR,
                            "history generation sequence is exhausted");
            }

            framegen_capability_result_t negotiated{};
            negotiated.struct_size = sizeof(negotiated);
            negotiated.struct_version = FRAMEGEN_ABI_VERSION;
            negotiated.supported_capabilities = supported;
            negotiated.backend_required_capabilities =
                state->backend_info.required_capabilities;
            negotiated.backend_required_input_capabilities =
                state->backend_info.required_input_capabilities;
            const auto preferred = config->preferred_capabilities & supported;
            const auto preferred_inputs = preferred & kOptionalInputMask &
                config->available_input_capabilities;
            negotiated.negotiated_capabilities =
                (preferred & ~kOptionalInputMask) | preferred_inputs | required;
            negotiated.unmet_capabilities = 0;
            negotiated.max_in_flight_requests = max_in_flight;
            state->capabilities = negotiated;
            state->config = *config;
            state->hud_options = framegen_hud_options_t{
                .struct_size = sizeof(framegen_hud_options_t),
                .struct_version = FRAMEGEN_ABI_VERSION,
                .mode = FRAMEGEN_HUD_MODE_NO_KNOWLEDGE,
                .ui_temporal_source = FRAMEGEN_UI_SOURCE_NEAREST_PRESENTATION,
                .debug_visualization = FRAMEGEN_HUD_DEBUG_DISABLED,
            };
            state->temporal_quality_options = framegen_temporal_quality_options_t{
                .struct_size = sizeof(framegen_temporal_quality_options_t),
                .struct_version = FRAMEGEN_ABI_VERSION,
                .policy = FRAMEGEN_TEMPORAL_QUALITY_DISABLED,
                .debug_visualization = FRAMEGEN_TEMPORAL_QUALITY_DEBUG_DISABLED,
            };
            state->max_in_flight = max_in_flight;
            state->history_capacity = history_capacity;
            state->configured = true;
            old_frames.swap(state->frames);
            state->stream_id = 0;
            state->clock_domain = 0;
            ++state->history_generation;
            state->last_backend_history_generation = 0;
            for (auto& [id, ticket] : state->tickets) {
                (void)id;
                if (ticket->history_generation < state->history_generation) {
                    ticket->status = FRAMEGEN_TICKET_INVALIDATED;
                    ticket->error_status = FRAMEGEN_STATUS_STALE_FRAME;
                }
            }
            fill_capability_result(*state, *out_result);
        }
        if (state->backend.invalidate) {
            try {
                state->backend.invalidate(FRAMEGEN_INVALIDATE_HISTORY_RESET);
            } catch (...) {
                // Configuration was committed; backend hooks cannot roll it back.
            }
        }
        return succeed();
    });
}

framegen_status_t framegen_context_set_hud_options(
    framegen_context_t* context, const framegen_hud_options_t* options) {
    return guarded([&] {
        if (context == nullptr || !valid_input_struct(options)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "HUD configuration requires a context and valid options");
        }
        if (options->mode > FRAMEGEN_HUD_MODE_AUTOMATIC_PROTECTION ||
            options->ui_temporal_source > FRAMEGEN_UI_SOURCE_NEAREST_PRESENTATION ||
            options->debug_visualization > FRAMEGEN_HUD_DEBUG_FINAL_COMPOSITE ||
            (options->mode == FRAMEGEN_HUD_MODE_NO_KNOWLEDGE &&
             options->debug_visualization != FRAMEGEN_HUD_DEBUG_DISABLED &&
             options->debug_visualization != FRAMEGEN_HUD_DEBUG_FINAL_COMPOSITE)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "HUD mode, UI source, or debug visualization is invalid");
        }

        auto state = context->impl;
        std::unique_lock backend_operation_lock(state->backend_operation_mutex);
        std::deque<std::shared_ptr<FrameRecord>> old_frames;
        {
            std::scoped_lock lock(state->mutex);
            const auto context_status = check_context(state);
            if (context_status != FRAMEGEN_STATUS_OK) {
                return context_status;
            }
            if (!state->configured) {
                return fail(FRAMEGEN_STATUS_INVALID_STATE,
                            "context must be configured before HUD options are set");
            }
            const auto supported = state->backend_info.supported_capabilities;
            const auto negotiated = state->capabilities.negotiated_capabilities;
            if (options->mode == FRAMEGEN_HUD_MODE_EXPLICIT_UI_PLANE &&
                (((supported & FRAMEGEN_CAP_UI_PLANE) == 0) ||
                 ((state->config.available_input_capabilities &
                   FRAMEGEN_INPUT_UI_PLANE) == 0) ||
                 ((negotiated & FRAMEGEN_CAP_UI_PLANE) == 0))) {
                return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                            "explicit UI-plane mode requires negotiated UI-plane input support");
            }
            if (options->mode == FRAMEGEN_HUD_MODE_AUTOMATIC_PROTECTION &&
                ((supported & FRAMEGEN_CAP_AUTOMATIC_HUD_PROTECTION) == 0 ||
                 (negotiated & FRAMEGEN_CAP_AUTOMATIC_HUD_PROTECTION) == 0)) {
                return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                            "automatic HUD protection must be supported and negotiated");
            }
            if (options->debug_visualization != FRAMEGEN_HUD_DEBUG_DISABLED &&
                options->debug_visualization != FRAMEGEN_HUD_DEBUG_FINAL_COMPOSITE &&
                ((supported & FRAMEGEN_CAP_HUD_DEBUG_VISUALIZATION) == 0 ||
                 (negotiated & FRAMEGEN_CAP_HUD_DEBUG_VISUALIZATION) == 0)) {
                return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                            "HUD debug visualization must be supported and negotiated");
            }
            if (std::memcmp(&state->hud_options, options,
                            offsetof(framegen_hud_options_t, reserved)) == 0) {
                return succeed();
            }
            if (state->history_generation == UINT64_MAX) {
                return fail(FRAMEGEN_STATUS_INTERNAL_ERROR,
                            "history generation sequence is exhausted");
            }

            state->hud_options = *options;
            ++state->history_generation;
            state->last_backend_history_generation = 0;
            old_frames.swap(state->frames);
            state->stream_id = 0;
            state->clock_domain = 0;
            for (auto& [id, ticket] : state->tickets) {
                (void)id;
                if (ticket->history_generation < state->history_generation) {
                    ticket->status = FRAMEGEN_TICKET_INVALIDATED;
                    ticket->error_status = FRAMEGEN_STATUS_STALE_FRAME;
                }
            }
        }
        if (state->backend.invalidate) {
            try {
                state->backend.invalidate(FRAMEGEN_INVALIDATE_HISTORY_RESET);
            } catch (...) {
                // HUD option changes are committed even if a backend hook fails.
            }
        }
        return succeed();
    });
}

framegen_status_t framegen_context_set_temporal_quality_options(
    framegen_context_t* context,
    const framegen_temporal_quality_options_t* options) {
    return guarded([&] {
        if (context == nullptr || !valid_input_struct(options)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "temporal quality configuration requires a context and valid options");
        }
        if (options->policy > FRAMEGEN_TEMPORAL_QUALITY_NEAREST_ENDPOINT_FALLBACK ||
            options->debug_visualization > FRAMEGEN_TEMPORAL_QUALITY_DEBUG_CLASSES) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "temporal quality policy or debug visualization is invalid");
        }

        auto state = context->impl;
        std::unique_lock backend_operation_lock(state->backend_operation_mutex);
        std::deque<std::shared_ptr<FrameRecord>> old_frames;
        {
            std::scoped_lock lock(state->mutex);
            const auto context_status = check_context(state);
            if (context_status != FRAMEGEN_STATUS_OK) {
                return context_status;
            }
            if (!state->configured) {
                return fail(FRAMEGEN_STATUS_INVALID_STATE,
                            "context must be configured before temporal quality options are set");
            }
            const bool controller_requested =
                options->policy != FRAMEGEN_TEMPORAL_QUALITY_DISABLED ||
                options->debug_visualization != FRAMEGEN_TEMPORAL_QUALITY_DEBUG_DISABLED;
            const auto supported = state->backend_info.supported_capabilities;
            const auto negotiated = state->capabilities.negotiated_capabilities;
            if (controller_requested &&
                ((supported & FRAMEGEN_CAP_TEMPORAL_QUALITY_CONTROLLER) == 0 ||
                 (negotiated & FRAMEGEN_CAP_TEMPORAL_QUALITY_CONTROLLER) == 0)) {
                return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                            "temporal quality control must be supported and negotiated");
            }
            if (std::memcmp(&state->temporal_quality_options, options,
                            offsetof(framegen_temporal_quality_options_t, reserved)) == 0) {
                return succeed();
            }
            if (state->history_generation == UINT64_MAX) {
                return fail(FRAMEGEN_STATUS_INTERNAL_ERROR,
                            "history generation sequence is exhausted");
            }

            state->temporal_quality_options = *options;
            ++state->history_generation;
            state->last_backend_history_generation = 0;
            old_frames.swap(state->frames);
            state->stream_id = 0;
            state->clock_domain = 0;
            for (auto& [id, ticket] : state->tickets) {
                (void)id;
                if (ticket->history_generation < state->history_generation) {
                    ticket->status = FRAMEGEN_TICKET_INVALIDATED;
                    ticket->error_status = FRAMEGEN_STATUS_STALE_FRAME;
                }
            }
        }
        if (state->backend.invalidate) {
            try {
                state->backend.invalidate(FRAMEGEN_INVALIDATE_HISTORY_RESET);
            } catch (...) {
                // Option changes are committed even if a backend hook fails.
            }
        }
        return succeed();
    });
}

framegen_status_t framegen_submit_source_frame(
    framegen_context_t* context, const framegen_source_frame_t* frame,
    framegen_frame_receipt_t* out_receipt) {
    return guarded([&] {
        if (context == nullptr || !valid_input_struct(frame) ||
            !valid_output_struct(out_receipt) ||
            !valid_input_struct(&frame->optional_inputs)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "source submission requires valid context, frame, and receipt");
        }
        auto state = context->impl;
        framegen_backend_info_t backend_info{};
        framegen_context_config_t config{};
        framegen_capability_flags_t negotiated_capabilities{};
        std::uint64_t current_generation{};
        std::uint64_t expected_device_id{};
        framegen_hud_mode_t hud_mode{};
        {
            std::scoped_lock lock(state->mutex);
            if (check_context(state) != FRAMEGEN_STATUS_OK) {
                return state->device_lost ? FRAMEGEN_STATUS_DEVICE_LOST
                                          : FRAMEGEN_STATUS_INVALID_STATE;
            }
            if (!state->configured) {
                return fail(FRAMEGEN_STATUS_INVALID_STATE,
                            "context must be configured before source submission");
            }
            backend_info = state->backend_info;
            config = state->config;
            negotiated_capabilities = state->capabilities.negotiated_capabilities;
            current_generation = state->history_generation;
            expected_device_id = state->device_id;
            hud_mode = state->hud_options.mode;
        }

        const auto optional_mask = frame->optional_inputs.present_mask;
        if ((optional_mask & ~kKnownInputMask) != 0 || frame->stream_id == 0 ||
            frame->source_frame_id == 0 || frame->clock_domain == 0 ||
            frame->source_duration_ns <= 0 ||
            !is_known_timestamp(frame->source_timestamp_ns) ||
            !is_known_timestamp(frame->desired_presentation_timestamp_ns) ||
            !valid_image_metadata(frame->color, backend_info, true) ||
            frame->color.resource.device_id != expected_device_id ||
            !valid_input_struct(&frame->render_completion) ||
            !has_paired_callbacks(frame->render_completion.retain,
                                  frame->render_completion.release)) {
            return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                        "source frame metadata or color resource is invalid");
        }
        if (!backend_supports_format(backend_info, frame->color.pixel_format)) {
            return fail(FRAMEGEN_STATUS_UNSUPPORTED_RESOURCE,
                        "backend does not support the source color pixel format");
        }
        const auto required_inputs = backend_info.required_input_capabilities |
            (backend_info.required_capabilities & kOptionalInputMask) |
            (config.required_capabilities & kOptionalInputMask);
        if ((optional_mask & required_inputs) != required_inputs) {
            return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                        "source frame is missing a backend-required optional input");
        }
        if ((optional_mask & ~config.available_input_capabilities) != 0) {
            return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                        "source frame contains an input absent from the negotiated configuration");
        }
        if (hud_mode == FRAMEGEN_HUD_MODE_EXPLICIT_UI_PLANE &&
            (optional_mask & FRAMEGEN_INPUT_UI_PLANE) == 0) {
            return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                        "explicit UI-plane mode requires a UI texture on every source frame");
        }
        if (frame->color.dynamic_range == FRAMEGEN_DYNAMIC_RANGE_HDR &&
            ((backend_info.supported_capabilities & FRAMEGEN_CAP_HDR) == 0 ||
             (negotiated_capabilities & FRAMEGEN_CAP_HDR) == 0)) {
            return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                        "HDR source frames require HDR support and successful negotiation");
        }
        if (((config.required_capabilities |
              backend_info.required_capabilities) & FRAMEGEN_CAP_HDR) != 0 &&
            frame->color.dynamic_range != FRAMEGEN_DYNAMIC_RANGE_HDR) {
            return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                        "required HDR capability requires HDR source frames");
        }
        if (frame->render_completion.sync_type == FRAMEGEN_SYNC_NONE_READY) {
            if (frame->render_completion.native_handle != nullptr ||
                frame->render_completion.user_data != nullptr ||
                frame->render_completion.retain != nullptr ||
                frame->render_completion.release != nullptr ||
                frame->render_completion.value != 0) {
                return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                            "ready synchronization points must not contain a native handle or value");
            }
        } else if (frame->render_completion.native_handle == nullptr ||
                   frame->render_completion.backend_type != backend_info.backend_type ||
                   frame->render_completion.device_id == 0 ||
                   frame->render_completion.device_id != frame->color.resource.device_id) {
            return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                        "render completion point does not match the source resource device");
        }

        std::unique_lock backend_operation_lock(state->backend_operation_mutex);
        {
            std::scoped_lock lock(state->mutex);
            const auto context_status = check_context(state);
            if (context_status != FRAMEGEN_STATUS_OK) {
                return context_status;
            }
            if (!state->configured || state->history_generation != current_generation) {
                return fail(FRAMEGEN_STATUS_STALE_FRAME,
                            "history changed before source resource import");
            }
        }

        auto record = std::make_shared<FrameRecord>();
        record->source = *frame;
        record->history_generation = current_generation;
        record->input.timing = FrameTiming{
            .sequence = frame->source_frame_id,
            .timestamp_ns = frame->source_timestamp_ns,
            .clock_domain = frame->clock_domain,
            .duration_ns = frame->source_duration_ns,
            .render_completion_timestamp_ns = frame->render_completion_timestamp_ns,
            .desired_presentation_timestamp_ns = frame->desired_presentation_timestamp_ns,
        };
        record->input.color_metadata = FrameColorMetadata{
            .transfer_function = to_transfer_function(frame->color.transfer_function),
            .dynamic_range = to_dynamic_range(frame->color.dynamic_range),
        };
        auto retain_image = [&](const framegen_image_t& image,
                                Texture* destination,
                                bool color_image,
                                bool import_for_backend) -> framegen_status_t {
            if (!valid_image_metadata(image, backend_info, color_image) ||
                image.resource.device_id != frame->color.resource.device_id) {
                return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                            "optional image resource metadata is invalid or from another device");
            }
            if (color_image &&
                (image.width != frame->color.width ||
                 image.height != frame->color.height)) {
                return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                            "UI plane dimensions must match the source color image");
            }
            if (import_for_backend) {
                if (!backend_supports_format(backend_info, image.pixel_format)) {
                    return fail(FRAMEGEN_STATUS_UNSUPPORTED_RESOURCE,
                                "backend does not support the supplied pixel format");
                }
                auto texture = state->backend.import_image(image);
                if (!texture) {
                    return fail(map_cpp_error(texture.error()), texture.error().message);
                }
                const auto desc = texture->descriptor();
                const auto imported_backend = state->backend.backend->backend_id();
                const auto expected_color_space = color_image
                    ? to_color_space(image) : std::optional<ColorSpace>{};
                const auto expected_alpha = color_image
                    ? to_alpha_mode(image.alpha_mode) : std::optional<AlphaMode>{};
                if (texture->backend_id() != imported_backend ||
                    texture->device_id() != state->device_id ||
                    desc.width != image.width || desc.height != image.height ||
                    static_cast<std::uint32_t>(desc.format) !=
                        expected_cpp_format(image.pixel_format) ||
                    (color_image &&
                     (desc.color_space != *expected_color_space ||
                      desc.alpha_mode != *expected_alpha))) {
                    return fail(FRAMEGEN_STATUS_UNSUPPORTED_RESOURCE,
                                "backend imported an image with mismatched device or metadata");
                }
                if (destination == nullptr) {
                    return fail(FRAMEGEN_STATUS_INTERNAL_ERROR,
                                "backend image destination is missing");
                }
                *destination = std::move(*texture);
            }
            if (auto lease = lease_handle(image.resource.native_handle,
                                          image.resource.user_data,
                                          image.resource.retain,
                                          image.resource.release)) {
                record->leases.push_back(std::move(lease));
            }
            return FRAMEGEN_STATUS_OK;
        };

        auto status = retain_image(frame->color, &record->input.texture, true, true);
        if (status != FRAMEGEN_STATUS_OK) {
            return status;
        }
        const auto& optional = frame->optional_inputs;
        auto& target = record->input.optional_inputs;
        struct OptionalImageSlot {
            framegen_capability_flags_t capability;
            const framegen_image_t* image;
            Texture* texture;
            bool color_image;
        };
        const std::array<OptionalImageSlot, 5> images{{
            {FRAMEGEN_INPUT_MOTION_VECTORS, &optional.motion_vectors,
             &target.motion_vectors, false},
            {FRAMEGEN_INPUT_DEPTH, &optional.depth, &target.depth, false},
            {FRAMEGEN_INPUT_UI_PLANE, &optional.ui_texture, &target.ui_texture, true},
            {FRAMEGEN_INPUT_UI_MASK, &optional.ui_mask, &target.ui_mask, false},
            {FRAMEGEN_INPUT_REACTIVE_MASK, &optional.reactive_mask,
             &target.reactive_mask, false},
        }};
        for (const auto& slot : images) {
            if ((optional_mask & slot.capability) != 0) {
                if (!valid_auxiliary_format(slot.capability, slot.image->pixel_format)) {
                    return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                                "optional image pixel format does not match its semantic role");
                }
                if (slot.capability == FRAMEGEN_INPUT_MOTION_VECTORS) {
                    const bool valid_encoding =
                        (optional.motion_vector_encoding ==
                             FRAMEGEN_MOTION_VECTOR_ENCODING_SIGNED_XY ||
                         optional.motion_vector_encoding ==
                             FRAMEGEN_MOTION_VECTOR_ENCODING_UNORM_XY) &&
                        (optional.motion_vector_units ==
                             FRAMEGEN_MOTION_VECTOR_UNITS_PIXELS ||
                         optional.motion_vector_units ==
                             FRAMEGEN_MOTION_VECTOR_UNITS_NORMALIZED_VIEWPORT) &&
                        (optional.motion_vector_direction ==
                             FRAMEGEN_MOTION_VECTOR_DIRECTION_PREVIOUS_TO_CURRENT ||
                         optional.motion_vector_direction ==
                             FRAMEGEN_MOTION_VECTOR_DIRECTION_CURRENT_TO_PREVIOUS);
                    const bool expected_encoding =
                        (slot.image->pixel_format == FRAMEGEN_PIXEL_FORMAT_RG8_UNORM &&
                         optional.motion_vector_encoding ==
                             FRAMEGEN_MOTION_VECTOR_ENCODING_UNORM_XY) ||
                        (slot.image->pixel_format != FRAMEGEN_PIXEL_FORMAT_RG8_UNORM &&
                         optional.motion_vector_encoding ==
                             FRAMEGEN_MOTION_VECTOR_ENCODING_SIGNED_XY);
                    if (!valid_encoding || !expected_encoding) {
                        return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                                    "motion-vector encoding must match its pixel format and declare units and direction");
                    }
                }
                if (slot.capability == FRAMEGEN_INPUT_DEPTH &&
                    optional.depth_encoding != FRAMEGEN_DEPTH_ENCODING_DEVICE_Z &&
                    optional.depth_encoding != FRAMEGEN_DEPTH_ENCODING_REVERSE_DEVICE_Z &&
                    optional.depth_encoding != FRAMEGEN_DEPTH_ENCODING_LINEAR_VIEW_DISTANCE) {
                    return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                                "depth encoding must be specified for a depth input");
                }
                const bool forwarded =
                    (negotiated_capabilities & slot.capability) != 0;
                if (forwarded && slot.capability == FRAMEGEN_INPUT_UI_PLANE &&
                    slot.image->dynamic_range == FRAMEGEN_DYNAMIC_RANGE_HDR &&
                    ((backend_info.supported_capabilities & FRAMEGEN_CAP_HDR) == 0 ||
                     (negotiated_capabilities & FRAMEGEN_CAP_HDR) == 0)) {
                    return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                                "HDR UI input requires negotiated HDR support");
                }
                status = retain_image(*slot.image,
                                      forwarded ? slot.texture : nullptr,
                                      slot.color_image, forwarded);
                if (status != FRAMEGEN_STATUS_OK) {
                    return status;
                }
                if (forwarded && slot.capability == FRAMEGEN_INPUT_UI_PLANE) {
                    target.ui_color_metadata = FrameColorMetadata{
                        .transfer_function =
                            to_transfer_function(slot.image->transfer_function),
                        .dynamic_range = to_dynamic_range(slot.image->dynamic_range),
                    };
                }
            }
        }
        if (target.ui_texture &&
            target.ui_texture.resource_identity() == record->input.texture.resource_identity()) {
            return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                        "scene color and explicit UI plane must use distinct texture storage");
        }
        if ((optional_mask & FRAMEGEN_INPUT_CAMERA_MATRICES) != 0) {
            std::array<float, 16> camera{};
            std::array<float, 16> projection{};
            std::copy_n(optional.camera_to_world, camera.size(), camera.begin());
            std::copy_n(optional.projection, projection.size(), projection.begin());
            if (!std::all_of(camera.begin(), camera.end(),
                             [](float value) { return std::isfinite(value); }) ||
                !std::all_of(projection.begin(), projection.end(),
                             [](float value) { return std::isfinite(value); })) {
                return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                            "camera and projection matrices must be finite");
            }
            if ((negotiated_capabilities & FRAMEGEN_INPUT_CAMERA_MATRICES) != 0) {
                target.camera_to_world = camera;
                target.projection = projection;
            }
        }
        if ((optional_mask & FRAMEGEN_INPUT_JITTER) != 0) {
            if (!std::isfinite(optional.jitter_xy[0]) ||
                !std::isfinite(optional.jitter_xy[1])) {
                return fail(FRAMEGEN_STATUS_INVALID_FRAME, "jitter values must be finite");
            }
            if ((negotiated_capabilities & FRAMEGEN_INPUT_JITTER) != 0) {
                target.jitter_xy = std::array<float, 2>{optional.jitter_xy[0],
                                                        optional.jitter_xy[1]};
            }
        }
        if ((optional_mask & FRAMEGEN_INPUT_EXPOSURE) != 0) {
            if (!std::isfinite(optional.exposure)) {
                return fail(FRAMEGEN_STATUS_INVALID_FRAME, "exposure must be finite");
            }
            if ((negotiated_capabilities & FRAMEGEN_INPUT_EXPOSURE) != 0) {
                target.exposure = optional.exposure;
            }
        }
        if ((optional_mask & FRAMEGEN_INPUT_MOTION_VECTORS) != 0) {
            if ((negotiated_capabilities & FRAMEGEN_INPUT_MOTION_VECTORS) != 0) {
                target.motion_vector_encoding = optional.motion_vector_encoding;
                target.motion_vector_units = optional.motion_vector_units;
                target.motion_vector_direction = optional.motion_vector_direction;
            }
        }
        if ((optional_mask & FRAMEGEN_INPUT_DEPTH) != 0) {
            if ((negotiated_capabilities & FRAMEGEN_INPUT_DEPTH) != 0) {
                target.depth_encoding = optional.depth_encoding;
            }
        }

        if (frame->render_completion.sync_type != FRAMEGEN_SYNC_NONE_READY) {
            auto dependency = state->backend.import_sync(frame->render_completion);
            if (!dependency) {
                return fail(map_cpp_error(dependency.error()), dependency.error().message);
            }
            if (!*dependency || (*dependency)->backend_id() !=
                    state->backend.backend->backend_id() ||
                (*dependency)->device_id() != state->device_id) {
                return fail(FRAMEGEN_STATUS_UNSUPPORTED_RESOURCE,
                            "backend imported a render dependency for another device");
            }
            record->render_dependency = std::move(*dependency);
            if (auto lease = lease_handle(frame->render_completion.native_handle,
                                          frame->render_completion.user_data,
                                          frame->render_completion.retain,
                                          frame->render_completion.release)) {
                record->leases.push_back(std::move(lease));
            }
        }

        {
            std::scoped_lock lock(state->mutex);
            if (check_context(state) != FRAMEGEN_STATUS_OK) {
                return state->device_lost ? FRAMEGEN_STATUS_DEVICE_LOST
                                          : FRAMEGEN_STATUS_INVALID_STATE;
            }
            if (!state->configured || current_generation != state->history_generation) {
                return fail(FRAMEGEN_STATUS_STALE_FRAME,
                            "history changed while the source frame was imported");
            }
            if (frame->source_frame_id <= state->last_source_frame_id) {
                return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                            "source frame IDs must increase monotonically for the context lifetime");
            }
            if (!state->frames.empty()) {
                const auto& previous = state->frames.back()->source;
                if (frame->stream_id != previous.stream_id ||
                    frame->clock_domain != previous.clock_domain) {
                    return fail(FRAMEGEN_STATUS_STALE_FRAME,
                                "stream or clock changes require an explicit history reset");
                }
                if (frame->source_timestamp_ns <= previous.source_timestamp_ns ||
                    frame->desired_presentation_timestamp_ns <=
                        previous.desired_presentation_timestamp_ns) {
                    return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                                "source and desired presentation timestamps must increase");
                }
                if (!same_image_metadata(frame->color, previous.color)) {
                    return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                                "resize, format, and color changes require explicit invalidation");
                }
            } else if (state->stream_id != 0 &&
                       (frame->stream_id != state->stream_id ||
                        frame->clock_domain != state->clock_domain)) {
                return fail(FRAMEGEN_STATUS_STALE_FRAME,
                            "stream or clock changes require an explicit history reset");
            }
            state->stream_id = frame->stream_id;
            state->clock_domain = frame->clock_domain;
            state->last_source_frame_id = frame->source_frame_id;
            state->frames.push_back(record);
            while (state->frames.size() > state->history_capacity) {
                state->frames.pop_front();
            }
            ++state->statistics.source_frames_submitted;
            initialize_output(*out_receipt);
            out_receipt->source_frame_id = frame->source_frame_id;
            out_receipt->history_generation = state->history_generation;
            out_receipt->available_input_capabilities =
                FRAMEGEN_CAP_COLOR_ONLY | (optional_mask & kKnownInputMask) |
                (frame->color.dynamic_range == FRAMEGEN_DYNAMIC_RANGE_HDR
                    ? FRAMEGEN_CAP_HDR : 0);
        }
        return succeed();
    });
}

framegen_status_t framegen_request_interpolation(
    framegen_context_t* context, const framegen_interpolation_request_t* request,
    framegen_ticket_t** out_ticket) {
    return guarded([&] {
        if (out_ticket != nullptr) {
            *out_ticket = nullptr;
        }
        if (context == nullptr || !valid_input_struct(request) || out_ticket == nullptr) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "interpolation request requires a context and valid structures");
        }
        auto state = context->impl;
        auto ticket_state = std::make_shared<TicketImpl>();
        std::unique_ptr<framegen_ticket> public_ticket(
            new framegen_ticket{ticket_state});
        FrameSubmission submission;
        const auto enqueue_started = std::chrono::steady_clock::now();
        {
            std::scoped_lock lock(state->mutex);
            if (check_context(state) != FRAMEGEN_STATUS_OK) {
                return state->device_lost ? FRAMEGEN_STATUS_DEVICE_LOST
                                          : FRAMEGEN_STATUS_INVALID_STATE;
            }
            if (!state->configured) {
                return fail(FRAMEGEN_STATUS_INVALID_STATE,
                            "context must be configured before interpolation requests");
            }
            if (request->history_generation != state->history_generation ||
                request->clock_domain == 0 || request->clock_domain != state->clock_domain) {
                return fail(FRAMEGEN_STATUS_STALE_FRAME,
                            "interpolation request belongs to a different frame history");
            }
            auto previous = find_frame(*state, request->previous_source_frame_id);
            auto current = find_frame(*state, request->current_source_frame_id);
            if (!previous || !current ||
                previous->history_generation != state->history_generation ||
                current->history_generation != state->history_generation ||
                previous->source.stream_id != current->source.stream_id ||
                previous->source.clock_domain != current->source.clock_domain ||
                previous->source.source_frame_id >= current->source.source_frame_id ||
                previous->source.source_timestamp_ns >= current->source.source_timestamp_ns) {
                return fail(FRAMEGEN_STATUS_STALE_FRAME,
                            "interpolation frames are missing, stale, or from mismatched history");
            }
            if (!same_image_metadata(previous->source.color, current->source.color)) {
                return fail(FRAMEGEN_STATUS_INVALID_FRAME,
                            "interpolation frame descriptions do not match");
            }
            if (request->interpolation_timestamp_ns <=
                    previous->source.source_timestamp_ns ||
                request->interpolation_timestamp_ns >=
                    current->source.source_timestamp_ns ||
                !is_known_timestamp(request->interpolation_timestamp_ns) ||
                !is_known_timestamp(request->desired_presentation_timestamp_ns) ||
                !is_known_timestamp(request->presentation_deadline_ns) ||
                request->presentation_deadline_ns <
                    request->desired_presentation_timestamp_ns) {
                return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                            "interpolation time must be bracketed and deadline must not precede desired presentation");
            }
            if ((state->capabilities.negotiated_capabilities &
                 FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME) == 0) {
                const auto left = timestamp_distance(
                    previous->source.source_timestamp_ns,
                    request->interpolation_timestamp_ns);
                const auto right = timestamp_distance(
                    request->interpolation_timestamp_ns,
                    current->source.source_timestamp_ns);
                if (left != right) {
                    return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                                "selected backend does not support arbitrary interpolation time");
                }
            }
            const auto required_inputs = state->backend_info.required_input_capabilities |
                (state->backend_info.required_capabilities & kOptionalInputMask) |
                (state->config.required_capabilities & kOptionalInputMask);
            const auto previous_inputs = previous->source.optional_inputs.present_mask;
            const auto current_inputs = current->source.optional_inputs.present_mask;
            if ((previous_inputs & required_inputs) != required_inputs ||
                (current_inputs & required_inputs) != required_inputs) {
                return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                            "one or both interpolation frames lack a required optional input");
            }
            if (state->hud_options.mode == FRAMEGEN_HUD_MODE_EXPLICIT_UI_PLANE &&
                ((previous_inputs & FRAMEGEN_INPUT_UI_PLANE) == 0 ||
                 (current_inputs & FRAMEGEN_INPUT_UI_PLANE) == 0)) {
                return fail(FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE,
                            "explicit UI-plane mode requires both source UI textures");
            }
            retire_released_tickets_locked(*state);
            if (state->active_in_flight >= state->max_in_flight ||
                state->tickets.size() >= state->max_in_flight) {
                return fail(FRAMEGEN_STATUS_BACKPRESSURE,
                            "GPU work or retained-ticket capacity is currently full");
            }
            if (state->next_ticket_id == 0 || state->next_ticket_id == UINT64_MAX) {
                return fail(FRAMEGEN_STATUS_INTERNAL_ERROR,
                            "interpolation ticket identifier sequence is exhausted");
            }
            ticket_state->context = state;
            ticket_state->id = state->next_ticket_id++;
            ticket_state->previous_frame_id = request->previous_source_frame_id;
            ticket_state->current_frame_id = request->current_source_frame_id;
            ticket_state->history_generation = request->history_generation;
            ticket_state->clock_domain = request->clock_domain;
            ticket_state->interpolation_ns = request->interpolation_timestamp_ns;
            ticket_state->desired_presentation_ns =
                request->desired_presentation_timestamp_ns;
            ticket_state->deadline_ns = request->presentation_deadline_ns;
            ticket_state->previous_source = previous;
            ticket_state->current_source = current;
            const auto interval = timestamp_distance(
                previous->source.source_timestamp_ns,
                current->source.source_timestamp_ns);
            const auto offset = timestamp_distance(
                previous->source.source_timestamp_ns,
                request->interpolation_timestamp_ns);
            submission.previous = previous->input;
            submission.current = current->input;
            submission.interpolation = static_cast<float>(
                static_cast<long double>(offset) / static_cast<long double>(interval));
            submission.interpolation_timestamp_ns =
                request->interpolation_timestamp_ns;
            submission.desired_presentation_timestamp_ns =
                request->desired_presentation_timestamp_ns;
            submission.presentation_deadline_ns = request->presentation_deadline_ns;
            switch (state->hud_options.mode) {
            case FRAMEGEN_HUD_MODE_EXPLICIT_UI_PLANE:
                submission.hud_options.mode = HudMode::explicit_ui_plane;
                break;
            case FRAMEGEN_HUD_MODE_AUTOMATIC_PROTECTION:
                submission.hud_options.mode = HudMode::automatic_protection;
                break;
            default:
                submission.hud_options.mode = HudMode::no_hud_knowledge;
                break;
            }
            switch (state->hud_options.ui_temporal_source) {
            case FRAMEGEN_UI_SOURCE_PREVIOUS:
                submission.hud_options.ui_source = UiTemporalSource::previous_frame;
                break;
            case FRAMEGEN_UI_SOURCE_CURRENT:
                submission.hud_options.ui_source = UiTemporalSource::current_frame;
                break;
            default:
                submission.hud_options.ui_source = UiTemporalSource::nearest_presentation;
                break;
            }
            switch (state->hud_options.debug_visualization) {
            case FRAMEGEN_HUD_DEBUG_RAW_MASK:
                submission.hud_options.debug_visualization = HudDebugVisualization::raw_mask;
                break;
            case FRAMEGEN_HUD_DEBUG_STABILIZED_MASK:
                submission.hud_options.debug_visualization = HudDebugVisualization::stabilized_mask;
                break;
            case FRAMEGEN_HUD_DEBUG_PROTECTED_REGIONS:
                submission.hud_options.debug_visualization = HudDebugVisualization::protected_regions;
                break;
            case FRAMEGEN_HUD_DEBUG_INTERPOLATION_CONFIDENCE:
                submission.hud_options.debug_visualization = HudDebugVisualization::interpolation_confidence;
                break;
            case FRAMEGEN_HUD_DEBUG_FINAL_COMPOSITE:
                submission.hud_options.debug_visualization = HudDebugVisualization::final_composite;
                break;
            default:
                submission.hud_options.debug_visualization = HudDebugVisualization::disabled;
                break;
            }
            switch (state->temporal_quality_options.policy) {
            case FRAMEGEN_TEMPORAL_QUALITY_CONTINUOUS_ENDPOINT_BLEND:
                submission.temporal_quality.policy =
                    TemporalQualityPolicy::continuous_endpoint_blend;
                break;
            case FRAMEGEN_TEMPORAL_QUALITY_CONTINUOUS_SOURCE_BLEND:
                submission.temporal_quality.policy =
                    TemporalQualityPolicy::continuous_source_blend;
                break;
            case FRAMEGEN_TEMPORAL_QUALITY_NEAREST_ENDPOINT_FALLBACK:
                submission.temporal_quality.policy =
                    TemporalQualityPolicy::nearest_endpoint_fallback;
                break;
            default:
                submission.temporal_quality.policy = TemporalQualityPolicy::disabled;
                break;
            }
            switch (state->temporal_quality_options.debug_visualization) {
            case FRAMEGEN_TEMPORAL_QUALITY_DEBUG_CONFIDENCE:
                submission.temporal_quality.debug_visualization =
                    TemporalQualityDebugVisualization::confidence;
                break;
            case FRAMEGEN_TEMPORAL_QUALITY_DEBUG_DISOCCLUSION:
                submission.temporal_quality.debug_visualization =
                    TemporalQualityDebugVisualization::disocclusion;
                break;
            case FRAMEGEN_TEMPORAL_QUALITY_DEBUG_UNSTABLE_THIN:
                submission.temporal_quality.debug_visualization =
                    TemporalQualityDebugVisualization::unstable_thin_features;
                break;
            case FRAMEGEN_TEMPORAL_QUALITY_DEBUG_HIGH_FREQUENCY:
                submission.temporal_quality.debug_visualization =
                    TemporalQualityDebugVisualization::high_frequency_texture;
                break;
            case FRAMEGEN_TEMPORAL_QUALITY_DEBUG_SPECULAR_PARTICLES:
                submission.temporal_quality.debug_visualization =
                    TemporalQualityDebugVisualization::specular_or_particles;
                break;
            case FRAMEGEN_TEMPORAL_QUALITY_DEBUG_SCENE_CUT:
                submission.temporal_quality.debug_visualization =
                    TemporalQualityDebugVisualization::scene_cut;
                break;
            case FRAMEGEN_TEMPORAL_QUALITY_DEBUG_CLASSES:
                submission.temporal_quality.debug_visualization =
                    TemporalQualityDebugVisualization::confidence_classes;
                break;
            default:
                submission.temporal_quality.debug_visualization =
                    TemporalQualityDebugVisualization::disabled;
                break;
            }
            if (previous->render_dependency) {
                submission.gpu_dependencies.push_back(previous->render_dependency);
            }
            if (current->render_dependency) {
                submission.gpu_dependencies.push_back(current->render_dependency);
            }
            state->tickets.emplace(ticket_state->id, ticket_state);
            ++state->active_in_flight;
            ticket_state->gpu_counted_in_flight = true;
            ++state->statistics.interpolation_requests;
        }

        {
            std::unique_lock backend_operation_lock(state->backend_operation_mutex);
            {
                std::scoped_lock lock(state->mutex);
                const bool stale = !state->configured ||
                    state->history_generation != request->history_generation ||
                    ticket_state->status == FRAMEGEN_TICKET_INVALIDATED;
                if (state->destroyed || state->device_lost || stale) {
                    if (ticket_state->gpu_counted_in_flight &&
                        state->active_in_flight != 0) {
                        --state->active_in_flight;
                        ticket_state->gpu_counted_in_flight = false;
                    }
                    ticket_state->gpu_completed = true;
                    ticket_state->presentation_notified = true;
                    ticket_state->consumer_finished = true;
                    state->tickets.erase(ticket_state->id);
                    if (state->device_lost) {
                        return fail(FRAMEGEN_STATUS_DEVICE_LOST,
                                    "device was lost before backend submission");
                    }
                    return fail(state->destroyed ? FRAMEGEN_STATUS_INVALID_STATE
                                                 : FRAMEGEN_STATUS_STALE_FRAME,
                                "history changed before backend submission");
                }
                // Derive reset state only after acquiring the same serialization
                // lock that orders backend submissions. Concurrent requests can
                // otherwise all observe the old generation and each discard the
                // temporal history initialized by the preceding request.
                submission.reset_history =
                    state->last_backend_history_generation !=
                    request->history_generation;
            }

            Result<GeneratedFrame> generated = std::unexpected(
                Error{ErrorCode::internal_error, "backend submission did not complete"});
            try {
                generated = state->generator->submit(submission);
            } catch (const std::exception& error) {
                generated = std::unexpected(Error{ErrorCode::backend_failure, error.what()});
            } catch (...) {
                generated = std::unexpected(Error{ErrorCode::backend_failure,
                    "backend submission threw an unknown exception"});
            }
            const auto enqueue_ended = std::chrono::steady_clock::now();
            const auto enqueue_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                enqueue_ended - enqueue_started).count();
            if (!generated) {
                std::scoped_lock lock(state->mutex);
                if (ticket_state->gpu_counted_in_flight && state->active_in_flight != 0) {
                    --state->active_in_flight;
                    ticket_state->gpu_counted_in_flight = false;
                }
                ticket_state->gpu_completed = true;
                ++state->statistics.failed_requests;
                state->statistics.total_cpu_enqueue_time_ns +=
                    static_cast<std::uint64_t>(std::max<std::int64_t>(0, enqueue_ns));
                state->tickets.erase(ticket_state->id);
                return fail(map_cpp_error(generated.error()), generated.error().message);
            }

            {
                std::scoped_lock lock(state->mutex);
                state->statistics.total_cpu_enqueue_time_ns +=
                    static_cast<std::uint64_t>(std::max<std::int64_t>(0, enqueue_ns));
                ticket_state->generated = std::move(*generated);
                state->last_backend_history_generation = request->history_generation;
            }
        }
        *out_ticket = public_ticket.release();
        return succeed();
    });
}

framegen_status_t framegen_ticket_poll(framegen_ticket_t* ticket,
                                       std::int64_t now_ns,
                                       framegen_ticket_result_t* out_result) {
    return guarded([&] {
        if (ticket == nullptr || !ticket->impl || !valid_output_struct(out_result) ||
            !is_known_timestamp(now_ns)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "ticket poll requires a ticket, valid output, and known time");
        }
        auto state = ticket->impl->context.lock();
        if (!state) {
            initialize_output(*out_result);
            out_result->status = FRAMEGEN_TICKET_FAILED;
            out_result->error_status = FRAMEGEN_STATUS_INVALID_STATE;
            out_result->ticket_id = ticket->impl->id;
            return succeed();
        }
        auto ticket_state = ticket->impl;
        std::unique_lock backend_operation_lock(state->backend_operation_mutex);
        {
            std::scoped_lock lock(state->mutex);
            refresh_ticket_locked(*state, *ticket_state);
            if (ticket_state->status == FRAMEGEN_TICKET_PENDING ||
                ticket_state->status == FRAMEGEN_TICKET_READY) {
                if (!ticket_state->presentation_notified &&
                    now_ns >= ticket_state->deadline_ns) {
                    ticket_state->status = FRAMEGEN_TICKET_DEADLINE_MISSED;
                    ++state->statistics.deadline_misses;
                } else if (ticket_state->gpu_completed &&
                           ticket_state->status == FRAMEGEN_TICKET_PENDING) {
                    ticket_state->status = FRAMEGEN_TICKET_READY;
                    ++state->statistics.generated_frames_ready;
                }
            }
            initialize_output(*out_result);
            out_result->status = ticket_state->status;
            out_result->error_status = ticket_state->error_status;
            out_result->ticket_id = ticket_state->id;
            if (ticket_state->status == FRAMEGEN_TICKET_PENDING ||
                ticket_state->status == FRAMEGEN_TICKET_READY) {
                auto generated = &out_result->generated_frame;
                generated->struct_size = sizeof(*generated);
                generated->struct_version = FRAMEGEN_ABI_VERSION;
                generated->ticket_id = ticket_state->id;
                generated->previous_source_frame_id = ticket_state->previous_frame_id;
                generated->current_source_frame_id = ticket_state->current_frame_id;
                generated->interpolation_timestamp_ns = ticket_state->interpolation_ns;
                generated->desired_presentation_timestamp_ns =
                    ticket_state->desired_presentation_ns;
                generated->presentation_deadline_ns = ticket_state->deadline_ns;
                auto image = state->backend.export_image(ticket_state->generated.texture);
                const auto output_descriptor =
                    ticket_state->generated.texture.descriptor();
                if (!image || !valid_exported_image(
                                  *image, state->backend_info, state->device_id,
                                  output_descriptor)) {
                    ticket_state->status = FRAMEGEN_TICKET_FAILED;
                    ticket_state->error_status = image
                        ? FRAMEGEN_STATUS_BACKEND_ERROR
                        : map_cpp_error(image.error());
                    out_result->status = ticket_state->status;
                    out_result->error_status = ticket_state->error_status;
                    ++state->statistics.failed_requests;
                    initialize_output(*generated);
                } else {
                    generated->image = std::move(*image);
                    generated->image.width = ticket_state->previous_source->source.color.width;
                    generated->image.height = ticket_state->previous_source->source.color.height;
                    generated->image.pixel_format =
                        ticket_state->previous_source->source.color.pixel_format;
                    generated->image.color_space =
                        ticket_state->previous_source->source.color.color_space;
                    generated->image.transfer_function =
                        ticket_state->previous_source->source.color.transfer_function;
                    generated->image.dynamic_range =
                        ticket_state->previous_source->source.color.dynamic_range;
                    generated->image.alpha_mode =
                        ticket_state->previous_source->source.color.alpha_mode;
                    auto sync = state->backend.export_sync(
                        *ticket_state->generated.completion);
                    if (!sync || !valid_exported_sync(
                                     *sync, state->backend_info, state->device_id,
                                     ticket_state->generated.completion->poll_status())) {
                        ticket_state->status = FRAMEGEN_TICKET_FAILED;
                        ticket_state->error_status = sync
                            ? FRAMEGEN_STATUS_BACKEND_ERROR
                            : map_cpp_error(sync.error());
                        out_result->status = ticket_state->status;
                        out_result->error_status = ticket_state->error_status;
                        ++state->statistics.failed_requests;
                        initialize_output(*generated);
                    } else {
                        generated->completion = std::move(*sync);
                    }
                }
            }
            retire_released_tickets_locked(*state);
        }
        return succeed();
    });
}

void framegen_ticket_release(framegen_ticket_t* ticket) {
    try {
        if (ticket == nullptr) {
            return;
        }
        if (ticket->impl) {
            auto state = ticket->impl->context.lock();
            if (state) {
                bool needs_background_retirement{};
                {
                    std::scoped_lock lock(state->mutex);
                    ticket->impl->client_released = true;
                    retire_released_tickets_locked(*state);
                    const auto retained = state->tickets.find(ticket->impl->id);
                    needs_background_retirement = retained != state->tickets.end() &&
                        retained->second->presentation_notified;
                }
                if (needs_background_retirement) {
                    enqueue_for_retirement(state);
                }
            }
        }
        delete ticket;
    } catch (...) {
        delete ticket;
    }
}

framegen_status_t framegen_notify_presentation(
    framegen_context_t* context, const framegen_presentation_event_t* event) {
    return guarded([&] {
        if (context == nullptr || !valid_input_struct(event) ||
            !is_known_timestamp(event->presentation_timestamp_ns) ||
            !valid_input_struct(&event->consumer_completion)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "presentation notification requires valid structures and a known timestamp");
        }
        auto state = context->impl;
        std::unique_lock backend_operation_lock(state->backend_operation_mutex);
        std::shared_ptr<TicketImpl> ticket_state;
        std::shared_ptr<NativeLease> consumer_lease;
        std::shared_ptr<const GpuSyncPoint> consumer_sync;
        bool needs_background_retirement{};
        const bool generated_disposition =
            event->disposition == FRAMEGEN_PRESENTED_GENERATED_FRAME;
        const bool ticket_capable_disposition = generated_disposition ||
            event->disposition == FRAMEGEN_BYPASSED_GENERATED_FRAME ||
            event->disposition == FRAMEGEN_DROPPED_FRAME;

        {
            std::scoped_lock lock(state->mutex);
            if (state->destroyed) {
                return fail(FRAMEGEN_STATUS_INVALID_STATE,
                            "context has been destroyed");
            }
            if (event->clock_domain == 0) {
                return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                            "presentation event requires a clock-domain token");
            }
            if (event->disposition != FRAMEGEN_PRESENTED_REAL_FRAME &&
                event->disposition != FRAMEGEN_PRESENTED_GENERATED_FRAME &&
                event->disposition != FRAMEGEN_BYPASSED_GENERATED_FRAME &&
                event->disposition != FRAMEGEN_DROPPED_FRAME) {
                return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                            "presentation disposition is unknown");
            }
            if (event->disposition == FRAMEGEN_PRESENTED_REAL_FRAME &&
                event->ticket_id != 0) {
                return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                            "real-frame presentation must not reference a generated ticket");
            }
            if (ticket_capable_disposition && event->ticket_id != 0) {
                const auto it = state->tickets.find(event->ticket_id);
                if (it == state->tickets.end()) {
                    return fail(FRAMEGEN_STATUS_STALE_FRAME,
                                "presentation notification references an unknown ticket");
                }
                ticket_state = it->second;
                if (ticket_state->presentation_notified) {
                    return fail(FRAMEGEN_STATUS_INVALID_STATE,
                                "ticket already has a presentation notification");
                }
                if (event->source_frame_id != ticket_state->current_frame_id) {
                    return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                                "presentation source frame does not match the ticket");
                }
                if (event->clock_domain != ticket_state->clock_domain) {
                    return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                                "presentation event uses a different ticket clock domain");
                }
                if (generated_disposition) {
                    const auto presentation_status =
                        validate_generated_presentation_locked(
                            *state, *ticket_state,
                            event->presentation_timestamp_ns);
                    if (presentation_status != FRAMEGEN_STATUS_OK) {
                        return presentation_status;
                    }
                }
            } else if (generated_disposition ||
                       event->disposition == FRAMEGEN_BYPASSED_GENERATED_FRAME) {
                return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                            "generated presentation and bypass events require a ticket ID");
            }
            if (event->disposition == FRAMEGEN_PRESENTED_REAL_FRAME) {
                if (event->source_frame_id == 0 ||
                    event->clock_domain != state->clock_domain) {
                    return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                                "real-frame presentation requires a source ID in the active clock domain");
                }
            } else if (event->disposition == FRAMEGEN_DROPPED_FRAME &&
                       !ticket_state && event->source_frame_id == 0) {
                return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                            "unticketed drop requires a source frame ID");
            }
            if (!ticket_state && event->disposition == FRAMEGEN_DROPPED_FRAME &&
                event->clock_domain != state->clock_domain) {
                return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                            "unticketed drop must use the active clock domain");
            }
            if (event->consumer_completion.sync_type == FRAMEGEN_SYNC_NONE_READY) {
                if (event->consumer_completion.native_handle != nullptr ||
                    event->consumer_completion.user_data != nullptr ||
                    event->consumer_completion.value != 0 ||
                    event->consumer_completion.retain != nullptr ||
                    event->consumer_completion.release != nullptr) {
                    return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                                "completed-consumer marker must not include a native point");
                }
            } else if (!ticket_state) {
                return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                            "consumer completion requires an associated generated ticket");
            }
        }

        if (event->consumer_completion.sync_type != FRAMEGEN_SYNC_NONE_READY) {
            if (event->consumer_completion.native_handle == nullptr ||
                event->consumer_completion.backend_type != state->backend_info.backend_type ||
                event->consumer_completion.device_id != state->device_id ||
                !has_paired_callbacks(event->consumer_completion.retain,
                                      event->consumer_completion.release)) {
                return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                            "consumer completion point does not match the context device");
            }
            auto imported = state->backend.import_sync(event->consumer_completion);
            if (!imported) {
                return fail(map_cpp_error(imported.error()), imported.error().message);
            }
            consumer_sync = std::move(*imported);
            if (!consumer_sync || consumer_sync->backend_id() !=
                    state->backend.backend->backend_id() ||
                consumer_sync->device_id() != state->device_id ||
                !consumer_sync->is_signaled().has_value()) {
                return fail(FRAMEGEN_STATUS_UNSUPPORTED_RESOURCE,
                            "consumer completion must support nonblocking signal queries");
            }
            consumer_lease = lease_handle(event->consumer_completion.native_handle,
                                          event->consumer_completion.user_data,
                                          event->consumer_completion.retain,
                                          event->consumer_completion.release);
        }

        {
            std::scoped_lock lock(state->mutex);
            if (state->destroyed) {
                return fail(FRAMEGEN_STATUS_INVALID_STATE,
                            "context has been destroyed");
            }
            if (ticket_state) {
                const auto it = state->tickets.find(ticket_state->id);
                if (it == state->tickets.end() || it->second != ticket_state ||
                    ticket_state->presentation_notified) {
                    return fail(FRAMEGEN_STATUS_STALE_FRAME,
                                "ticket changed before presentation notification commit");
                }
                if (event->clock_domain != ticket_state->clock_domain) {
                    return fail(FRAMEGEN_STATUS_STALE_FRAME,
                                "ticket clock domain changed before notification commit");
                }
                if (generated_disposition) {
                    const auto presentation_status =
                        validate_generated_presentation_locked(
                            *state, *ticket_state,
                            event->presentation_timestamp_ns);
                    if (presentation_status != FRAMEGEN_STATUS_OK) {
                        return presentation_status;
                    }
                }
                ticket_state->presentation_notified = true;
                ticket_state->consumer_lease = consumer_lease;
                ticket_state->consumer_completion = consumer_sync;
                ticket_state->consumer_finished = !consumer_sync ||
                    consumer_sync->is_signaled().value_or(false);
                if (event->disposition == FRAMEGEN_PRESENTED_GENERATED_FRAME) {
                    ++state->statistics.generated_frames_presented;
                } else if (event->disposition == FRAMEGEN_BYPASSED_GENERATED_FRAME ||
                           event->disposition == FRAMEGEN_DROPPED_FRAME) {
                    if (ticket_state->status == FRAMEGEN_TICKET_PENDING ||
                        ticket_state->status == FRAMEGEN_TICKET_READY) {
                        if (event->presentation_timestamp_ns >=
                            ticket_state->deadline_ns) {
                            ticket_state->status = FRAMEGEN_TICKET_DEADLINE_MISSED;
                            ++state->statistics.deadline_misses;
                        } else {
                            ticket_state->status = FRAMEGEN_TICKET_BYPASSED;
                        }
                    }
                    if (event->disposition == FRAMEGEN_BYPASSED_GENERATED_FRAME) {
                        ++state->statistics.bypassed_frames;
                    }
                }
            } else if (event->clock_domain != state->clock_domain) {
                return fail(FRAMEGEN_STATUS_STALE_FRAME,
                            "presentation history changed before notification commit");
            }
            if (event->disposition == FRAMEGEN_PRESENTED_REAL_FRAME) {
                ++state->statistics.real_frames_presented;
            }
            retire_released_tickets_locked(*state);
            if (ticket_state) {
                const auto retained = state->tickets.find(ticket_state->id);
                needs_background_retirement = retained != state->tickets.end() &&
                    retained->second->client_released &&
                    retained->second->presentation_notified;
            }
        }
        if (state->backend.notify_presentation) {
            try {
                state->backend.notify_presentation(*event);
            } catch (...) {
                // The notification is advisory to backend policy. Public
                // state is already committed and cannot be rolled back.
            }
        }
        if (needs_background_retirement) {
            enqueue_for_retirement(state);
        }
        return succeed();
    });
}

framegen_status_t framegen_context_reset_history(framegen_context_t* context) {
    return guarded([&] {
        if (context == nullptr || !context->impl) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT, "context must not be null");
        }
        auto state = context->impl;
        std::unique_lock backend_operation_lock(state->backend_operation_mutex);
        std::deque<std::shared_ptr<FrameRecord>> old_frames;
        {
            std::scoped_lock lock(state->mutex);
            const auto context_status = check_context(state);
            if (context_status != FRAMEGEN_STATUS_OK) {
                return context_status;
            }
            if (state->history_generation == UINT64_MAX) {
                return fail(FRAMEGEN_STATUS_INTERNAL_ERROR,
                            "history generation sequence is exhausted");
            }
            ++state->history_generation;
            old_frames.swap(state->frames);
            state->stream_id = 0;
            state->clock_domain = 0;
            for (auto& [id, ticket] : state->tickets) {
                (void)id;
                if (ticket->history_generation < state->history_generation) {
                    ticket->status = FRAMEGEN_TICKET_INVALIDATED;
                    ticket->error_status = FRAMEGEN_STATUS_STALE_FRAME;
                }
            }
        }
        if (state->backend.invalidate) {
            try {
                state->backend.invalidate(FRAMEGEN_INVALIDATE_HISTORY_RESET);
            } catch (...) {
                // History reset was committed; backend hooks cannot roll it back.
            }
        }
        return succeed();
    });
}

framegen_status_t framegen_context_invalidate(
    framegen_context_t* context, framegen_invalidation_reason_t reason,
    std::uint64_t* out_history_generation) {
    return guarded([&] {
        if (context == nullptr || !context->impl || out_history_generation == nullptr ||
            reason < FRAMEGEN_INVALIDATE_RESIZE ||
            (reason > FRAMEGEN_INVALIDATE_DEVICE_LOSS &&
             reason != FRAMEGEN_INVALIDATE_HISTORY_RESET)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "invalidation requires a context, known reason, and generation output");
        }
        auto state = context->impl;
        std::unique_lock backend_operation_lock(state->backend_operation_mutex);
        std::deque<std::shared_ptr<FrameRecord>> old_frames;
        {
            std::scoped_lock lock(state->mutex);
            if (state->destroyed) {
                return fail(FRAMEGEN_STATUS_INVALID_STATE, "context has been destroyed");
            }
            if (state->history_generation == UINT64_MAX) {
                return fail(FRAMEGEN_STATUS_INTERNAL_ERROR,
                            "history generation sequence is exhausted");
            }
            ++state->history_generation;
            old_frames.swap(state->frames);
            state->stream_id = 0;
            state->clock_domain = 0;
            if (reason == FRAMEGEN_INVALIDATE_DEVICE_LOSS) {
                state->device_lost = true;
            }
            for (auto& [id, ticket] : state->tickets) {
                (void)id;
                if (ticket->history_generation < state->history_generation) {
                    ticket->status = reason == FRAMEGEN_INVALIDATE_DEVICE_LOSS
                        ? FRAMEGEN_TICKET_FAILED : FRAMEGEN_TICKET_INVALIDATED;
                    ticket->error_status = reason == FRAMEGEN_INVALIDATE_DEVICE_LOSS
                        ? FRAMEGEN_STATUS_DEVICE_LOST : FRAMEGEN_STATUS_STALE_FRAME;
                }
            }
            *out_history_generation = state->history_generation;
        }
        if (state->backend.invalidate) {
            try {
                state->backend.invalidate(reason);
            } catch (...) {
                // Invalidation was committed; backend hooks cannot roll it back.
            }
        }
        return succeed();
    });
}

framegen_status_t framegen_context_collect_statistics(
    framegen_context_t* context, framegen_statistics_t* out_statistics) {
    return guarded([&] {
        if (context == nullptr || !valid_output_struct(out_statistics)) {
            return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                        "statistics collection requires a context and valid output");
        }
        auto state = context->impl;
        std::scoped_lock lock(state->mutex);
        if (state->destroyed) {
            return fail(FRAMEGEN_STATUS_INVALID_STATE, "context has been destroyed");
        }
        retire_released_tickets_locked(*state);
        const auto size = out_statistics->struct_size;
        initialize_output(*out_statistics);
        out_statistics->source_frames_submitted =
            state->statistics.source_frames_submitted;
        out_statistics->interpolation_requests =
            state->statistics.interpolation_requests;
        out_statistics->generated_frames_ready =
            state->statistics.generated_frames_ready;
        out_statistics->bypassed_frames = state->statistics.bypassed_frames;
        out_statistics->deadline_misses = state->statistics.deadline_misses;
        out_statistics->failed_requests = state->statistics.failed_requests;
        out_statistics->real_frames_presented =
            state->statistics.real_frames_presented;
        out_statistics->generated_frames_presented =
            state->statistics.generated_frames_presented;
        out_statistics->active_in_flight_requests = state->active_in_flight;
        out_statistics->total_cpu_enqueue_time_ns =
            state->statistics.total_cpu_enqueue_time_ns;
        out_statistics->gpu_execution_time_ns =
            state->statistics.gpu_execution_time_ns;
        out_statistics->gpu_execution_time_samples =
            state->statistics.gpu_execution_time_samples;
        out_statistics->struct_size = size;
        return succeed();
    });
}

framegen_status_t framegen_get_last_error(std::uint32_t capacity, char* buffer,
                                          std::uint32_t* out_required_size) {
    if (out_required_size == nullptr || (capacity != 0 && buffer == nullptr)) {
        return fail(FRAMEGEN_STATUS_INVALID_ARGUMENT,
                    "last-error query requires a size output and valid buffer");
    }
    const auto required = static_cast<std::uint32_t>(g_last_error_length + 1);
    *out_required_size = required;
    if (capacity != 0) {
        const auto copied = std::min<std::uint32_t>(
            capacity - 1, static_cast<std::uint32_t>(g_last_error_length));
        std::memcpy(buffer, g_last_error.data(), copied);
        buffer[copied] = '\0';
    }
    return FRAMEGEN_STATUS_OK;
}

} // extern "C"
