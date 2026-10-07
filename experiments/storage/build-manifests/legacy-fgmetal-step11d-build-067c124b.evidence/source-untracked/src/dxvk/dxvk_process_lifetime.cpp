#include <mutex>
#include <string>
#include <unordered_map>

#include "../util/util_env.h"
#include "dxvk_device.h"
#include "dxvk_presenter.h"
#include "dxvk_process_lifetime.h"

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

namespace dxvk {

  namespace {

    struct RetainedPresenter {
      DxvkDevice* device = nullptr;
      Rc<Presenter> presenter;
    };

    struct Registry {
      std::mutex mutex;
      std::unordered_map<DxvkDevice*, Rc<DxvkDevice>> devices;
      std::unordered_map<Presenter*, RetainedPresenter> presenters;
    };

    Registry& registry() {
      // Intentionally leaked. Its mutex and strong references must survive
      // DLL_PROCESS_DETACH during process termination.
      static Registry* instance = new Registry();
      return *instance;
    }

    std::string testEventName(const char* prefix, const char* stage, const char* suffix) {
      return std::string("Local\\FGMetal11C1R_") + prefix + "_" + stage + "_" + suffix;
    }

  }


  void DxvkProcessLifetimeRegistry::retainDevice(const Rc<DxvkDevice>& device) {
#ifdef _WIN32
    if (!device)
      return;

    Registry& state = registry();
    std::lock_guard<std::mutex> lock(state.mutex);
    state.devices.emplace(device.ptr(), device);
#else
    (void) device;
#endif
  }


  void DxvkProcessLifetimeRegistry::releaseDevice(DxvkDevice* device) {
#ifdef _WIN32
    Rc<DxvkDevice> retired;
    Registry& state = registry();
    {
      std::lock_guard<std::mutex> lock(state.mutex);
      auto entry = state.devices.find(device);
      if (entry != state.devices.end()) {
        retired = std::move(entry->second);
        state.devices.erase(entry);
      }
    }
#else
    (void) device;
#endif
  }


  void DxvkProcessLifetimeRegistry::retainPresenter(const Rc<Presenter>& presenter) {
#ifdef _WIN32
    if (!presenter)
      return;

    Registry& state = registry();
    std::lock_guard<std::mutex> lock(state.mutex);
    state.presenters.emplace(presenter.ptr(), RetainedPresenter {
      presenter->device().ptr(), presenter,
    });
#else
    (void) presenter;
#endif
  }


  void DxvkProcessLifetimeRegistry::releasePresenter(Presenter* presenter) {
#ifdef _WIN32
    Rc<Presenter> retired;
    Registry& state = registry();
    {
      std::lock_guard<std::mutex> lock(state.mutex);
      auto entry = state.presenters.find(presenter);
      if (entry != state.presenters.end()) {
        retired = std::move(entry->second.presenter);
        state.presenters.erase(entry);
      }
    }
#else
    (void) presenter;
#endif
  }


  std::vector<Rc<Presenter>> DxvkProcessLifetimeRegistry::presentersForDevice(DxvkDevice* device) {
    std::vector<Rc<Presenter>> result;
#ifdef _WIN32
    Registry& state = registry();
    std::lock_guard<std::mutex> lock(state.mutex);
    result.reserve(state.presenters.size());
    for (const auto& entry : state.presenters) {
      if (entry.second.device == device)
        result.push_back(entry.second.presenter);
    }
#else
    (void) device;
#endif
    return result;
  }


  bool DxvkProcessLifetimeRegistry::retainsDevice(DxvkDevice* device) {
#ifdef _WIN32
    Registry& state = registry();
    std::lock_guard<std::mutex> lock(state.mutex);
    return state.devices.find(device) != state.devices.end();
#else
    (void) device;
    return false;
#endif
  }


  size_t DxvkProcessLifetimeRegistry::retainedPresentersForDevice(DxvkDevice* device) {
#ifdef _WIN32
    Registry& state = registry();
    std::lock_guard<std::mutex> lock(state.mutex);
    size_t count = 0u;
    for (const auto& entry : state.presenters)
      count += entry.second.device == device;
    return count;
#else
    (void) device;
    return 0u;
#endif
  }


  size_t DxvkProcessLifetimeRegistry::retainedDeviceCount() {
#ifdef _WIN32
    Registry& state = registry();
    std::lock_guard<std::mutex> lock(state.mutex);
    return state.devices.size();
#else
    return 0u;
#endif
  }


  size_t DxvkProcessLifetimeRegistry::retainedPresenterCount() {
#ifdef _WIN32
    Registry& state = registry();
    std::lock_guard<std::mutex> lock(state.mutex);
    return state.presenters.size();
#else
    return 0u;
#endif
  }


  bool DxvkProcessLifetimeRegistry::testModeEnabled() {
#ifdef _WIN32
    return !env::getEnvVar("DXVK_INTERNAL_WSI_TEST_PREFIX").empty();
#else
    return false;
#endif
  }


  void DxvkProcessLifetimeRegistry::testGate(const char* stage) {
#ifdef _WIN32
    auto prefix = env::getEnvVar("DXVK_INTERNAL_WSI_TEST_PREFIX");
    auto selectedStage = env::getEnvVar("DXVK_INTERNAL_WSI_TEST_STAGE");
    if (prefix.empty() || selectedStage != stage)
      return;

    auto enteredName = testEventName(prefix.c_str(), stage, "entered");
    auto releaseName = testEventName(prefix.c_str(), stage, "release");
    HANDLE entered = ::CreateEventA(nullptr, TRUE, FALSE, enteredName.c_str());
    HANDLE release = ::CreateEventA(nullptr, TRUE, FALSE, releaseName.c_str());
    if (!entered || !release) {
      if (entered)
        ::CloseHandle(entered);
      if (release)
        ::CloseHandle(release);
      return;
    }

    try {
      Logger::info(str::format("InternalWSI-Test: entered stage=", stage));
    } catch (...) {
    }
    ::SetEvent(entered);
    ::WaitForSingleObject(release, 30000);
    ::CloseHandle(release);
    ::CloseHandle(entered);
#else
    (void) stage;
#endif
  }


  void DxvkProcessLifetimeRegistry::testNotify(const char* stage) {
#ifdef _WIN32
    auto prefix = env::getEnvVar("DXVK_INTERNAL_WSI_TEST_PREFIX");
    if (prefix.empty())
      return;

    auto enteredName = testEventName(prefix.c_str(), stage, "entered");
    HANDLE entered = ::CreateEventA(nullptr, TRUE, FALSE, enteredName.c_str());
    if (entered) {
      ::SetEvent(entered);
      ::CloseHandle(entered);
    }
#else
    (void) stage;
#endif
  }

}
