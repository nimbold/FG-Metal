#include "framegen/api.h"

#include "backend_registry.hpp"
#include "framegen/backend.hpp"
#include "framegen/texture.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

using namespace framegen;

constexpr std::uint64_t kDeviceId = 77;
constexpr char kColorBackend[] = "org.framegen.test.color-only";
constexpr char kMotionBackend[] = "org.framegen.test.motion";
constexpr char kLateBackend[] = "org.framegen.test.late";
constexpr char kHdrBackend[] = "org.framegen.test.hdr";

class TestFailure final : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct LeaseCounts {
    std::atomic<std::uint32_t> retains{};
    std::atomic<std::uint32_t> releases{};
};

void retain_test_handle(void* user_data, void*) {
    static_cast<LeaseCounts*>(user_data)->retains.fetch_add(1, std::memory_order_relaxed);
}

void release_test_handle(void* user_data, void*) {
    static_cast<LeaseCounts*>(user_data)->releases.fetch_add(1, std::memory_order_relaxed);
}

void check(bool condition, const char* expression, int line) {
    if (!condition) {
        throw TestFailure(std::string("line ") + std::to_string(line) +
                          ": check failed: " + expression);
    }
}

#define CHECK(expression) check(static_cast<bool>(expression), #expression, __LINE__)

template <typename T>
void init(T& value) {
    value = {};
    value.struct_size = sizeof(T);
    value.struct_version = FRAMEGEN_ABI_VERSION;
}

class TestTextureResource final : public TextureResource {
public:
    TestTextureResource(TextureDescriptor descriptor, std::string backend,
                        const void* identity = nullptr)
        : descriptor_(descriptor), backend_(std::move(backend)), identity_(identity) {}

    TextureDescriptor descriptor() const noexcept override { return descriptor_; }
    std::string_view backend_id() const noexcept override { return backend_; }
    std::uint64_t device_id() const noexcept override { return kDeviceId; }
    const void* resource_identity() const noexcept override {
        return identity_ == nullptr ? this : identity_;
    }

private:
    TextureDescriptor descriptor_;
    std::string backend_;
    const void* identity_{};
};

class TestSync final : public GpuSyncPoint {
public:
    explicit TestSync(std::string backend, const std::atomic<bool>* signaled = nullptr)
        : backend_(std::move(backend)), signaled_(signaled) {}
    std::string_view backend_id() const noexcept override { return backend_; }
    std::uint64_t device_id() const noexcept override { return kDeviceId; }
    std::optional<bool> is_signaled() const noexcept override {
        return signaled_ != nullptr && signaled_->load(std::memory_order_acquire);
    }

private:
    std::string backend_;
    const std::atomic<bool>* signaled_{};
};

class TestCompletion final : public GpuCompletion {
public:
    explicit TestCompletion(std::string backend,
                            std::shared_ptr<std::atomic<bool>> complete)
        : backend_(std::move(backend)), complete_(std::move(complete)) {}
    std::string_view backend_id() const noexcept override { return backend_; }
    std::uint64_t device_id() const noexcept override { return kDeviceId; }
    bool is_complete() const noexcept override {
        return complete_->load(std::memory_order_acquire);
    }
    Result<void> wait() override {
        complete_->store(true, std::memory_order_release);
        return {};
    }

private:
    std::string backend_;
    std::shared_ptr<std::atomic<bool>> complete_;
};

struct BackendControl {
    std::shared_ptr<std::atomic<bool>> complete_immediately =
        std::make_shared<std::atomic<bool>>(true);
    std::uint32_t submit_count{};
    std::uint32_t last_dependency_count{};
    float last_interpolation{};
    std::int64_t last_interpolation_timestamp{};
    std::int64_t last_presentation_timestamp{};
    std::uint32_t last_transfer_function{};
    std::uint32_t last_dynamic_range{};
    bool last_motion_input_valid{};
    std::uint32_t last_motion_pixel_format{};
    std::atomic<std::uint32_t> last_invalidation_reason{};
    bool corrupt_exported_image{};
    bool corrupt_exported_sync{};
    std::mutex lifecycle_mutex;
    std::condition_variable lifecycle_condition;
    bool block_submit{};
    bool submit_entered{};
    bool unblock_submit{};
    std::vector<std::uint32_t> lifecycle_events;
};

class TestBackend final : public FrameGenerationBackend {
public:
    TestBackend(std::string backend, std::shared_ptr<BackendControl> control)
        : backend_(std::move(backend)), control_(std::move(control)) {}

    std::string_view backend_id() const noexcept override { return backend_; }
    std::uint64_t device_id() const noexcept override { return kDeviceId; }
    Result<GeneratedFrame> submit(const FrameSubmission& submission) override {
        {
            std::unique_lock lock(control_->lifecycle_mutex);
            if (control_->block_submit) {
                control_->submit_entered = true;
                control_->lifecycle_condition.notify_all();
                control_->lifecycle_condition.wait(lock, [&] {
                    return control_->unblock_submit;
                });
                control_->lifecycle_events.push_back(1);
                control_->block_submit = false;
            }
        }
        ++control_->submit_count;
        control_->last_dependency_count =
            static_cast<std::uint32_t>(submission.gpu_dependencies.size());
        control_->last_interpolation = submission.interpolation;
        control_->last_interpolation_timestamp = submission.interpolation_timestamp_ns;
        control_->last_presentation_timestamp =
            submission.desired_presentation_timestamp_ns;
        control_->last_transfer_function = static_cast<std::uint32_t>(
            submission.current.color_metadata.transfer_function);
        control_->last_dynamic_range = static_cast<std::uint32_t>(
            submission.current.color_metadata.dynamic_range);
        control_->last_motion_input_valid =
            submission.current.optional_inputs.motion_vectors.valid();
        if (control_->last_motion_input_valid) {
            control_->last_motion_pixel_format = static_cast<std::uint32_t>(
                submission.current.optional_inputs.motion_vectors.descriptor().format);
        }
        auto texture = Texture::from_resource(std::make_shared<TestTextureResource>(
            submission.previous.texture.descriptor(), backend_));
        if (!texture) {
            return std::unexpected(texture.error());
        }
        return GeneratedFrame{
            .texture = std::move(*texture),
            .completion = std::make_shared<TestCompletion>(
                backend_, control_->complete_immediately),
        };
    }

private:
    std::string backend_;
    std::shared_ptr<BackendControl> control_;
};

