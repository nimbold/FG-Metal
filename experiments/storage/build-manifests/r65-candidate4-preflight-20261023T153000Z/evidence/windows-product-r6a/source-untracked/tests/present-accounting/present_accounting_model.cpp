#include "../../src/dxvk/dxvk_present_accounting.h"

#include <iostream>
#include <string>
#include <vector>

using namespace dxvk;

namespace {

  // MODEL only: this simulates caller routing and a WSI return around the
  // production ledger. It does not invoke DXGI, D3D11, DxvkDevice, the queue,
  // Presenter, or vkQueuePresentKHR and is not production-path evidence.
  bool check(bool condition, const char* message) {
    if (!condition)
      std::cerr << "MODEL assertion failed: " << message << '\n';
    return condition;
  }

  bool dispatch(
    PresentAccounting& accounting,
    const PresentRequest& request,
    uint64_t ordinal,
    uint64_t signalValue,
    WsiPresentId* wsiId) {
    if (request.origin == PresentOrigin::SOURCE
     && !accounting.setSourceOrdinal(request, ordinal, signalValue))
      return false;
    if (!accounting.markQueued(request))
      return false;

    auto id = accounting.beginWsiCall(request, signalValue);
    if (!id)
      return false;

    *wsiId = *id;
    return accounting.recordWsiResult(*id, 0)
        && accounting.terminalizeWsi(*id).has_value();
  }

  bool drop(PresentAccounting& accounting, const PresentRequest& request) {
    return accounting.terminalizeBeforeDispatch(
      request, PresentRequestTerminal::PreDispatchFailure, -1);
  }

}

int main() {
  PresentAccounting accounting;
  std::vector<PresentRequest> requests;
  std::vector<std::string> trace;

  auto a0 = accounting.makeSourceRequest();
  auto i0 = accounting.makeInternalRequest();
  auto a1 = accounting.makeSourceRequest();
  auto i1 = accounting.makeInternalRequest();
  auto a2 = accounting.makeSourceRequest();

  if (!check(a0 && i0 && a1 && i1 && a2, "model allocates interleaved request origins"))
    return 1;

  requests = {
    PresentRequest::source(*a0),
    PresentRequest::internal(*i0),
    PresentRequest::source(*a1),
    PresentRequest::internal(*i1),
    PresentRequest::source(*a2),
  };

  WsiPresentId w0, w1, w2;
  if (!check(dispatch(accounting, requests[0], 1u, 101u, &w0), "MODEL A0 -> W0 succeeds"))
    return 1;
  trace.push_back("MODEL SOURCE A" + std::to_string(a0->value())
    + " -> WSI W" + std::to_string(w0.value()) + " -> simulated result 0");
  if (!check(dispatch(accounting, requests[1], 0u, 0u, &w1), "MODEL I0 -> W1 succeeds"))
    return 1;
  trace.push_back("MODEL INTERNAL I" + std::to_string(i0->value())
    + " -> WSI W" + std::to_string(w1.value()) + " -> simulated result 0");
  if (!check(drop(accounting, requests[2]), "MODEL A1 terminates before WSI"))
    return 1;
  trace.push_back("MODEL SOURCE A" + std::to_string(a1->value()) + " -> pre-WSI terminal");
  if (!check(drop(accounting, requests[3]), "MODEL I1 terminates before WSI"))
    return 1;
  trace.push_back("MODEL INTERNAL I" + std::to_string(i1->value()) + " -> pre-WSI terminal");
  if (!check(dispatch(accounting, requests[4], 3u, 103u, &w2), "MODEL A2 -> W2 succeeds"))
    return 1;
  trace.push_back("MODEL SOURCE A" + std::to_string(a2->value())
    + " -> WSI W" + std::to_string(w2.value()) + " -> simulated result 0");

  if (!check(w0.value() == 1u && w1.value() == 2u && w2.value() == 3u,
          "only simulated WSI calls consume the monotonic WSI sequence"))
    return 1;
  if (!check(!accounting.recordTimingFeedback(w1, 200u),
          "INTERNAL WSI cannot update the SOURCE projection"))
    return 1;
  if (!check(accounting.recordTimingFeedback(w2, 300u), "newer SOURCE feedback resolves through W2"))
    return 1;
  if (!check(!accounting.recordTimingFeedback(w0, 100u), "delayed older feedback cannot regress projection"))
    return 1;
  if (!check(!accounting.recordTimingFeedback(WsiPresentId(99u), 990u), "unknown WSI feedback is rejected"))
    return 1;

  PresentTimingFeedback feedback = accounting.querySourceTiming();
  if (!check(feedback.appFrameId == *a2 && feedback.sourcePresentOrdinal == 3u
          && feedback.presentTime == 300u,
          "MODEL public source projection remains tied to A2"))
    return 1;

  for (const std::string& event : trace)
    std::cout << event << '\n';

  return 0;
}
