#pragma once

#include <cstddef>
#include <vector>

#include "../util/rc/util_rc_ptr.h"

namespace dxvk {

  class DxvkDevice;
  class Presenter;

  /**
   * \brief Roots worker-owning renderer objects until normal shutdown
   *
   * On Windows, DLL_PROCESS_DETACH during process termination is not a safe
   * place to join renderer workers or destroy their owners. This registry is
   * allocated lazily during normal execution and intentionally has no static
   * destructor. Normal device and Presenter teardown must explicitly remove
   * each root after its workers and queued references have been retired.
   */
  class DxvkProcessLifetimeRegistry {
  public:

    static void retainDevice(const Rc<DxvkDevice>& device);
    static void releaseDevice(DxvkDevice* device);

    static void retainPresenter(const Rc<Presenter>& presenter);
    static void releasePresenter(Presenter* presenter);

    static std::vector<Rc<Presenter>> presentersForDevice(DxvkDevice* device);

    static bool retainsDevice(DxvkDevice* device);

    static size_t retainedPresentersForDevice(DxvkDevice* device);

    // Exposed for focused lifecycle diagnostics and regression tests.
    static size_t retainedDeviceCount();
    static size_t retainedPresenterCount();

    static bool testModeEnabled();
    static void testGate(const char* stage);
    static void testNotify(const char* stage);

  };

}
