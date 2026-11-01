#include "../../src/dxvk/dxvk_present_accounting.h"

#include <array>
#include <cstddef>
#include <iostream>
#include <type_traits>

using namespace dxvk;

static_assert(!std::is_convertible<AppFrameId, InternalPresentId>::value,
  "application and internal IDs must not convert");
static_assert(!std::is_convertible<AppFrameId, WsiPresentId>::value,
  "application and WSI IDs must not convert");
static_assert(!std::is_convertible<InternalPresentId, AppFrameId>::value,
  "internal and application IDs must not convert");
static_assert(!std::is_convertible<InternalPresentId, WsiPresentId>::value,
  "internal and WSI IDs must not convert");
static_assert(!std::is_convertible<WsiPresentId, AppFrameId>::value,
  "WSI and application IDs must not convert");
static_assert(!std::is_convertible<WsiPresentId, InternalPresentId>::value,
  "WSI and internal IDs must not convert");
static_assert(!std::is_constructible<InternalPresentId, AppFrameId>::value,
  "application IDs must not construct internal IDs");
static_assert(!std::is_constructible<WsiPresentId, InternalPresentId>::value,
  "internal IDs must not construct WSI IDs");

namespace {

  bool check(bool condition, const char* message) {
    if (!condition)
      std::cerr << "UNIT assertion failed: " << message << '\n';
    return condition;
  }

  bool testIndependentIdentitySpaces() {
    PresentAccounting accounting;
    auto app = accounting.makeSourceRequest();
    auto internal = accounting.makeInternalRequest();

    if (!check(app && internal, "both request allocators produce an initial ID"))
      return false;
    if (!check(app->value() == internal->value(), "equal numeric payloads can exist in separate domains"))
      return false;
    if (!check(PresentRequest::source(*app).valid(), "SOURCE variant matches SOURCE origin"))
      return false;
    if (!check(PresentRequest::internal(*internal).valid(), "INTERNAL variant matches INTERNAL origin"))
      return false;

    PresentRequest malformed { *app, PresentOrigin::INTERNAL };
    return check(!malformed.valid(), "mismatched origin and ID variant is rejected")
        && check(usesSourcePresentPacing(PresentOrigin::SOURCE), "SOURCE origin uses source pacing")
        && check(!usesSourcePresentPacing(PresentOrigin::INTERNAL), "INTERNAL origin bypasses source pacing")
        && check(!accounting.markQueued(malformed), "mismatched request cannot be queued");
  }

  bool testOrderedSourceSignalRelease() {
    SourceSignalAccounting signals(20u);
    PresentRequest source0 = PresentRequest::source(AppFrameId(1u));
    PresentRequest source1 = PresentRequest::source(AppFrameId(2u));
    PresentRequest internal = PresentRequest::internal(InternalPresentId(1u));

    if (!check(signals.registerSignal(source0, 21u)
            && signals.registerSignal(source1, 22u),
            "consecutive SOURCE waitable tokens are registered"))
      return false;
    if (!check(!signals.registerSignal(internal, 23u),
            "INTERNAL requests cannot own SOURCE waitable tokens"))
      return false;

    // Model source1 failing before WSI while source0 is still awaiting its
    // pacing retirement. The later token must remain held behind source0.
    if (!check(!signals.markQueueReady(source1, 22u)
            && !signals.markPacingComplete(source1, 22u),
            "later pre-WSI failure cannot signal through an earlier SOURCE frame"))
      return false;
    if (!check(!signals.markQueueReady(source0, 21u),
            "queue readiness alone does not release SOURCE waitable"))
      return false;

    auto released = signals.markPacingComplete(source0, 21u);
    return check(released && *released == 22u,
        "ordered SOURCE release advances through the completed frame and failure once")
      && check(!signals.markPacingComplete(source1, 22u),
        "duplicate SOURCE pacing completion cannot signal twice");
  }