std::optional<PixelFormat> cpp_pixel_format(std::uint32_t value) {
    switch (value) {
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

ColorSpace cpp_color_space(const framegen_image_t& image) {
    if (image.color_space == FRAMEGEN_COLOR_SPACE_DISPLAY_P3) {
        return ColorSpace::display_p3;
    }
    if (image.color_space == FRAMEGEN_COLOR_SPACE_REC2020) {
        if (image.transfer_function == FRAMEGEN_TRANSFER_PQ) return ColorSpace::rec2020_pq;
        if (image.transfer_function == FRAMEGEN_TRANSFER_HLG) return ColorSpace::rec2020_hlg;
        return ColorSpace::rec2020;
    }
    if (image.color_space == FRAMEGEN_COLOR_SPACE_UNKNOWN) return ColorSpace::unknown;
    return image.transfer_function == FRAMEGEN_TRANSFER_LINEAR
        ? ColorSpace::linear_srgb : ColorSpace::srgb;
}

AlphaMode cpp_alpha_mode(std::uint32_t alpha) {
    if (alpha == FRAMEGEN_ALPHA_OPAQUE) return AlphaMode::opaque;
    if (alpha == FRAMEGEN_ALPHA_STRAIGHT) return AlphaMode::straight;
    if (alpha == FRAMEGEN_ALPHA_UNKNOWN) return AlphaMode::unknown;
    return AlphaMode::premultiplied;
}

class TestProvider final : public detail::BackendProvider {
public:
    TestProvider(std::string id, framegen_capability_flags_t supported,
                 framegen_capability_flags_t required,
                 framegen_capability_flags_t required_inputs,
                 std::shared_ptr<BackendControl> control)
        : id_(std::move(id)), supported_(supported), required_(required),
          required_inputs_(required_inputs), control_(std::move(control)) {}

    framegen_backend_info_t info() const override {
        framegen_backend_info_t result{};
        init(result);
        std::strncpy(result.backend_id, id_.c_str(), sizeof(result.backend_id) - 1);
        result.backend_type = FRAMEGEN_GPU_BACKEND_TEST;
        result.available = 1;
        result.supported_capabilities = supported_;
        result.required_capabilities = required_;
        result.required_input_capabilities = required_inputs_;
        result.supported_pixel_formats =
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_R8_UNORM) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_RG8_UNORM) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM_SRGB) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM_SRGB) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_RGB10A2_UNORM) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_BGR10A2_UNORM) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_RG11B10_FLOAT) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_RGBA16_FLOAT) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_RGBA32_FLOAT) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_R16_FLOAT) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_RG16_FLOAT) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_RG32_FLOAT) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_R32_FLOAT) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_D16_UNORM) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_D32_FLOAT) |
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_D24_UNORM_S8_UINT);
        result.max_width = 8192;
        result.max_height = 8192;
        result.max_in_flight_requests = id_ == kLateBackend ? 1 : 2;
        return result;
    }

    Result<detail::BackendInstance> create(
        const framegen_device_ref_t& device) const override {
        if (device.native_handle == nullptr ||
            device.backend_type != FRAMEGEN_GPU_BACKEND_TEST) {
            return std::unexpected(Error{ErrorCode::invalid_argument,
                                         "test backend requires a test device handle"});
        }
        auto backend = std::make_shared<TestBackend>(id_, control_);
        detail::BackendInstance instance;
        instance.backend = backend;
        instance.import_image = [id = id_](const framegen_image_t& image)
            -> Result<Texture> {
            const auto format = cpp_pixel_format(image.pixel_format);
            if (!format || image.resource.device_id != kDeviceId) {
                return std::unexpected(Error{ErrorCode::incompatible_resource,
                    "test backend received invalid image metadata"});
            }
            TextureDescriptor descriptor{
                .width = image.width,
                .height = image.height,
                .format = *format,
                .color_space = cpp_color_space(image),
                .alpha_mode = cpp_alpha_mode(image.alpha_mode),
            };
            const auto identity = image.resource.resource_identity == 0
                ? image.resource.native_handle
                : reinterpret_cast<const void*>(static_cast<std::uintptr_t>(
                      image.resource.resource_identity));
            return Texture::from_resource(std::make_shared<TestTextureResource>(
                descriptor, id, identity));
        };
        instance.import_sync = [id = id_](const framegen_sync_point_t& sync)
            -> Result<std::shared_ptr<const GpuSyncPoint>> {
            if (sync.backend_type != FRAMEGEN_GPU_BACKEND_TEST ||
                sync.device_id != kDeviceId || sync.sync_type != FRAMEGEN_SYNC_TEST ||
                sync.native_handle == nullptr || sync.value == 0) {
                return std::unexpected(Error{ErrorCode::invalid_argument,
                    "test backend received an invalid sync point"});
            }
            return std::shared_ptr<const GpuSyncPoint>(std::make_shared<TestSync>(
                id, static_cast<const std::atomic<bool>*>(sync.native_handle)));
        };
        instance.export_image = [control = control_](const Texture& texture)
            -> Result<framegen_image_t> {
            framegen_image_t result{};
            init(result);
            result.resource.struct_size = sizeof(result.resource);
            result.resource.struct_version = FRAMEGEN_ABI_VERSION;
            result.resource.backend_type = FRAMEGEN_GPU_BACKEND_TEST;
            result.resource.resource_type = FRAMEGEN_RESOURCE_TEXTURE_2D;
            result.resource.device_id = kDeviceId;
            result.resource.resource_identity = static_cast<std::uint64_t>(
                reinterpret_cast<std::uintptr_t>(texture.resource_identity()));
            result.resource.native_handle = const_cast<void*>(texture.resource_identity());
            const auto descriptor = texture.descriptor();
            result.width = descriptor.width;
            result.height = descriptor.height;
            result.pixel_format = static_cast<std::uint32_t>(descriptor.format);
            result.color_space = FRAMEGEN_COLOR_SPACE_SRGB;
            result.transfer_function = FRAMEGEN_TRANSFER_LINEAR;
            result.dynamic_range = FRAMEGEN_DYNAMIC_RANGE_SDR;
            result.alpha_mode = FRAMEGEN_ALPHA_PREMULTIPLIED;
            if (control->corrupt_exported_image) {
                ++result.resource.device_id;
            }
            return result;
        };
        instance.export_sync = [control = control_](const GpuSyncPoint& sync)
            -> Result<framegen_sync_point_t> {
            framegen_sync_point_t result{};
            init(result);
            result.backend_type = FRAMEGEN_GPU_BACKEND_TEST;
            result.sync_type = FRAMEGEN_SYNC_TEST;
            result.device_id = sync.device_id();
            result.value = 1;
            result.native_handle = const_cast<GpuSyncPoint*>(&sync);
            if (control->corrupt_exported_sync) {
                ++result.device_id;
            }
            return result;
        };
        instance.invalidate = [control = control_](framegen_invalidation_reason_t reason) {
            control->last_invalidation_reason.store(reason, std::memory_order_release);
            std::scoped_lock lock(control->lifecycle_mutex);
            control->lifecycle_events.push_back(2);
            control->lifecycle_condition.notify_all();
        };
        instance.notify_presentation = [](const framegen_presentation_event_t&) {};
        return instance;
    }

private:
    std::string id_;
    framegen_capability_flags_t supported_{};
    framegen_capability_flags_t required_{};
    framegen_capability_flags_t required_inputs_{};
    std::shared_ptr<BackendControl> control_;
};

struct RegisteredBackends {
    std::shared_ptr<BackendControl> color = std::make_shared<BackendControl>();
    std::shared_ptr<BackendControl> motion = std::make_shared<BackendControl>();
    std::shared_ptr<BackendControl> late = std::make_shared<BackendControl>();
    std::shared_ptr<BackendControl> hdr = std::make_shared<BackendControl>();

    RegisteredBackends() {
        detail::register_backend_provider(std::make_shared<TestProvider>(
            kColorBackend, FRAMEGEN_CAP_COLOR_ONLY |
                FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME,
            FRAMEGEN_CAP_COLOR_ONLY, 0, color));
        detail::register_backend_provider(std::make_shared<TestProvider>(
            kMotionBackend, FRAMEGEN_CAP_MOTION_VECTORS |
                FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME,
            0, FRAMEGEN_CAP_MOTION_VECTORS, motion));
        late->complete_immediately->store(false, std::memory_order_release);
        detail::register_backend_provider(std::make_shared<TestProvider>(
            kLateBackend, FRAMEGEN_CAP_COLOR_ONLY |
                FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME,
            FRAMEGEN_CAP_COLOR_ONLY, 0, late));
        detail::register_backend_provider(std::make_shared<TestProvider>(
            kHdrBackend, FRAMEGEN_CAP_COLOR_ONLY |
                FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME | FRAMEGEN_CAP_HDR,
            FRAMEGEN_CAP_COLOR_ONLY | FRAMEGEN_CAP_HDR, 0, hdr));
    }
} g_backends;

framegen_image_t image(void* native, std::uint64_t identity,
                       std::uint32_t width = 640,
                       std::uint32_t height = 360,
                       std::uint32_t format = FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM) {
    framegen_image_t result{};
    init(result);
    result.resource.struct_size = sizeof(result.resource);
    result.resource.struct_version = FRAMEGEN_ABI_VERSION;
    result.resource.backend_type = FRAMEGEN_GPU_BACKEND_TEST;
    result.resource.resource_type = FRAMEGEN_RESOURCE_TEXTURE_2D;
    result.resource.device_id = kDeviceId;
    result.resource.resource_identity = identity;
    result.resource.native_handle = native;
    result.width = width;
    result.height = height;
    result.pixel_format = format;
    result.color_space = FRAMEGEN_COLOR_SPACE_SRGB;
    result.transfer_function = FRAMEGEN_TRANSFER_LINEAR;
    result.dynamic_range = FRAMEGEN_DYNAMIC_RANGE_SDR;
    result.alpha_mode = FRAMEGEN_ALPHA_PREMULTIPLIED;
    return result;
}

framegen_source_frame_t source(std::uint64_t id, std::uint64_t native_id,
                               std::uint64_t stream = 1,
                               std::int64_t timestamp = 1'000'000,
                               std::uint32_t width = 640,
                               std::uint32_t height = 360,
                               std::uint32_t format = FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM) {
    framegen_source_frame_t result{};
    init(result);
    result.optional_inputs.struct_size = sizeof(result.optional_inputs);
    result.optional_inputs.struct_version = FRAMEGEN_ABI_VERSION;
    result.stream_id = stream;
    result.source_frame_id = id;
    result.color = image(reinterpret_cast<void*>(static_cast<std::uintptr_t>(native_id)),
                         native_id, width, height, format);
    result.clock_domain = 9;
    result.source_timestamp_ns = timestamp;
    result.source_duration_ns = 1'000'000;
    result.render_completion_timestamp_ns = timestamp ==
            std::numeric_limits<std::int64_t>::max()
        ? FRAMEGEN_TIMESTAMP_UNKNOWN : timestamp + 100;
    result.desired_presentation_timestamp_ns = timestamp;
    init(result.render_completion);
    result.render_completion.backend_type = FRAMEGEN_GPU_BACKEND_TEST;
    result.render_completion.device_id = kDeviceId;
    result.render_completion.sync_type = FRAMEGEN_SYNC_NONE_READY;
    return result;
}

framegen_context_t* create_context(const char* id) {
    framegen_context_desc_t desc{};
    init(desc);
    desc.backend_id = id;
    init(desc.device);
    desc.device.backend_type = FRAMEGEN_GPU_BACKEND_TEST;
    desc.device.resource_type = FRAMEGEN_RESOURCE_DEVICE;
    desc.device.device_id = kDeviceId;
    desc.device.native_handle = reinterpret_cast<void*>(0x1234);
    framegen_context_t* context{};
    CHECK(framegen_context_create(&desc, &context) == FRAMEGEN_STATUS_OK);
    CHECK(context != nullptr);
    return context;
}

