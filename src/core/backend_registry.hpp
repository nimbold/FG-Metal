#pragma once

#include "framegen/api.h"
#include "framegen/backend.hpp"

#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace framegen::detail {

struct BackendInstance {
    std::shared_ptr<FrameGenerationBackend> backend;
    std::function<Result<Texture>(const framegen_image_t&)> import_image;
    std::function<Result<std::shared_ptr<const GpuSyncPoint>>(
        const framegen_sync_point_t&)> import_sync;
    std::function<Result<framegen_image_t>(const Texture&)> export_image;
    std::function<Result<framegen_sync_point_t>(const GpuSyncPoint&)> export_sync;
    std::function<void(framegen_invalidation_reason_t)> invalidate;
    std::function<void(const framegen_presentation_event_t&)> notify_presentation;
};

class BackendProvider {
public:
    virtual ~BackendProvider() = default;
    [[nodiscard]] virtual framegen_backend_info_t info() const = 0;
    [[nodiscard]] virtual Result<BackendInstance> create(
        const framegen_device_ref_t& device) const = 0;
};

void register_backend_provider(std::shared_ptr<BackendProvider> provider);
[[nodiscard]] std::vector<std::shared_ptr<BackendProvider>> backend_providers();
[[nodiscard]] std::shared_ptr<BackendProvider> find_backend_provider(
    const std::string& backend_id);

} // namespace framegen::detail
