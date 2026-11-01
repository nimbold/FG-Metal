#pragma once

#if !defined(DXVK_R6A_PRODUCTION_PATH_TEST_BUILD)
#error This header is only available to the production-path fixture build.
#endif

#include <functional>

#include "dxvk_present_accounting.h"

namespace dxvk {

  class DxvkImage;
  struct DxvkImageCreateInfo;

  // Per-fixture dependencies. These callbacks model WSI, synchronization,
  // and scheduling only; production accounting remains owned by Presenter.
  struct PresentAccountingProductionPathDependencies {
    std::function<bool(const PresentRequest&)> beforeWsiCall;
    std::function<void(VkSemaphore, VkSemaphore)> synchronizeWsi;
    std::function<void(WsiPresentId)> beforeFrameProcessing;
    std::function<void(const DxvkImageCreateInfo&, VkImage)> importSwapchainImage;
  };

  struct PresentAccountingProductionPathTag { };

}