framegen_capability_result_t configure(framegen_context_t* context,
                                       framegen_capability_flags_t preferred,
                                       framegen_capability_flags_t required = 0,
                                       framegen_capability_flags_t available = 0,
                                       std::uint32_t history_capacity = 4) {
    framegen_context_config_t config{};
    init(config);
    config.preferred_capabilities = preferred;
    config.required_capabilities = required;
    config.available_input_capabilities = available;
    config.max_in_flight_requests = 0;
    config.history_capacity = history_capacity;
    framegen_capability_result_t result{};
    init(result);
    CHECK(framegen_context_configure(context, &config, &result) == FRAMEGEN_STATUS_OK);
    return result;
}

framegen_frame_receipt_t submit(framegen_context_t* context,
                                const framegen_source_frame_t& frame) {
    framegen_frame_receipt_t receipt{};
    init(receipt);
    const auto status = framegen_submit_source_frame(context, &frame, &receipt);
    CHECK(status == FRAMEGEN_STATUS_OK);
    return receipt;
}

framegen_interpolation_request_t request_for(
    const framegen_frame_receipt_t& previous,
    const framegen_frame_receipt_t& current,
    std::uint64_t clock_domain = 9,
    std::int64_t target = 1'500'000,
    std::int64_t deadline = 1'900'000) {
    framegen_interpolation_request_t request{};
    init(request);
    request.previous_source_frame_id = previous.source_frame_id;
    request.current_source_frame_id = current.source_frame_id;
    request.history_generation = current.history_generation;
    request.clock_domain = clock_domain;
    request.interpolation_timestamp_ns = target;
    request.desired_presentation_timestamp_ns = target;
    request.presentation_deadline_ns = deadline;
    return request;
}