  bool testSourceDispatchAndFeedback() {
    PresentAccounting accounting;
    auto app = accounting.makeSourceRequest();
    if (!check(app.has_value(), "SOURCE request is allocated"))
      return false;

    PresentRequest request = PresentRequest::source(*app);
    if (!check(accounting.setSourceOrdinal(request, 10u, 31u), "SOURCE ordinal and signal token bind once"))
      return false;
    if (!check(!accounting.setSourceOrdinal(request, 11u, 32u), "SOURCE ordinal cannot be rebound"))
      return false;
    if (!check(accounting.markQueued(request), "created SOURCE request transitions to queued"))
      return false;

    auto wsi = accounting.beginWsiCall(request, 31u);
    if (!check(wsi && wsi->value() == 1u, "WSI ID is allocated for dispatch"))
      return false;
    if (!check(!accounting.beginWsiCall(request, 31u), "one request cannot dispatch twice"))
      return false;
    if (!check(accounting.recordTimingFeedback(*wsi, 100u), "known SOURCE WSI feedback is accepted"))
      return false;

    PresentTimingFeedback feedback = accounting.querySourceTiming();
    if (!check(feedback.appFrameId == *app && feedback.sourcePresentOrdinal == 10u
            && feedback.presentTime == 100u, "feedback preserves exact SOURCE lineage"))
      return false;
    if (!check(!accounting.recordTimingFeedback(WsiPresentId(900u), 900u), "unknown WSI feedback is rejected"))
      return false;

    auto laterApp = accounting.makeSourceRequest();
    if (!check(laterApp.has_value(), "later SOURCE request is allocated"))
      return false;
    PresentRequest laterRequest = PresentRequest::source(*laterApp);
    if (!check(accounting.setSourceOrdinal(laterRequest, 12u, 33u)
            && accounting.markQueued(laterRequest), "later SOURCE request is bound and queued"))
      return false;
    auto laterWsi = accounting.beginWsiCall(laterRequest, 33u);
    if (!check(laterWsi && accounting.recordTimingFeedback(*laterWsi, 120u),
            "later WSI feedback is accepted"))
      return false;
    if (!check(!accounting.recordTimingFeedback(*wsi, 101u), "late feedback cannot move SOURCE projection backward"))
      return false;

    feedback = accounting.querySourceTiming();
    if (!check(feedback.appFrameId == *laterApp && feedback.presentTime == 120u,
            "latest SOURCE projection remains tied to the later request"))
      return false;

    if (!check(accounting.recordWsiResult(*laterWsi, -3), "SOURCE dispatch failure result is recorded"))
      return false;
    auto terminal = accounting.terminalizeWsi(*laterWsi);
    return check(terminal && terminal->wsiId == laterWsi
              && terminal->terminal == PresentRequestTerminal::DispatchFailed,
      "failed SOURCE call retains its consumed WSI identity and one terminal");
  }

  bool testInternalAndTerminalOutcomes() {
    PresentAccounting accounting;
    auto internal = accounting.makeInternalRequest();
    if (!check(internal.has_value(), "INTERNAL request is allocated"))
      return false;

    PresentRequest request = PresentRequest::internal(*internal);
    if (!check(accounting.markQueued(request), "INTERNAL request transitions to queued"))
      return false;
    auto wsi = accounting.beginWsiCall(request, 0u);
    if (!check(wsi.has_value(), "INTERNAL request receives WSI identity at dispatch"))
      return false;
    if (!check(!accounting.recordTimingFeedback(*wsi, 500u), "INTERNAL feedback is excluded from SOURCE projection"))
      return false;
    if (!check(accounting.recordWsiResult(*wsi, -4), "dispatch result is recorded once"))
      return false;
    if (!check(!accounting.recordWsiResult(*wsi, 0), "dispatch result cannot be overwritten"))
      return false;

    auto terminal = accounting.terminalizeWsi(*wsi);
    if (!check(terminal && terminal->terminal == PresentRequestTerminal::DispatchFailed,
            "failed Vulkan dispatch has one request terminal"))
      return false;
    if (!check(!accounting.terminalizeWsi(*wsi), "duplicate WSI terminalization is rejected"))
      return false;

    auto dropped = accounting.makeSourceRequest();
    if (!check(dropped.has_value(), "pre-dispatch SOURCE request is allocated"))
      return false;
    PresentRequest droppedRequest = PresentRequest::source(*dropped);
    if (!check(accounting.terminalizeBeforeDispatch(
          droppedRequest, PresentRequestTerminal::PreDispatchFailure, -7),
          "pre-dispatch drop terminalizes the request"))
      return false;
    if (!check(!accounting.terminalizeBeforeDispatch(
          droppedRequest, PresentRequestTerminal::PreDispatchFailure, -8),
          "pre-dispatch terminal cannot be duplicated"))
      return false;
    if (!check(!accounting.markQueued(droppedRequest), "terminal request cannot be queued"))
      return false;
    return check(!accounting.beginWsiCall(droppedRequest, 0u), "pre-dispatch drop owns no WSI ID");
  }

  bool testActiveHistoryFailsClosedAtLimit() {
    PresentAccounting accounting;
    std::array<AppFrameId, PresentAccounting::HistoryLimit> active;

    for (std::size_t i = 0; i < active.size(); i++) {
      auto id = accounting.makeSourceRequest();
      if (!check(id.has_value(), "request capacity accepts entries up to the fixed limit"))
        return false;
      active[i] = *id;
    }

    if (!check(!accounting.makeSourceRequest(), "active request history cannot grow beyond its bound"))
      return false;
    if (!check(accounting.terminalizeBeforeDispatch(
          PresentRequest::source(active.front()), PresentRequestTerminal::PreDispatchFailure),
          "terminal request can be retired"))
      return false;

    auto replacement = accounting.makeSourceRequest();
    return check(replacement && replacement->value() == PresentAccounting::HistoryLimit + 1u,
      "retiring history frees capacity without recycling IDs");
  }

}

int main() {
  if (!testIndependentIdentitySpaces())
    return 1;
  if (!testOrderedSourceSignalRelease())
    return 1;
  if (!testSourceDispatchAndFeedback())
    return 1;
  if (!testInternalAndTerminalOutcomes())
    return 1;
  if (!testActiveHistoryFailsClosedAtLimit())
    return 1;
  return 0;
}
