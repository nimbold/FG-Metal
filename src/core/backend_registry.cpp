#include "backend_registry.hpp"

#include <algorithm>
#include <mutex>
#include <string>

namespace framegen::detail {
namespace {

std::mutex g_registry_mutex;
std::vector<std::shared_ptr<BackendProvider>> g_providers;

std::string backend_id_of(const framegen_backend_info_t& info) {
    const auto end = std::find(info.backend_id, info.backend_id + FRAMEGEN_BACKEND_ID_CAPACITY,
                               '\0');
    return std::string(info.backend_id, end);
}

} // namespace

void register_backend_provider(std::shared_ptr<BackendProvider> provider) {
    if (!provider) {
        return;
    }
    const std::string id = backend_id_of(provider->info());
    if (id.empty()) {
        return;
    }
    std::scoped_lock lock(g_registry_mutex);
    for (auto& current : g_providers) {
        if (backend_id_of(current->info()) == id) {
            current = std::move(provider);
            return;
        }
    }
    g_providers.push_back(std::move(provider));
}

std::vector<std::shared_ptr<BackendProvider>> backend_providers() {
    std::scoped_lock lock(g_registry_mutex);
    return g_providers;
}

std::shared_ptr<BackendProvider> find_backend_provider(const std::string& backend_id) {
    std::scoped_lock lock(g_registry_mutex);
    for (const auto& provider : g_providers) {
        if (backend_id_of(provider->info()) == backend_id) {
            return provider;
        }
    }
    return {};
}

} // namespace framegen::detail