void test_abi_and_capability_query() {
    framegen_abi_info_t abi{};
    init(abi);
    CHECK(framegen_get_abi_info(&abi) == FRAMEGEN_STATUS_OK);
    CHECK(abi.abi_version == FRAMEGEN_ABI_VERSION);
    CHECK(abi.stability == FRAMEGEN_ABI_PROVISIONAL);

    framegen_backend_info_t info{};
    init(info);
    CHECK(framegen_query_backend(kColorBackend, &info) == FRAMEGEN_STATUS_OK);
    CHECK(info.supported_capabilities & FRAMEGEN_CAP_COLOR_ONLY);
    CHECK(info.supported_capabilities & FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
    CHECK(info.required_input_capabilities == 0);
    std::uint32_t count{};
    CHECK(framegen_enumerate_backends(0, 0, nullptr, &count) == FRAMEGEN_STATUS_OK);
    CHECK(count >= 3);
    constexpr std::uint32_t stride = sizeof(framegen_backend_info_t) + 16;
    std::vector<std::byte> entries(stride);
    std::uint32_t total{};
    CHECK(framegen_enumerate_backends(1, stride, entries.data(), &total) ==
          FRAMEGEN_STATUS_OK);
    framegen_backend_info_t first{};
    std::memcpy(&first, entries.data(), sizeof(first));
    CHECK(first.struct_size == sizeof(framegen_backend_info_t));
    CHECK(total == count);

    framegen_context_desc_t invalid_desc{};
    init(invalid_desc);
    framegen_context_t* invalid_context = reinterpret_cast<framegen_context_t*>(1);
    CHECK(framegen_context_create(&invalid_desc, &invalid_context) ==
          FRAMEGEN_STATUS_INVALID_ARGUMENT);
    CHECK(invalid_context == nullptr);
    framegen_interpolation_request_t invalid_request{};
    init(invalid_request);
    framegen_ticket_t* invalid_ticket = reinterpret_cast<framegen_ticket_t*>(1);
    CHECK(framegen_request_interpolation(nullptr, &invalid_request, &invalid_ticket) ==
          FRAMEGEN_STATUS_INVALID_ARGUMENT);
    CHECK(invalid_ticket == nullptr);
}

void test_color_only_submission_and_async_output() {
    auto* context = create_context(kColorBackend);
    const auto caps = configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME, 0,
        FRAMEGEN_INPUT_MOTION_VECTORS);
    CHECK(caps.negotiated_capabilities & FRAMEGEN_CAP_COLOR_ONLY);

    auto first = source(1, 101);
    auto second = source(2, 102, 1, 2'000'000);
    auto attach_unnegotiated_motion = [](framegen_source_frame_t& frame,
                                         std::uint64_t native_id) {
        frame.optional_inputs.present_mask = FRAMEGEN_INPUT_MOTION_VECTORS;
        frame.optional_inputs.motion_vectors = image(
            reinterpret_cast<void*>(static_cast<std::uintptr_t>(native_id)), native_id,
            640, 360, FRAMEGEN_PIXEL_FORMAT_RG16_FLOAT);
        frame.optional_inputs.motion_vectors.color_space = FRAMEGEN_COLOR_SPACE_UNKNOWN;
        frame.optional_inputs.motion_vectors.transfer_function = FRAMEGEN_TRANSFER_UNKNOWN;
        frame.optional_inputs.motion_vectors.dynamic_range = FRAMEGEN_DYNAMIC_RANGE_UNKNOWN;
        frame.optional_inputs.motion_vectors.alpha_mode = FRAMEGEN_ALPHA_UNKNOWN;
        frame.optional_inputs.motion_vector_encoding =
            FRAMEGEN_MOTION_VECTOR_ENCODING_SIGNED_XY;
        frame.optional_inputs.motion_vector_units = FRAMEGEN_MOTION_VECTOR_UNITS_PIXELS;
        frame.optional_inputs.motion_vector_direction =
            FRAMEGEN_MOTION_VECTOR_DIRECTION_PREVIOUS_TO_CURRENT;
    };
    attach_unnegotiated_motion(first, 111);
    attach_unnegotiated_motion(second, 112);
    const auto first_receipt = submit(context, first);
    const auto second_receipt = submit(context, second);
    CHECK(first_receipt.available_input_capabilities & FRAMEGEN_CAP_COLOR_ONLY);
    CHECK(second_receipt.available_input_capabilities & FRAMEGEN_CAP_COLOR_ONLY);
    CHECK(second_receipt.available_input_capabilities & FRAMEGEN_INPUT_MOTION_VECTORS);

    auto request = request_for(first_receipt, second_receipt);
    request.desired_presentation_timestamp_ns = 1'800'000;
    framegen_ticket_t* ticket{};
    CHECK(framegen_request_interpolation(context, &request, &ticket) == FRAMEGEN_STATUS_OK);
    CHECK(ticket != nullptr);
    framegen_ticket_result_t result{};
    init(result);
    CHECK(framegen_ticket_poll(ticket, 1'600'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_READY);
    CHECK(result.generated_frame.image.resource.native_handle != nullptr);
    CHECK(result.generated_frame.completion.native_handle != nullptr);
    CHECK(result.generated_frame.previous_source_frame_id == 1);
    CHECK(result.generated_frame.current_source_frame_id == 2);
    CHECK(result.generated_frame.interpolation_timestamp_ns == 1'500'000);
    CHECK(result.generated_frame.desired_presentation_timestamp_ns == 1'800'000);
    CHECK(g_backends.color->last_interpolation == 0.5F);
    CHECK(g_backends.color->last_interpolation_timestamp == 1'500'000);
    CHECK(g_backends.color->last_presentation_timestamp == 1'800'000);
    CHECK(g_backends.color->last_transfer_function ==
          static_cast<std::uint32_t>(TransferFunction::linear));
    CHECK(!g_backends.color->last_motion_input_valid);

    framegen_presentation_event_t event{};
    init(event);
    init(event.consumer_completion);
    event.disposition = FRAMEGEN_PRESENTED_GENERATED_FRAME;
    event.ticket_id = result.generated_frame.ticket_id;
    event.source_frame_id = 2;
    event.clock_domain = 9;
    event.presentation_timestamp_ns = 1'800'000;
    std::atomic<bool> consumer_done{false};
    event.consumer_completion.backend_type = FRAMEGEN_GPU_BACKEND_TEST;
    event.consumer_completion.sync_type = FRAMEGEN_SYNC_TEST;
    event.consumer_completion.device_id = kDeviceId;
    event.consumer_completion.value = 1;
    event.consumer_completion.native_handle = &consumer_done;
    auto mismatched_event = event;
    mismatched_event.source_frame_id = 99;
    CHECK(framegen_notify_presentation(context, &mismatched_event) ==
          FRAMEGEN_STATUS_INVALID_ARGUMENT);
    CHECK(framegen_notify_presentation(context, &event) == FRAMEGEN_STATUS_OK);

    framegen_statistics_t stats{};
    init(stats);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(stats.source_frames_submitted == 2);
    CHECK(stats.interpolation_requests == 1);
    CHECK(stats.generated_frames_ready == 1);
    CHECK(stats.generated_frames_presented == 1);
    framegen_ticket_release(ticket);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(framegen_notify_presentation(context, &event) == FRAMEGEN_STATUS_INVALID_STATE);
    consumer_done.store(true, std::memory_order_release);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(framegen_notify_presentation(context, &event) == FRAMEGEN_STATUS_STALE_FRAME);
    framegen_context_destroy(context);
}

void test_invalid_dimensions_and_mismatched_history() {
    auto* context = create_context(kColorBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
    auto invalid = source(1, 201, 1, 1'000'000, 0, 360);
    framegen_frame_receipt_t receipt{};
    init(receipt);
    CHECK(framegen_submit_source_frame(context, &invalid, &receipt) ==
          FRAMEGEN_STATUS_INVALID_FRAME);

    auto first = submit(context, source(1, 202));
    auto second = submit(context, source(2, 203, 1, 2'000'000));
    auto request = request_for(first, second);
    ++request.history_generation;
    framegen_ticket_t* ticket{};
    CHECK(framegen_request_interpolation(context, &request, &ticket) ==
          FRAMEGEN_STATUS_STALE_FRAME);

    auto wrong_stream = source(3, 204, 2, 3'000'000);
    CHECK(framegen_submit_source_frame(context, &wrong_stream, &receipt) ==
          FRAMEGEN_STATUS_STALE_FRAME);
    framegen_context_destroy(context);
}

void test_full_signed_timestamp_range_is_overflow_safe() {
    auto* context = create_context(kColorBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
    auto first_frame = source(1, 250, 1,
        std::numeric_limits<std::int64_t>::min() + 1);
    auto second_frame = source(2, 251, 1,
        std::numeric_limits<std::int64_t>::max());
    first_frame.render_completion_timestamp_ns = FRAMEGEN_TIMESTAMP_UNKNOWN;
    second_frame.render_completion_timestamp_ns = FRAMEGEN_TIMESTAMP_UNKNOWN;
    const auto first = submit(context, first_frame);
    const auto second = submit(context, second_frame);
    auto request = request_for(first, second, 9, 0, 100);
    request.desired_presentation_timestamp_ns = 50;
    framegen_ticket_t* ticket{};
    CHECK(framegen_request_interpolation(context, &request, &ticket) == FRAMEGEN_STATUS_OK);
    framegen_ticket_result_t result{};
    init(result);
    CHECK(framegen_ticket_poll(ticket, 10, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_READY);
    CHECK(std::abs(g_backends.color->last_interpolation - 0.5F) < 1e-6F);
    framegen_ticket_release(ticket);
    framegen_context_destroy(context);
}

void test_unknown_timestamp_sentinel_is_reserved_for_diagnostics() {
    auto* context = create_context(kColorBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);

    framegen_frame_receipt_t receipt{};
    init(receipt);
    auto unknown_source_time = source(1, 260);
    unknown_source_time.source_timestamp_ns = FRAMEGEN_TIMESTAMP_UNKNOWN;
    CHECK(framegen_submit_source_frame(context, &unknown_source_time, &receipt) ==
          FRAMEGEN_STATUS_INVALID_FRAME);

    auto unknown_source_presentation = source(1, 261);
    unknown_source_presentation.desired_presentation_timestamp_ns =
        FRAMEGEN_TIMESTAMP_UNKNOWN;
    CHECK(framegen_submit_source_frame(context, &unknown_source_presentation,
                                       &receipt) == FRAMEGEN_STATUS_INVALID_FRAME);

    auto first_frame = source(1, 262, 1, -3'000'000);
    auto second_frame = source(2, 263, 1, -1'000'000);
    first_frame.render_completion_timestamp_ns = FRAMEGEN_TIMESTAMP_UNKNOWN;
    second_frame.render_completion_timestamp_ns = FRAMEGEN_TIMESTAMP_UNKNOWN;
    const auto first = submit(context, first_frame);
    const auto second = submit(context, second_frame);

    auto request = request_for(first, second, 9, -2'000'000, -1'400'000);
    request.desired_presentation_timestamp_ns = -1'500'000;
    auto invalid_request = request;
    framegen_ticket_t* ticket{};
    invalid_request.interpolation_timestamp_ns = FRAMEGEN_TIMESTAMP_UNKNOWN;
    CHECK(framegen_request_interpolation(context, &invalid_request, &ticket) ==
          FRAMEGEN_STATUS_INVALID_ARGUMENT);
    invalid_request = request;
    invalid_request.desired_presentation_timestamp_ns = FRAMEGEN_TIMESTAMP_UNKNOWN;
    CHECK(framegen_request_interpolation(context, &invalid_request, &ticket) ==
          FRAMEGEN_STATUS_INVALID_ARGUMENT);
    invalid_request = request;
    invalid_request.presentation_deadline_ns = FRAMEGEN_TIMESTAMP_UNKNOWN;
    CHECK(framegen_request_interpolation(context, &invalid_request, &ticket) ==
          FRAMEGEN_STATUS_INVALID_ARGUMENT);

    CHECK(framegen_request_interpolation(context, &request, &ticket) ==
          FRAMEGEN_STATUS_OK);
    framegen_ticket_result_t result{};
    init(result);
    CHECK(framegen_ticket_poll(ticket, FRAMEGEN_TIMESTAMP_UNKNOWN, &result) ==
          FRAMEGEN_STATUS_INVALID_ARGUMENT);
    CHECK(framegen_ticket_poll(ticket, -1'600'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_READY);

    framegen_presentation_event_t presentation{};
    init(presentation);
    init(presentation.consumer_completion);
    presentation.disposition = FRAMEGEN_PRESENTED_GENERATED_FRAME;
    presentation.source_frame_id = second.source_frame_id;
    presentation.ticket_id = result.ticket_id;
    presentation.clock_domain = 9;
    presentation.presentation_timestamp_ns = FRAMEGEN_TIMESTAMP_UNKNOWN;
    CHECK(framegen_notify_presentation(context, &presentation) ==
          FRAMEGEN_STATUS_INVALID_ARGUMENT);
    presentation.presentation_timestamp_ns = -1'500'000;
    CHECK(framegen_notify_presentation(context, &presentation) == FRAMEGEN_STATUS_OK);

    framegen_ticket_release(ticket);
    framegen_context_destroy(context);
}

void test_resize_format_change_and_reset_invalidate_history() {
    auto* context = create_context(kColorBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
    auto first = submit(context, source(10, 301));
    auto second = submit(context, source(11, 302, 1, 2'000'000));

    framegen_frame_receipt_t receipt{};
    init(receipt);
    auto resized = source(12, 303, 1, 3'000'000, 800, 450);
    CHECK(framegen_submit_source_frame(context, &resized, &receipt) ==
          FRAMEGEN_STATUS_INVALID_FRAME);
    std::uint64_t generation{};
    CHECK(framegen_context_invalidate(context, FRAMEGEN_INVALIDATE_RESIZE,
                                      &generation) == FRAMEGEN_STATUS_OK);
    CHECK(g_backends.color->last_invalidation_reason.load(std::memory_order_acquire) ==
          FRAMEGEN_INVALIDATE_RESIZE);
    CHECK(generation > second.history_generation);
    const auto resized_receipt = submit(context, resized);

    auto changed_format = source(13, 304, 1, 4'000'000, 800, 450,
                                 FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM);
    CHECK(framegen_submit_source_frame(context, &changed_format, &receipt) ==
          FRAMEGEN_STATUS_INVALID_FRAME);
    CHECK(framegen_context_invalidate(context, FRAMEGEN_INVALIDATE_FORMAT_CHANGE,
                                      &generation) == FRAMEGEN_STATUS_OK);
    CHECK(g_backends.color->last_invalidation_reason.load(std::memory_order_acquire) ==
          FRAMEGEN_INVALIDATE_FORMAT_CHANGE);
    const auto format_receipt = submit(context, changed_format);

    auto changed_space = source(14, 305, 1, 5'000'000, 800, 450,
                                FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM);
    changed_space.color.color_space = FRAMEGEN_COLOR_SPACE_DISPLAY_P3;
    CHECK(framegen_submit_source_frame(context, &changed_space, &receipt) ==
          FRAMEGEN_STATUS_INVALID_FRAME);
    CHECK(framegen_context_invalidate(context, FRAMEGEN_INVALIDATE_COLORSPACE_CHANGE,
                                      &generation) == FRAMEGEN_STATUS_OK);
    CHECK(g_backends.color->last_invalidation_reason.load(std::memory_order_acquire) ==
          FRAMEGEN_INVALIDATE_COLORSPACE_CHANGE);
    const auto space_receipt = submit(context, changed_space);
    auto space_second_frame = source(15, 306, 1, 6'000'000, 800, 450,
                                     FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM);
    space_second_frame.color.color_space = FRAMEGEN_COLOR_SPACE_DISPLAY_P3;
    const auto space_second_receipt = submit(context, space_second_frame);
    auto in_flight = request_for(space_receipt, space_second_receipt, 9,
                                 5'500'000, 6'900'000);
    framegen_ticket_t* old_ticket{};
    CHECK(framegen_request_interpolation(context, &in_flight, &old_ticket) ==
          FRAMEGEN_STATUS_OK);

    CHECK(framegen_context_reset_history(context) == FRAMEGEN_STATUS_OK);
    CHECK(g_backends.color->last_invalidation_reason.load(std::memory_order_acquire) ==
          FRAMEGEN_INVALIDATE_HISTORY_RESET);
    framegen_ticket_result_t invalidated{};
    init(invalidated);
    CHECK(framegen_ticket_poll(old_ticket, 6'000'000, &invalidated) ==
          FRAMEGEN_STATUS_OK);
    CHECK(invalidated.status == FRAMEGEN_TICKET_INVALIDATED);
    framegen_ticket_release(old_ticket);
    framegen_presentation_event_t bypass_old{};
    init(bypass_old);
    init(bypass_old.consumer_completion);
    bypass_old.disposition = FRAMEGEN_BYPASSED_GENERATED_FRAME;
    bypass_old.source_frame_id = space_second_receipt.source_frame_id;
    bypass_old.ticket_id = invalidated.ticket_id;
    bypass_old.clock_domain = 9;
    bypass_old.presentation_timestamp_ns = 6'100'000;
    CHECK(framegen_notify_presentation(context, &bypass_old) == FRAMEGEN_STATUS_OK);

    auto new_stream = source(16, 307, 9, 10, 800, 450,
                             FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM);
    new_stream.color.color_space = FRAMEGEN_COLOR_SPACE_DISPLAY_P3;
    const auto reset_receipt = submit(context, new_stream);
    CHECK(reset_receipt.history_generation > format_receipt.history_generation);

    auto stale_request = request_for(first, second);
    framegen_ticket_t* ticket{};
    CHECK(framegen_request_interpolation(context, &stale_request, &ticket) ==
          FRAMEGEN_STATUS_STALE_FRAME);
    CHECK(resized_receipt.history_generation < reset_receipt.history_generation);

    std::uint64_t lost_generation{};
    CHECK(framegen_context_invalidate(context, FRAMEGEN_INVALIDATE_DEVICE_LOSS,
                                      &lost_generation) == FRAMEGEN_STATUS_OK);
    framegen_context_info_t lost_info{};
    init(lost_info);
    CHECK(framegen_context_get_info(context, &lost_info) == FRAMEGEN_STATUS_OK);
    CHECK(lost_info.device_lost == 1);
    CHECK(lost_info.history_generation == lost_generation);
    framegen_context_destroy(context);
}

void test_backend_unavailable_and_capability_negotiation() {
    framegen_backend_info_t info{};
    init(info);
    CHECK(framegen_query_backend("org.framegen.not-installed", &info) ==
          FRAMEGEN_STATUS_BACKEND_UNAVAILABLE);
    framegen_context_desc_t missing_desc{};
    init(missing_desc);
    missing_desc.backend_id = "org.framegen.not-installed";
    init(missing_desc.device);
    missing_desc.device.backend_type = FRAMEGEN_GPU_BACKEND_TEST;
    missing_desc.device.resource_type = FRAMEGEN_RESOURCE_DEVICE;
    missing_desc.device.native_handle = reinterpret_cast<void*>(0x1234);
    framegen_context_t* missing_context{};
    CHECK(framegen_context_create(&missing_desc, &missing_context) ==
          FRAMEGEN_STATUS_BACKEND_UNAVAILABLE);

    auto* sdr_only = create_context(kColorBackend);
    configure(sdr_only, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
    auto hdr_frame = source(1, 390);
    hdr_frame.color.color_space = FRAMEGEN_COLOR_SPACE_REC2020;
    hdr_frame.color.transfer_function = FRAMEGEN_TRANSFER_PQ;
    hdr_frame.color.dynamic_range = FRAMEGEN_DYNAMIC_RANGE_HDR;
    hdr_frame.color.alpha_mode = FRAMEGEN_ALPHA_OPAQUE;
    framegen_frame_receipt_t hdr_receipt{};
    init(hdr_receipt);
    CHECK(framegen_submit_source_frame(sdr_only, &hdr_frame, &hdr_receipt) ==
          FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE);
    framegen_context_destroy(sdr_only);

    auto* hdr_context = create_context(kHdrBackend);
    const auto hdr_caps = configure(hdr_context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME | FRAMEGEN_CAP_HDR,
        FRAMEGEN_CAP_HDR);
    CHECK(hdr_caps.negotiated_capabilities & FRAMEGEN_CAP_HDR);
    CHECK(hdr_caps.backend_required_capabilities & FRAMEGEN_CAP_HDR);
    framegen_frame_receipt_t rejected_sdr_receipt{};
    init(rejected_sdr_receipt);
    const auto rejected_sdr = source(1, 389);
    CHECK(framegen_submit_source_frame(hdr_context, &rejected_sdr,
                                       &rejected_sdr_receipt) ==
          FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE);
    auto hdr_second = source(2, 391, 1, 2'000'000);
    hdr_second.color.color_space = FRAMEGEN_COLOR_SPACE_REC2020;
    hdr_second.color.transfer_function = FRAMEGEN_TRANSFER_PQ;
    hdr_second.color.dynamic_range = FRAMEGEN_DYNAMIC_RANGE_HDR;
    hdr_second.color.alpha_mode = FRAMEGEN_ALPHA_OPAQUE;
    const auto hdr_first_receipt = submit(hdr_context, hdr_frame);
    const auto hdr_second_receipt = submit(hdr_context, hdr_second);
    CHECK(hdr_first_receipt.available_input_capabilities & FRAMEGEN_CAP_HDR);
    auto hdr_request = request_for(hdr_first_receipt, hdr_second_receipt);
    framegen_ticket_t* hdr_ticket{};
    CHECK(framegen_request_interpolation(hdr_context, &hdr_request, &hdr_ticket) ==
          FRAMEGEN_STATUS_OK);
    framegen_ticket_result_t hdr_result{};
    init(hdr_result);
    CHECK(framegen_ticket_poll(hdr_ticket, 1'600'000, &hdr_result) ==
          FRAMEGEN_STATUS_OK);
    CHECK(hdr_result.status == FRAMEGEN_TICKET_READY);
    CHECK(g_backends.hdr->last_transfer_function ==
          static_cast<std::uint32_t>(TransferFunction::pq));
    CHECK(g_backends.hdr->last_dynamic_range ==
          static_cast<std::uint32_t>(DynamicRange::hdr));
    framegen_ticket_release(hdr_ticket);
    framegen_context_destroy(hdr_context);

    auto* context = create_context(kMotionBackend);
    framegen_context_config_t config{};
    init(config);
    config.required_capabilities = FRAMEGEN_CAP_MOTION_VECTORS;
    config.available_input_capabilities = 0;
    config.history_capacity = 4;
    framegen_capability_result_t caps{};
    init(caps);
    CHECK(framegen_context_configure(context, &config, &caps) ==
          FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE);

    config.available_input_capabilities = FRAMEGEN_CAP_MOTION_VECTORS;
    CHECK(framegen_context_configure(context, &config, &caps) == FRAMEGEN_STATUS_OK);
    CHECK(caps.negotiated_capabilities & FRAMEGEN_CAP_MOTION_VECTORS);

    auto first = source(1, 401);
    auto second = source(2, 402, 1, 2'000'000);
    framegen_frame_receipt_t receipt{};
    init(receipt);
    CHECK(framegen_submit_source_frame(context, &first, &receipt) ==
          FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE);
    auto make_motion = [](framegen_source_frame_t& frame,
                          std::uint64_t native_id, std::uint64_t identity) {
        frame.optional_inputs.present_mask = FRAMEGEN_INPUT_MOTION_VECTORS;
        frame.optional_inputs.motion_vectors = image(
            reinterpret_cast<void*>(static_cast<std::uintptr_t>(native_id)), identity,
            640, 360, FRAMEGEN_PIXEL_FORMAT_RG16_FLOAT);
        frame.optional_inputs.motion_vectors.color_space = FRAMEGEN_COLOR_SPACE_UNKNOWN;
        frame.optional_inputs.motion_vectors.transfer_function = FRAMEGEN_TRANSFER_UNKNOWN;
        frame.optional_inputs.motion_vectors.dynamic_range = FRAMEGEN_DYNAMIC_RANGE_UNKNOWN;
        frame.optional_inputs.motion_vectors.alpha_mode = FRAMEGEN_ALPHA_UNKNOWN;
        frame.optional_inputs.motion_vector_encoding =
            FRAMEGEN_MOTION_VECTOR_ENCODING_SIGNED_XY;
        frame.optional_inputs.motion_vector_units = FRAMEGEN_MOTION_VECTOR_UNITS_PIXELS;
        frame.optional_inputs.motion_vector_direction =
            FRAMEGEN_MOTION_VECTOR_DIRECTION_PREVIOUS_TO_CURRENT;
    };
    make_motion(first, 403, 403);
    make_motion(second, 404, 404);
    const auto first_receipt = submit(context, first);
    const auto second_receipt = submit(context, second);
    auto request = request_for(first_receipt, second_receipt);
    framegen_ticket_t* ticket{};
    CHECK(framegen_request_interpolation(context, &request, &ticket) ==
          FRAMEGEN_STATUS_OK);
    framegen_ticket_release(ticket);
    framegen_context_destroy(context);
}

void test_late_output_can_be_bypassed_without_waiting() {
    g_backends.late->complete_immediately->store(false, std::memory_order_release);
    auto* context = create_context(kLateBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
    const auto first = submit(context, source(1, 501));
    const auto second = submit(context, source(2, 502, 1, 2'000'000));
    auto request = request_for(first, second, 9, 1'500'000, 1'700'000);
    framegen_ticket_t* ticket{};
    CHECK(framegen_request_interpolation(context, &request, &ticket) == FRAMEGEN_STATUS_OK);

    framegen_ticket_result_t result{};
    init(result);
    CHECK(framegen_ticket_poll(ticket, 1'600'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_PENDING);
    CHECK(result.ticket_id != 0);
    CHECK(result.generated_frame.ticket_id == result.ticket_id);
    CHECK(result.generated_frame.image.resource.native_handle != nullptr);
    CHECK(result.generated_frame.completion.native_handle != nullptr);
    CHECK(framegen_ticket_poll(ticket, 1'700'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_DEADLINE_MISSED);
    CHECK(result.generated_frame.image.resource.native_handle == nullptr);

    framegen_presentation_event_t event{};
    init(event);
    init(event.consumer_completion);
    event.disposition = FRAMEGEN_BYPASSED_GENERATED_FRAME;
    event.source_frame_id = second.source_frame_id;
    event.ticket_id = result.ticket_id;
    event.clock_domain = 9;
    event.presentation_timestamp_ns = 1'700'000;
    CHECK(framegen_notify_presentation(context, &event) == FRAMEGEN_STATUS_OK);
    framegen_statistics_t stats{};
    init(stats);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(stats.deadline_misses == 1);
    CHECK(stats.bypassed_frames == 1);
    CHECK(stats.active_in_flight_requests == 1);
    g_backends.late->complete_immediately->store(true, std::memory_order_release);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(stats.active_in_flight_requests == 0);
    framegen_ticket_release(ticket);
    framegen_context_destroy(context);
}

void test_ready_output_misses_deadline_if_not_committed() {
    g_backends.late->complete_immediately->store(false, std::memory_order_release);
    auto* context = create_context(kLateBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
    const auto first = submit(context, source(41, 541));
    const auto second = submit(context, source(42, 542, 1, 2'000'000));
    auto request = request_for(first, second, 9, 1'500'000, 1'900'000);
    framegen_ticket_t* ticket{};
    CHECK(framegen_request_interpolation(context, &request, &ticket) ==
          FRAMEGEN_STATUS_OK);

    framegen_ticket_result_t result{};
    init(result);
    CHECK(framegen_ticket_poll(ticket, 1'700'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_PENDING);
    g_backends.late->complete_immediately->store(true, std::memory_order_release);
    CHECK(framegen_ticket_poll(ticket, 1'800'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_READY);
    const auto cached_ready = result;
    CHECK(framegen_ticket_poll(ticket, 1'900'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_DEADLINE_MISSED);
    CHECK(result.generated_frame.image.resource.native_handle == nullptr);

    framegen_ticket_release(ticket);

    LeaseCounts consumer_counts;
    std::atomic<bool> consumer_done{false};
    framegen_presentation_event_t cached_presentation{};
    init(cached_presentation);
    init(cached_presentation.consumer_completion);
    cached_presentation.disposition = FRAMEGEN_PRESENTED_GENERATED_FRAME;
    cached_presentation.source_frame_id =
        cached_ready.generated_frame.current_source_frame_id;
    cached_presentation.ticket_id = cached_ready.generated_frame.ticket_id;
    cached_presentation.clock_domain = 9;
    cached_presentation.presentation_timestamp_ns = 1'800'000;
    cached_presentation.consumer_completion.backend_type = FRAMEGEN_GPU_BACKEND_TEST;
    cached_presentation.consumer_completion.sync_type = FRAMEGEN_SYNC_TEST;
    cached_presentation.consumer_completion.device_id = kDeviceId;
    cached_presentation.consumer_completion.value = 1;
    cached_presentation.consumer_completion.native_handle = &consumer_done;
    cached_presentation.consumer_completion.user_data = &consumer_counts;
    cached_presentation.consumer_completion.retain = retain_test_handle;
    cached_presentation.consumer_completion.release = release_test_handle;
    CHECK(framegen_notify_presentation(context, &cached_presentation) ==
          FRAMEGEN_STATUS_INVALID_STATE);
    CHECK(consumer_counts.retains.load() == 0);
    CHECK(consumer_counts.releases.load() == 0);

    cached_presentation.disposition = FRAMEGEN_DROPPED_FRAME;
    cached_presentation.presentation_timestamp_ns = 1'900'000;
    CHECK(framegen_notify_presentation(context, &cached_presentation) ==
          FRAMEGEN_STATUS_OK);
    CHECK(consumer_counts.retains.load() == 1);
    CHECK(consumer_counts.releases.load() == 0);

    framegen_statistics_t stats{};
    init(stats);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(stats.generated_frames_presented == 0);
    CHECK(stats.bypassed_frames == 0);
    CHECK(stats.deadline_misses == 1);
    consumer_done.store(true, std::memory_order_release);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(consumer_counts.releases.load() == 1);
    framegen_context_destroy(context);
}

void test_late_presentation_timestamp_misses_deadline() {
    auto* context = create_context(kColorBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
    const auto first = submit(context, source(43, 543));
    const auto second = submit(context, source(44, 544, 1, 2'000'000));
    auto request = request_for(first, second, 9, 1'500'000, 1'900'000);
    framegen_ticket_t* ticket{};
    CHECK(framegen_request_interpolation(context, &request, &ticket) ==
          FRAMEGEN_STATUS_OK);

    framegen_ticket_result_t cached{};
    init(cached);
    CHECK(framegen_ticket_poll(ticket, 1'800'000, &cached) == FRAMEGEN_STATUS_OK);
    CHECK(cached.status == FRAMEGEN_TICKET_READY);

    framegen_presentation_event_t late_presentation{};
    init(late_presentation);
    init(late_presentation.consumer_completion);
    late_presentation.disposition = FRAMEGEN_PRESENTED_GENERATED_FRAME;
    late_presentation.source_frame_id =
        cached.generated_frame.current_source_frame_id;
    late_presentation.ticket_id = cached.generated_frame.ticket_id;
    late_presentation.clock_domain = 9;
    late_presentation.presentation_timestamp_ns = 1'900'000;
    CHECK(framegen_notify_presentation(context, &late_presentation) ==
          FRAMEGEN_STATUS_INVALID_STATE);

    framegen_ticket_result_t expired{};
    init(expired);
    CHECK(framegen_ticket_poll(ticket, 1'800'000, &expired) == FRAMEGEN_STATUS_OK);
    CHECK(expired.status == FRAMEGEN_TICKET_DEADLINE_MISSED);
    CHECK(expired.ticket_id == cached.ticket_id);

    late_presentation.disposition = FRAMEGEN_DROPPED_FRAME;
    CHECK(framegen_notify_presentation(context, &late_presentation) ==
          FRAMEGEN_STATUS_OK);
    framegen_ticket_release(ticket);

    framegen_statistics_t stats{};
    init(stats);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(stats.generated_frames_presented == 0);
    CHECK(stats.bypassed_frames == 0);
    CHECK(stats.deadline_misses == 1);

    const auto next_first = submit(context, source(45, 545, 1, 3'000'000));
    const auto next_second = submit(context, source(46, 546, 1, 4'000'000));
    auto next_request = request_for(next_first, next_second, 9, 3'500'000, 3'900'000);
    framegen_ticket_t* bypassed_ticket{};
    CHECK(framegen_request_interpolation(context, &next_request, &bypassed_ticket) ==
          FRAMEGEN_STATUS_OK);
    framegen_ticket_result_t next_result{};
    init(next_result);
    CHECK(framegen_ticket_poll(bypassed_ticket, 3'800'000, &next_result) ==
          FRAMEGEN_STATUS_OK);
    CHECK(next_result.status == FRAMEGEN_TICKET_READY);
    framegen_presentation_event_t late_bypass{};
    init(late_bypass);
    init(late_bypass.consumer_completion);
    late_bypass.disposition = FRAMEGEN_BYPASSED_GENERATED_FRAME;
    late_bypass.source_frame_id = next_second.source_frame_id;
    late_bypass.ticket_id = next_result.ticket_id;
    late_bypass.clock_domain = 9;
    late_bypass.presentation_timestamp_ns = 3'900'000;
    CHECK(framegen_notify_presentation(context, &late_bypass) == FRAMEGEN_STATUS_OK);
    CHECK(framegen_ticket_poll(bypassed_ticket, 3'800'000, &next_result) ==
          FRAMEGEN_STATUS_OK);
    CHECK(next_result.status == FRAMEGEN_TICKET_DEADLINE_MISSED);
    framegen_ticket_release(bypassed_ticket);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(stats.generated_frames_presented == 0);
    CHECK(stats.bypassed_frames == 1);
    CHECK(stats.deadline_misses == 2);
    framegen_context_destroy(context);
}

void test_retained_ticket_capacity_applies_backpressure() {
    g_backends.late->complete_immediately->store(false, std::memory_order_release);
    auto* context = create_context(kLateBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
    const auto first = submit(context, source(45, 545));
    const auto second = submit(context, source(46, 546, 1, 2'000'000));
    auto request = request_for(first, second, 9, 1'500'000, 1'900'000);
    framegen_ticket_t* first_ticket{};
    CHECK(framegen_request_interpolation(context, &request, &first_ticket) ==
          FRAMEGEN_STATUS_OK);

    framegen_ticket_result_t first_result{};
    init(first_result);
    CHECK(framegen_ticket_poll(first_ticket, 1'600'000, &first_result) ==
          FRAMEGEN_STATUS_OK);
    CHECK(first_result.status == FRAMEGEN_TICKET_PENDING);
    g_backends.late->complete_immediately->store(true, std::memory_order_release);
    CHECK(framegen_ticket_poll(first_ticket, 1'700'000, &first_result) ==
          FRAMEGEN_STATUS_OK);
    CHECK(first_result.status == FRAMEGEN_TICKET_READY);

    framegen_ticket_t* blocked_ticket = reinterpret_cast<framegen_ticket_t*>(1);
    CHECK(framegen_request_interpolation(context, &request, &blocked_ticket) ==
          FRAMEGEN_STATUS_BACKPRESSURE);
    CHECK(blocked_ticket == nullptr);

    framegen_presentation_event_t dropped{};
    init(dropped);
    init(dropped.consumer_completion);
    dropped.disposition = FRAMEGEN_DROPPED_FRAME;
    dropped.source_frame_id = second.source_frame_id;
    dropped.ticket_id = first_result.ticket_id;
    dropped.clock_domain = 9;
    dropped.presentation_timestamp_ns = 1'800'000;
    CHECK(framegen_notify_presentation(context, &dropped) == FRAMEGEN_STATUS_OK);
    framegen_ticket_release(first_ticket);

    framegen_ticket_t* second_ticket{};
    CHECK(framegen_request_interpolation(context, &request, &second_ticket) ==
          FRAMEGEN_STATUS_OK);
    framegen_ticket_result_t second_result{};
    init(second_result);
    CHECK(framegen_ticket_poll(second_ticket, 1'700'000, &second_result) ==
          FRAMEGEN_STATUS_OK);
    CHECK(second_result.status == FRAMEGEN_TICKET_READY);
    dropped.ticket_id = second_result.ticket_id;
    CHECK(framegen_notify_presentation(context, &dropped) == FRAMEGEN_STATUS_OK);
    framegen_ticket_release(second_ticket);
    framegen_context_destroy(context);
}

void test_dropped_invalidated_ticket_retains_queued_consumer() {
    g_backends.late->complete_immediately->store(false, std::memory_order_release);
    auto* context = create_context(kLateBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME, 0, 0, 2);

    LeaseCounts previous_counts;
    LeaseCounts current_counts;
    auto previous_frame = source(51, 551);
    auto current_frame = source(52, 552, 1, 2'000'000);
    previous_frame.color.resource.user_data = &previous_counts;
    previous_frame.color.resource.retain = retain_test_handle;
    previous_frame.color.resource.release = release_test_handle;
    current_frame.color.resource.user_data = &current_counts;
    current_frame.color.resource.retain = retain_test_handle;
    current_frame.color.resource.release = release_test_handle;
    const auto previous = submit(context, previous_frame);
    const auto current = submit(context, current_frame);
    auto request = request_for(previous, current, 9, 1'500'000, 1'900'000);
    framegen_ticket_t* ticket{};
    CHECK(framegen_request_interpolation(context, &request, &ticket) ==
          FRAMEGEN_STATUS_OK);
    framegen_ticket_result_t result{};
    init(result);
    CHECK(framegen_ticket_poll(ticket, 1'600'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_PENDING);

    CHECK(framegen_context_reset_history(context) == FRAMEGEN_STATUS_OK);

    LeaseCounts consumer_counts;
    std::atomic<bool> consumer_done{false};
    framegen_presentation_event_t generated{};
    init(generated);
    init(generated.consumer_completion);
    generated.disposition = FRAMEGEN_PRESENTED_GENERATED_FRAME;
    generated.source_frame_id = result.generated_frame.current_source_frame_id;
    generated.ticket_id = result.generated_frame.ticket_id;
    generated.clock_domain = 9;
    generated.presentation_timestamp_ns = 1'700'000;
    generated.consumer_completion.backend_type = FRAMEGEN_GPU_BACKEND_TEST;
    generated.consumer_completion.sync_type = FRAMEGEN_SYNC_TEST;
    generated.consumer_completion.device_id = kDeviceId;
    generated.consumer_completion.value = 1;
    generated.consumer_completion.native_handle = &consumer_done;
    generated.consumer_completion.user_data = &consumer_counts;
    generated.consumer_completion.retain = retain_test_handle;
    generated.consumer_completion.release = release_test_handle;
    CHECK(framegen_notify_presentation(context, &generated) ==
          FRAMEGEN_STATUS_STALE_FRAME);
    CHECK(consumer_counts.retains.load() == 0);
    CHECK(consumer_counts.releases.load() == 0);

    framegen_ticket_release(ticket);
    CHECK(previous_counts.releases.load() == 0);
    CHECK(current_counts.releases.load() == 0);

    generated.disposition = FRAMEGEN_DROPPED_FRAME;
    CHECK(framegen_notify_presentation(context, &generated) == FRAMEGEN_STATUS_OK);
    CHECK(consumer_counts.retains.load() == 1);
    CHECK(consumer_counts.releases.load() == 0);

    framegen_statistics_t before_retirement{};
    init(before_retirement);
    CHECK(framegen_context_collect_statistics(context, &before_retirement) ==
          FRAMEGEN_STATUS_OK);
    CHECK(before_retirement.generated_frames_presented == 0);
    CHECK(before_retirement.bypassed_frames == 0);
    CHECK(before_retirement.deadline_misses == 0);

    g_backends.late->complete_immediately->store(true, std::memory_order_release);
    framegen_statistics_t stats{};
    init(stats);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(stats.active_in_flight_requests == 0);
    CHECK(previous_counts.releases.load() == 0);
    CHECK(current_counts.releases.load() == 0);

    consumer_done.store(true, std::memory_order_release);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(consumer_counts.releases.load() == 1);
    CHECK(previous_counts.releases.load() == 1);
    CHECK(current_counts.releases.load() == 1);
    framegen_context_destroy(context);
}

void test_pending_output_can_be_presented_with_gpu_wait() {
    g_backends.late->complete_immediately->store(false, std::memory_order_release);
    auto* context = create_context(kLateBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
    const auto first = submit(context, source(55, 555));
    const auto second = submit(context, source(56, 556, 1, 2'000'000));
    auto request = request_for(first, second, 9, 1'500'000, 1'900'000);
    framegen_ticket_t* ticket{};
    CHECK(framegen_request_interpolation(context, &request, &ticket) ==
          FRAMEGEN_STATUS_OK);
    framegen_ticket_result_t result{};
    init(result);
    CHECK(framegen_ticket_poll(ticket, 1'600'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_PENDING);
    CHECK(result.generated_frame.image.resource.native_handle != nullptr);

    std::atomic<bool> consumer_done{false};
    framegen_presentation_event_t presentation{};
    init(presentation);
    init(presentation.consumer_completion);
    presentation.disposition = FRAMEGEN_PRESENTED_GENERATED_FRAME;
    presentation.source_frame_id = second.source_frame_id;
    presentation.ticket_id = result.ticket_id;
    presentation.clock_domain = 9;
    presentation.presentation_timestamp_ns = 1'700'000;
    presentation.consumer_completion.backend_type = FRAMEGEN_GPU_BACKEND_TEST;
    presentation.consumer_completion.sync_type = FRAMEGEN_SYNC_TEST;
    presentation.consumer_completion.device_id = kDeviceId;
    presentation.consumer_completion.value = 1;
    presentation.consumer_completion.native_handle = &consumer_done;
    CHECK(framegen_notify_presentation(context, &presentation) == FRAMEGEN_STATUS_OK);

    g_backends.late->complete_immediately->store(true, std::memory_order_release);
    CHECK(framegen_ticket_poll(ticket, 1'800'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_READY);
    consumer_done.store(true, std::memory_order_release);
    framegen_ticket_release(ticket);
    framegen_context_destroy(context);
}

void test_exported_output_handles_are_validated() {
    auto verify_bad_export = [](bool bad_image) {
        auto* context = create_context(kColorBackend);
        configure(context, FRAMEGEN_CAP_COLOR_ONLY |
            FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
        const auto first = submit(context, source(71, 571));
        const auto second = submit(context, source(72, 572, 1, 2'000'000));
        auto request = request_for(first, second);
        framegen_ticket_t* ticket{};
        CHECK(framegen_request_interpolation(context, &request, &ticket) ==
              FRAMEGEN_STATUS_OK);
        g_backends.color->corrupt_exported_image = bad_image;
        g_backends.color->corrupt_exported_sync = !bad_image;

        framegen_ticket_result_t result{};
        init(result);
        CHECK(framegen_ticket_poll(ticket, 1'600'000, &result) == FRAMEGEN_STATUS_OK);
        CHECK(result.status == FRAMEGEN_TICKET_FAILED);
        CHECK(result.error_status == FRAMEGEN_STATUS_BACKEND_ERROR);
        CHECK(result.generated_frame.image.resource.native_handle == nullptr);
        CHECK(result.generated_frame.completion.native_handle == nullptr);

        g_backends.color->corrupt_exported_image = false;
        g_backends.color->corrupt_exported_sync = false;
        framegen_presentation_event_t dropped{};
        init(dropped);
        init(dropped.consumer_completion);
        dropped.disposition = FRAMEGEN_DROPPED_FRAME;
        dropped.source_frame_id = second.source_frame_id;
        dropped.ticket_id = result.ticket_id;
        dropped.clock_domain = 9;
        dropped.presentation_timestamp_ns = 1'600'000;
        CHECK(framegen_notify_presentation(context, &dropped) == FRAMEGEN_STATUS_OK);
        framegen_ticket_release(ticket);
        framegen_context_destroy(context);
    };

    verify_bad_export(true);
    verify_bad_export(false);
}

void test_history_invalidation_serializes_after_backend_submit() {
    auto* context = create_context(kColorBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME);
    const auto first = submit(context, source(61, 561));
    const auto second = submit(context, source(62, 562, 1, 2'000'000));
    auto request = request_for(first, second);
    {
        std::scoped_lock lock(g_backends.color->lifecycle_mutex);
        g_backends.color->lifecycle_events.clear();
        g_backends.color->block_submit = true;
        g_backends.color->submit_entered = false;
        g_backends.color->unblock_submit = false;
    }

    framegen_ticket_t* ticket{};
    framegen_status_t request_status = FRAMEGEN_STATUS_INTERNAL_ERROR;
    std::thread submit_thread([&] {
        request_status = framegen_request_interpolation(context, &request, &ticket);
    });
    bool entered{};
    {
        std::unique_lock lock(g_backends.color->lifecycle_mutex);
        entered = g_backends.color->lifecycle_condition.wait_for(
            lock, std::chrono::seconds(2), [&] {
                return g_backends.color->submit_entered;
            });
    }
    if (!entered) {
        {
            std::scoped_lock lock(g_backends.color->lifecycle_mutex);
            g_backends.color->unblock_submit = true;
            g_backends.color->lifecycle_condition.notify_all();
        }
        submit_thread.join();
        framegen_context_destroy(context);
        throw TestFailure("backend submit did not enter the blocking test hook");
    }

    framegen_status_t reset_status = FRAMEGEN_STATUS_INTERNAL_ERROR;
    std::atomic<bool> reset_started{false};
    std::atomic<bool> reset_returned{false};
    std::thread reset_thread([&] {
        reset_started.store(true, std::memory_order_release);
        reset_status = framegen_context_reset_history(context);
        reset_returned.store(true, std::memory_order_release);
    });
    while (!reset_started.load(std::memory_order_acquire)) {
        std::this_thread::yield();
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(30));
    const bool reset_finished_while_submit_blocked =
        reset_returned.load(std::memory_order_acquire);
    {
        std::scoped_lock lock(g_backends.color->lifecycle_mutex);
        g_backends.color->unblock_submit = true;
        g_backends.color->lifecycle_condition.notify_all();
    }
    submit_thread.join();
    reset_thread.join();
    CHECK(request_status == FRAMEGEN_STATUS_OK);
    CHECK(reset_status == FRAMEGEN_STATUS_OK);
    CHECK(!reset_finished_while_submit_blocked);
    {
        std::scoped_lock lock(g_backends.color->lifecycle_mutex);
        CHECK(g_backends.color->lifecycle_events.size() == 2);
        CHECK(g_backends.color->lifecycle_events[0] == 1);
        CHECK(g_backends.color->lifecycle_events[1] == 2);
    }

    framegen_ticket_result_t result{};
    init(result);
    CHECK(framegen_ticket_poll(ticket, 1'600'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_INVALIDATED);
    framegen_presentation_event_t bypass{};
    init(bypass);
    init(bypass.consumer_completion);
    bypass.disposition = FRAMEGEN_BYPASSED_GENERATED_FRAME;
    bypass.source_frame_id = second.source_frame_id;
    bypass.ticket_id = result.ticket_id;
    bypass.clock_domain = 9;
    bypass.presentation_timestamp_ns = 1'600'000;
    CHECK(framegen_notify_presentation(context, &bypass) == FRAMEGEN_STATUS_OK);
    framegen_ticket_release(ticket);
    framegen_context_destroy(context);
}

void test_input_leases_survive_history_eviction_and_ticket_release() {
    g_backends.late->complete_immediately->store(false, std::memory_order_release);
    auto* context = create_context(kLateBackend);
    configure(context, FRAMEGEN_CAP_COLOR_ONLY |
        FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME, 0, 0, 2);

    LeaseCounts previous_counts;
    LeaseCounts current_counts;
    auto previous_frame = source(31, 531);
    auto current_frame = source(32, 532, 1, 2'000'000);
    for (auto [frame, counts] : {
             std::pair{&previous_frame, &previous_counts},
             std::pair{&current_frame, &current_counts}}) {
        frame->color.resource.user_data = counts;
        frame->color.resource.retain = retain_test_handle;
        frame->color.resource.release = release_test_handle;
    }
    const auto previous = submit(context, previous_frame);
    const auto current = submit(context, current_frame);
    auto request = request_for(previous, current, 9, 1'500'000, 1'900'000);
    framegen_ticket_t* ticket{};
    CHECK(framegen_request_interpolation(context, &request, &ticket) == FRAMEGEN_STATUS_OK);
    framegen_ticket_result_t result{};
    init(result);
    CHECK(framegen_ticket_poll(ticket, 1'600'000, &result) == FRAMEGEN_STATUS_OK);
    CHECK(result.status == FRAMEGEN_TICKET_PENDING);

    (void)submit(context, source(33, 533, 1, 3'000'000));
    (void)submit(context, source(34, 534, 1, 4'000'000));
    framegen_ticket_release(ticket);
    CHECK(previous_counts.retains.load() == 1);
    CHECK(previous_counts.releases.load() == 0);
    CHECK(current_counts.retains.load() == 1);
    CHECK(current_counts.releases.load() == 0);

    framegen_presentation_event_t bypass{};
    init(bypass);
    init(bypass.consumer_completion);
    bypass.disposition = FRAMEGEN_BYPASSED_GENERATED_FRAME;
    bypass.source_frame_id = current.source_frame_id;
    bypass.ticket_id = result.ticket_id;
    bypass.clock_domain = 9;
    bypass.presentation_timestamp_ns = 1'800'000;
    CHECK(framegen_notify_presentation(context, &bypass) == FRAMEGEN_STATUS_OK);
    g_backends.late->complete_immediately->store(true, std::memory_order_release);
    framegen_statistics_t stats{};
    init(stats);
    CHECK(framegen_context_collect_statistics(context, &stats) == FRAMEGEN_STATUS_OK);
    CHECK(stats.active_in_flight_requests == 0);
    CHECK(previous_counts.releases.load() == 1);
    CHECK(current_counts.releases.load() == 1);
    framegen_context_destroy(context);
}

using Test = std::pair<const char*, void (*)()>;

} // namespace

int main() {
    const std::vector<Test> tests{
        {"provisional C ABI and backend capability query", test_abi_and_capability_query},
        {"color-only submission and asynchronous output",
         test_color_only_submission_and_async_output},
        {"invalid dimensions and mismatched frame history",
         test_invalid_dimensions_and_mismatched_history},
        {"full signed timestamp range is overflow safe",
         test_full_signed_timestamp_range_is_overflow_safe},
        {"unknown timestamp sentinel is diagnostic only",
         test_unknown_timestamp_sentinel_is_reserved_for_diagnostics},
        {"resize, format, color-space, and reset invalidation",
         test_resize_format_change_and_reset_invalidate_history},
        {"unavailable backend and capability negotiation",
         test_backend_unavailable_and_capability_negotiation},
        {"late output bypass without waiting",
         test_late_output_can_be_bypassed_without_waiting},
        {"ready output expires at its deadline",
         test_ready_output_misses_deadline_if_not_committed},
        {"late presentation timestamps cannot commit generated output",
         test_late_presentation_timestamp_misses_deadline},
        {"retained tickets apply backpressure",
         test_retained_ticket_capacity_applies_backpressure},
        {"dropped invalidated ticket retains queued consumer",
         test_dropped_invalidated_ticket_retains_queued_consumer},
        {"pending output can be presented with a GPU wait",
         test_pending_output_can_be_presented_with_gpu_wait},
        {"backend exported handles are validated",
         test_exported_output_handles_are_validated},
        {"history invalidation follows submitted backend work",
         test_history_invalidation_serializes_after_backend_submit},
        {"input leases survive history eviction and ticket release",
         test_input_leases_survive_history_eviction_and_ticket_release},
    };

    int failures = 0;
    for (const auto& [name, test] : tests) {
        try {
            test();
            std::cout << "[pass] " << name << '\n';
        } catch (const std::exception& error) {
            ++failures;
            std::cerr << "[fail] " << name << ": " << error.what() << '\n';
        }
    }
    return failures == 0 ? 0 : 1;
}
