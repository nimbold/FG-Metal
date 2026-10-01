#include "framegen/framegen.hpp"

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace {

using namespace framegen;

constexpr std::string_view kBackendId = "org.example.framegen.fake";
constexpr std::uint64_t kDeviceId = 17;

class TestFailure final : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

void check(bool condition, const char* expression, const char* file, int line) {
    if (!condition) {
        throw TestFailure(std::string(file) + ":" + std::to_string(line) +
                          ": check failed: " + expression);
    }
}

#define CHECK(expression) check(static_cast<bool>(expression), #expression, __FILE__, __LINE__)

TextureDescriptor standard_descriptor() {
    return TextureDescriptor{
        .width = 1280,
        .height = 720,
        .format = PixelFormat::rgba16_float,
        .color_space = ColorSpace::linear_srgb,
        .alpha_mode = AlphaMode::premultiplied,
    };
}

class FakeTextureResource final : public TextureResource {
public:
    explicit FakeTextureResource(TextureDescriptor descriptor = standard_descriptor(),
                                 std::string backend = std::string(kBackendId),
                                 std::uint64_t device = kDeviceId,
                                 const void* identity = nullptr)
        : descriptor_(descriptor),
          backend_(std::move(backend)),
          device_(device),
          identity_(identity) {}

    [[nodiscard]] TextureDescriptor descriptor() const noexcept override {
        return descriptor_;
    }
    [[nodiscard]] std::string_view backend_id() const noexcept override {
        return backend_;
    }
    [[nodiscard]] std::uint64_t device_id() const noexcept override {
        return device_;
    }
    [[nodiscard]] const void* resource_identity() const noexcept override {
        return identity_ ? identity_ : this;
    }

private:
    TextureDescriptor descriptor_;
    std::string backend_;
    std::uint64_t device_;
    const void* identity_;
};

Texture make_texture(TextureDescriptor descriptor = standard_descriptor(),
                     std::string backend = std::string(kBackendId),
                     std::uint64_t device = kDeviceId,
                     const void* identity = nullptr) {
    auto texture = Texture::from_resource(std::make_shared<FakeTextureResource>(
        descriptor, std::move(backend), device, identity));
    CHECK(texture.has_value());
    return std::move(*texture);
}

class FakeSyncPoint : public GpuSyncPoint {
public:
    explicit FakeSyncPoint(std::string backend = std::string(kBackendId),
                           std::uint64_t device = kDeviceId)
        : backend_(std::move(backend)), device_(device) {}

    [[nodiscard]] std::string_view backend_id() const noexcept override {
        return backend_;
    }
    [[nodiscard]] std::uint64_t device_id() const noexcept override {
        return device_;
    }

private:
    std::string backend_;
    std::uint64_t device_;
};

class FakeCompletion final : public GpuCompletion {
public:
    explicit FakeCompletion(std::string backend = std::string(kBackendId),
                            std::uint64_t device = kDeviceId)
        : backend_(std::move(backend)), device_(device) {}

    [[nodiscard]] std::string_view backend_id() const noexcept override {
        return backend_;
    }
    [[nodiscard]] std::uint64_t device_id() const noexcept override {
        return device_;
    }
    [[nodiscard]] bool is_complete() const noexcept override { return complete_; }
    [[nodiscard]] Result<void> wait() override {
        complete_ = true;
        return {};
    }

private:
    std::string backend_;
    std::uint64_t device_;
    bool complete_{};
};

class FakeBackend final : public FrameGenerationBackend {
public:
    using SubmitFunction = std::function<Result<GeneratedFrame>(const FrameSubmission&)>;

    [[nodiscard]] std::string_view backend_id() const noexcept override {
        return backend_;
    }
    [[nodiscard]] std::uint64_t device_id() const noexcept override { return device_; }

    [[nodiscard]] Result<GeneratedFrame> submit(
        const FrameSubmission& submission) override {
        ++submit_count;
        last_reset_history = submission.reset_history;
        last_dependency_count = submission.gpu_dependencies.size();
        if (submit_function) {
            return submit_function(submission);
        }
        return GeneratedFrame{
            .texture = make_texture(submission.previous.texture.descriptor(), backend_, device_),
            .completion = std::make_shared<FakeCompletion>(backend_, device_),
        };
    }

    std::string backend_{kBackendId};
    std::uint64_t device_{kDeviceId};
    SubmitFunction submit_function;
    int submit_count{};
    bool last_reset_history{};
    std::size_t last_dependency_count{};
};

FrameGenerator make_generator(const std::shared_ptr<FakeBackend>& backend) {
    auto generator = FrameGenerator::create(backend);
    CHECK(generator.has_value());
    return std::move(*generator);
}

FrameSubmission valid_submission(TextureDescriptor descriptor = standard_descriptor()) {
    return FrameSubmission{
        .previous = FrameInput{
            .texture = make_texture(descriptor),
            .timing = FrameTiming{.sequence = 100, .timestamp_ns = 1'000'000,
                                  .clock_domain = 9},
        },
        .current = FrameInput{
            .texture = make_texture(descriptor),
            .timing = FrameTiming{.sequence = 101, .timestamp_ns = 2'000'000,
                                  .clock_domain = 9},
        },
        .interpolation = 0.5F,
    };
}

void expect_error(const Result<GeneratedFrame>& result, ErrorCode code) {
    CHECK(!result.has_value());
    CHECK(result.error().code == code);
}

void test_valid_submission_returns_distinct_texture_and_completion() {
    auto backend = std::make_shared<FakeBackend>();
    auto generator = make_generator(backend);
    auto submission = valid_submission();
    submission.gpu_dependencies.push_back(std::make_shared<FakeSyncPoint>());

    auto result = generator.submit(submission);

    CHECK(result.has_value());
    CHECK(backend->submit_count == 1);
    CHECK(backend->last_dependency_count == 1);
    CHECK(result->texture.valid());
    CHECK(result->texture.resource_identity() !=
          submission.previous.texture.resource_identity());
    CHECK(result->texture.resource_identity() !=
          submission.current.texture.resource_identity());
    CHECK(result->completion != nullptr);
    CHECK(result->completion->backend_id() == kBackendId);
    CHECK(result->completion->device_id() == kDeviceId);
    CHECK(!result->completion->is_complete());
    CHECK(result->completion->wait().has_value());
    CHECK(result->completion->is_complete());
}

void test_null_backend_and_null_inputs_are_rejected() {
    auto no_backend = FrameGenerator::create(std::shared_ptr<FrameGenerationBackend>{});
    CHECK(!no_backend.has_value());
    CHECK(no_backend.error().code == ErrorCode::invalid_argument);

    auto backend = std::make_shared<FakeBackend>();
    auto generator = make_generator(backend);

    auto missing_previous = valid_submission();
    missing_previous.previous.texture = Texture{};
    expect_error(generator.submit(missing_previous), ErrorCode::invalid_argument);

    auto missing_current = valid_submission();
    missing_current.current.texture = Texture{};
    expect_error(generator.submit(missing_current), ErrorCode::invalid_argument);
    CHECK(backend->submit_count == 0);

    CHECK(!Texture::from_resource(std::shared_ptr<TextureResource>{}).has_value());
}

void test_interpolation_requires_finite_value_in_closed_unit_interval() {
    auto backend = std::make_shared<FakeBackend>();
    auto generator = make_generator(backend);

    for (const float valid_value : {0.0F, 1.0F}) {
        auto submission = valid_submission();
        submission.interpolation = valid_value;
        CHECK(generator.submit(submission).has_value());
    }

    for (const float invalid_value : {-0.001F, 1.001F,
                                      std::numeric_limits<float>::quiet_NaN(),
                                      std::numeric_limits<float>::infinity(),
                                      -std::numeric_limits<float>::infinity()}) {
        auto submission = valid_submission();
        submission.interpolation = invalid_value;
        expect_error(generator.submit(submission), ErrorCode::invalid_argument);
    }
    CHECK(backend->submit_count == 2);
}

void test_backend_and_device_mismatches_are_rejected() {
    auto backend = std::make_shared<FakeBackend>();
    auto generator = make_generator(backend);

    auto foreign_backend = valid_submission();
    foreign_backend.current.texture = make_texture(
        standard_descriptor(), "org.example.framegen.other", kDeviceId);
    expect_error(generator.submit(foreign_backend), ErrorCode::incompatible_resource);

    auto foreign_device = valid_submission();
    foreign_device.current.texture = make_texture(standard_descriptor(),
                                                   std::string(kBackendId), kDeviceId + 1);
    expect_error(generator.submit(foreign_device), ErrorCode::incompatible_resource);

    auto foreign_optional = valid_submission();
    foreign_optional.current.optional_inputs.depth = make_texture(
        standard_descriptor(), std::string(kBackendId), kDeviceId + 1);
    expect_error(generator.submit(foreign_optional), ErrorCode::incompatible_resource);

    auto matching_foreign_pair = valid_submission();
    matching_foreign_pair.previous.texture = make_texture(
        standard_descriptor(), "org.example.framegen.other", kDeviceId);
    matching_foreign_pair.current.texture = make_texture(
        standard_descriptor(), "org.example.framegen.other", kDeviceId);
    expect_error(generator.submit(matching_foreign_pair), ErrorCode::incompatible_resource);

    auto dependency = std::make_shared<FakeSyncPoint>(
        std::string(kBackendId), kDeviceId + 1);
    auto foreign_dependency = valid_submission();
    foreign_dependency.gpu_dependencies.push_back(dependency);
    expect_error(generator.submit(foreign_dependency), ErrorCode::incompatible_resource);
    CHECK(backend->submit_count == 0);
}

void test_input_descriptors_must_match() {
    auto backend = std::make_shared<FakeBackend>();
    auto generator = make_generator(backend);

    const std::vector<TextureDescriptor> mismatches = [&] {
        std::vector<TextureDescriptor> values;
        auto description = standard_descriptor();
        description.width += 1;
        values.push_back(description);
        description = standard_descriptor();
        description.height += 1;
        values.push_back(description);
        description = standard_descriptor();
        description.format = PixelFormat::rgba8_unorm;
        values.push_back(description);
        description = standard_descriptor();
        description.color_space = ColorSpace::display_p3;
        values.push_back(description);
        description = standard_descriptor();
        description.alpha_mode = AlphaMode::straight;
        values.push_back(description);
        return values;
    }();

    for (const auto& description : mismatches) {
        auto submission = valid_submission();
        submission.current.texture = make_texture(description);
        expect_error(generator.submit(submission), ErrorCode::incompatible_resource);
    }
    CHECK(backend->submit_count == 0);
}

void test_explicit_ui_plane_requires_two_matching_layers() {
    auto backend = std::make_shared<FakeBackend>();
    auto generator = make_generator(backend);
    auto submission = valid_submission();
    submission.hud_options.mode = HudMode::explicit_ui_plane;
    expect_error(generator.submit(submission), ErrorCode::invalid_argument);
    CHECK(backend->submit_count == 0);

    const auto descriptor = submission.previous.texture.descriptor();
    submission.previous.optional_inputs.ui_texture = make_texture(descriptor);
    submission.current.optional_inputs.ui_texture = make_texture(descriptor);
    CHECK(generator.submit(submission).has_value());
    CHECK(backend->submit_count == 1);

    auto mismatched = descriptor;
    mismatched.width += 1;
    submission.current.optional_inputs.ui_texture = make_texture(mismatched);
    expect_error(generator.submit(submission), ErrorCode::incompatible_resource);
    CHECK(backend->submit_count == 1);
}

void test_timing_and_clock_domain_validation_with_history_reset() {
    auto backend = std::make_shared<FakeBackend>();
    auto generator = make_generator(backend);

    auto zero_previous_domain = valid_submission();
    zero_previous_domain.previous.timing.clock_domain = 0;
    expect_error(generator.submit(zero_previous_domain), ErrorCode::invalid_argument);

    auto zero_current_domain = valid_submission();
    zero_current_domain.current.timing.clock_domain = 0;
    expect_error(generator.submit(zero_current_domain), ErrorCode::invalid_argument);

    auto different_domain = valid_submission();
    different_domain.current.timing.clock_domain += 1;
    expect_error(generator.submit(different_domain), ErrorCode::invalid_argument);

    auto decreasing_sequence = valid_submission();
    decreasing_sequence.current.timing.sequence = decreasing_sequence.previous.timing.sequence;
    expect_error(generator.submit(decreasing_sequence), ErrorCode::invalid_argument);

    auto decreasing_timestamp = valid_submission();
    decreasing_timestamp.current.timing.timestamp_ns =
        decreasing_timestamp.previous.timing.timestamp_ns;
    expect_error(generator.submit(decreasing_timestamp), ErrorCode::invalid_argument);

    auto reset_cannot_reverse_pair = valid_submission();
    reset_cannot_reverse_pair.current.timing.sequence =
        reset_cannot_reverse_pair.previous.timing.sequence;
    reset_cannot_reverse_pair.current.timing.timestamp_ns =
        reset_cannot_reverse_pair.previous.timing.timestamp_ns - 1;
    reset_cannot_reverse_pair.reset_history = true;
    expect_error(generator.submit(reset_cannot_reverse_pair), ErrorCode::invalid_argument);

    auto reset_stream = valid_submission();
    reset_stream.reset_history = true;
    CHECK(generator.submit(reset_stream).has_value());
    CHECK(backend->last_reset_history);

    auto reset_does_not_allow_mixed_clocks = valid_submission();
    reset_does_not_allow_mixed_clocks.reset_history = true;
    reset_does_not_allow_mixed_clocks.current.timing.clock_domain += 1;
    expect_error(generator.submit(reset_does_not_allow_mixed_clocks),
                 ErrorCode::invalid_argument);
    CHECK(backend->submit_count == 1);
}

void test_null_and_foreign_gpu_dependencies_are_rejected() {
    auto backend = std::make_shared<FakeBackend>();
    auto generator = make_generator(backend);

    auto null_dependency = valid_submission();
    null_dependency.gpu_dependencies.emplace_back();
    expect_error(generator.submit(null_dependency), ErrorCode::invalid_argument);

    auto foreign_backend = valid_submission();
    foreign_backend.gpu_dependencies.push_back(std::make_shared<FakeSyncPoint>(
        "org.example.framegen.other", kDeviceId));
    expect_error(generator.submit(foreign_backend), ErrorCode::incompatible_resource);

    auto foreign_device = valid_submission();
    foreign_device.gpu_dependencies.push_back(std::make_shared<FakeSyncPoint>(
        std::string(kBackendId), kDeviceId + 1));
    expect_error(generator.submit(foreign_device), ErrorCode::incompatible_resource);

    CHECK(backend->submit_count == 0);
}

void test_backend_output_validation() {
    const auto input_description = standard_descriptor();

    const auto expect_backend_failure = [&](FakeBackend::SubmitFunction function) {
        auto backend = std::make_shared<FakeBackend>();
        backend->submit_function = std::move(function);
        auto generator = make_generator(backend);
        auto submission = valid_submission(input_description);
        expect_error(generator.submit(submission), ErrorCode::backend_failure);
        CHECK(backend->submit_count == 1);
    };

    expect_backend_failure([](const FrameSubmission&) {
        return GeneratedFrame{.texture = Texture{},
                              .completion = std::make_shared<FakeCompletion>()};
    });
    expect_backend_failure([](const FrameSubmission&) {
        return GeneratedFrame{
            .texture = make_texture(standard_descriptor(), "org.example.framegen.other",
                                    kDeviceId),
            .completion = std::make_shared<FakeCompletion>(),
        };
    });
    expect_backend_failure([](const FrameSubmission&) {
        return GeneratedFrame{
            .texture = make_texture(standard_descriptor(), std::string(kBackendId),
                                    kDeviceId + 1),
            .completion = std::make_shared<FakeCompletion>(),
        };
    });
    expect_backend_failure([](const FrameSubmission&) {
        auto mismatched = standard_descriptor();
        mismatched.format = PixelFormat::rgba8_unorm;
        return GeneratedFrame{
            .texture = make_texture(mismatched),
            .completion = std::make_shared<FakeCompletion>(),
        };
    });
    expect_backend_failure([](const FrameSubmission& submission) {
        return GeneratedFrame{
            .texture = submission.current.texture,
            .completion = std::make_shared<FakeCompletion>(),
        };
    });
    expect_backend_failure([](const FrameSubmission& submission) {
        // A distinct wrapper advertises the same underlying GPU storage as an
        // input, which must be rejected as an aliased output resource.
        return GeneratedFrame{
            .texture = make_texture(standard_descriptor(), std::string(kBackendId),
                                    kDeviceId,
                                    submission.previous.texture.resource_identity()),
            .completion = std::make_shared<FakeCompletion>(),
        };
    });
    expect_backend_failure([](const FrameSubmission&) {
        return GeneratedFrame{.texture = make_texture(), .completion = nullptr};
    });
    expect_backend_failure([](const FrameSubmission&) {
        return GeneratedFrame{
            .texture = make_texture(),
            .completion = std::make_shared<FakeCompletion>("org.example.framegen.other",
                                                           kDeviceId),
        };
    });
    expect_backend_failure([](const FrameSubmission&) {
        return GeneratedFrame{
            .texture = make_texture(),
            .completion = std::make_shared<FakeCompletion>(std::string(kBackendId),
                                                           kDeviceId + 1),
        };
    });
}

using Test = std::pair<const char*, void (*)()>;

} // namespace

int main() {
    const std::vector<Test> tests{
        {"valid submission returns a distinct texture and completion",
         test_valid_submission_returns_distinct_texture_and_completion},
        {"null backend and null input validation", test_null_backend_and_null_inputs_are_rejected},
        {"interpolation range and finiteness",
         test_interpolation_requires_finite_value_in_closed_unit_interval},
        {"backend and device mismatch validation",
         test_backend_and_device_mismatches_are_rejected},
        {"input descriptor compatibility", test_input_descriptors_must_match},
        {"explicit UI-plane input validation",
         test_explicit_ui_plane_requires_two_matching_layers},
        {"timing validation and history reset",
         test_timing_and_clock_domain_validation_with_history_reset},
        {"GPU dependency ownership", test_null_and_foreign_gpu_dependencies_are_rejected},
        {"backend output validation", test_backend_output_validation},
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
