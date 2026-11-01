#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <limits>
#include <map>
#include <mutex>
#include <optional>
#include <utility>
#include <variant>

namespace dxvk {

  class AppFrameId {
  public:
    constexpr AppFrameId() = default;
    explicit constexpr AppFrameId(uint64_t value) : m_value(value) { }

    constexpr uint64_t value() const { return m_value; }
    constexpr bool valid() const { return m_value != 0; }

    friend constexpr bool operator==(AppFrameId a, AppFrameId b) { return a.m_value == b.m_value; }
    friend constexpr bool operator!=(AppFrameId a, AppFrameId b) { return !(a == b); }
    friend constexpr bool operator<(AppFrameId a, AppFrameId b) { return a.m_value < b.m_value; }

  private:
    uint64_t m_value = 0;
  };

  class InternalPresentId {
  public:
    constexpr InternalPresentId() = default;
    explicit constexpr InternalPresentId(uint64_t value) : m_value(value) { }

    constexpr uint64_t value() const { return m_value; }
    constexpr bool valid() const { return m_value != 0; }

    friend constexpr bool operator==(InternalPresentId a, InternalPresentId b) { return a.m_value == b.m_value; }
    friend constexpr bool operator!=(InternalPresentId a, InternalPresentId b) { return !(a == b); }
    friend constexpr bool operator<(InternalPresentId a, InternalPresentId b) { return a.m_value < b.m_value; }

  private:
    uint64_t m_value = 0;
  };

  class WsiPresentId {
  public:
    constexpr WsiPresentId() = default;
    explicit constexpr WsiPresentId(uint64_t value) : m_value(value) { }

    constexpr uint64_t value() const { return m_value; }
    constexpr bool valid() const { return m_value != 0; }

    friend constexpr bool operator==(WsiPresentId a, WsiPresentId b) { return a.m_value == b.m_value; }
    friend constexpr bool operator!=(WsiPresentId a, WsiPresentId b) { return !(a == b); }
    friend constexpr bool operator<(WsiPresentId a, WsiPresentId b) { return a.m_value < b.m_value; }

  private:
    uint64_t m_value = 0;
  };

  enum class PresentOrigin : uint8_t {
    SOURCE,
    INTERNAL,
  };

  constexpr bool usesSourcePresentPacing(PresentOrigin origin) {
    return origin == PresentOrigin::SOURCE;
  }

  using PresentRequestIdentity = std::variant<AppFrameId, InternalPresentId>;

  struct PresentRequest {
    PresentRequestIdentity identity;
    PresentOrigin origin;

    static PresentRequest source(AppFrameId id) {
      return { id, PresentOrigin::SOURCE };
    }

    static PresentRequest internal(InternalPresentId id) {
      return { id, PresentOrigin::INTERNAL };
    }

    bool valid() const {
      if (origin == PresentOrigin::SOURCE)
        return std::holds_alternative<AppFrameId>(identity)
            && std::get<AppFrameId>(identity).valid();

      return origin == PresentOrigin::INTERNAL
          && std::holds_alternative<InternalPresentId>(identity)
          && std::get<InternalPresentId>(identity).valid();
    }

    friend bool operator==(const PresentRequest& a, const PresentRequest& b) {
      return a.origin == b.origin && a.identity == b.identity;
    }

    friend bool operator!=(const PresentRequest& a, const PresentRequest& b) {
      return !(a == b);
    }
  };

  /**
   * \brief Orders SOURCE waitable release across dispatch and pacing completion
   *
   * A SOURCE signal is eligible only after both GPU-queue retirement and
   * Presenter pacing have completed. Pre-dispatch failures mark both sides
   * complete. The bounded ordered ledger prevents a later failed request from
   * advancing a waitable past an earlier in-flight SOURCE frame.
   */
  class SourceSignalAccounting {
  public:
    explicit SourceSignalAccounting(uint64_t initialValue = 0u)
    : m_lastRegistered(initialValue), m_lastEmitted(initialValue) { }

    bool registerSignal(const PresentRequest& request, uint64_t value) {
      if (!request.valid() || request.origin != PresentOrigin::SOURCE || !value)
        return false;

      std::lock_guard lock(m_mutex);
      if (m_pending.size() >= HistoryLimit
       || m_lastRegistered == std::numeric_limits<uint64_t>::max()
       || value != m_lastRegistered + 1u)
        return false;

      m_pending.emplace(value, SignalRecord { request });
      if (!m_nextSignalValue)
        m_nextSignalValue = value;
      m_lastRegistered = value;
      return true;
    }

    bool cancelRegistration(const PresentRequest& request, uint64_t value) {
      std::lock_guard lock(m_mutex);
      auto record = m_pending.find(value);
      if (record == m_pending.end() || record->second.request != request
       || record->second.queueReady || record->second.pacingComplete
       || value != m_lastRegistered)
        return false;

      m_pending.erase(record);
      m_lastRegistered = value - 1u;
      if (m_pending.empty() && m_nextSignalValue == value)
        m_nextSignalValue = 0u;
      return true;
    }

    std::optional<uint64_t> markQueueReady(
      const PresentRequest& request,
      uint64_t value) {
      std::lock_guard lock(m_mutex);
      auto record = findRecord(request, value);
      if (!record || record->queueReady)
        return std::nullopt;

      record->queueReady = true;
      return advanceReadySignals();
    }

    std::optional<uint64_t> markPacingComplete(
      const PresentRequest& request,
      uint64_t value) {
      std::lock_guard lock(m_mutex);
      auto record = findRecord(request, value);
      if (!record || record->pacingComplete)
        return std::nullopt;

      record->pacingComplete = true;
      return advanceReadySignals();
    }

    static constexpr size_t HistoryLimit = 4096u;

  private:
    struct SignalRecord {
      PresentRequest request;
      bool queueReady = false;
      bool pacingComplete = false;
    };

    SignalRecord* findRecord(const PresentRequest& request, uint64_t value) {
      auto record = m_pending.find(value);
      return record != m_pending.end() && record->second.request == request
        ? &record->second : nullptr;
    }

    std::optional<uint64_t> advanceReadySignals() {
      std::optional<uint64_t> signaledValue;
      while (m_nextSignalValue) {
        auto record = m_pending.find(m_nextSignalValue);
        if (record == m_pending.end()
         || !record->second.queueReady || !record->second.pacingComplete)
          break;

        signaledValue = m_nextSignalValue;
        m_lastEmitted = m_nextSignalValue;
        m_pending.erase(record);

        m_nextSignalValue = m_lastEmitted == std::numeric_limits<uint64_t>::max()
          ? 0u : m_lastEmitted + 1u;
      }
      return signaledValue;
    }

    std::mutex m_mutex;
    std::map<uint64_t, SignalRecord> m_pending;
    uint64_t m_lastRegistered = 0u;
    uint64_t m_lastEmitted = 0u;
    uint64_t m_nextSignalValue = 0u;
  };

  enum class PresentRequestTerminal : uint8_t {
    PreDispatchFailure,
    QueueDeviceLost,
    DispatchIdExhausted,
    DispatchUnavailable,
    DispatchCompleted,
    DispatchFailed,
    Abandoned,
  };

  struct PresentRequestSnapshot {
    PresentRequest request;
    std::optional<uint64_t> sourcePresentOrdinal;
    std::optional<uint64_t> sourceSignalValue;
    std::optional<WsiPresentId> wsiId;
    std::optional<PresentRequestTerminal> terminal;
    int32_t result = 0;
  };

  struct PresentTimingFeedback {
    AppFrameId appFrameId;
    uint64_t sourcePresentOrdinal = 0;
    uint64_t presentTime = 0;
  };

  /**
   * \brief SOURCE/INTERNAL request and WSI dispatch accounting
   *
   * Request identity is allocated before fallible presentation work. WSI identity
   * is allocated only when a caller is about to invoke vkQueuePresentKHR. The
   * bounded WSI history retains enough lineage to join delayed timing reports;
   * IDs are never recycled when old records are retired.
   */
  class PresentAccounting {
  public:
    static constexpr size_t HistoryLimit = 4096;

    ~PresentAccounting() {
      terminalizePending();
    }

    std::optional<AppFrameId> makeSourceRequest() {
      std::lock_guard lock(m_mutex);
      pruneRequestHistory();
      if (m_requests.size() >= HistoryLimit)
        return std::nullopt;

      auto id = allocate<AppFrameId>(m_nextAppFrameId);
      if (!id)
        return std::nullopt;

      PresentRequest request = PresentRequest::source(*id);
      m_requests.emplace(request.identity, RequestRecord { request });
      m_requestOrder.push_back(request.identity);
      return id;
    }

    std::optional<InternalPresentId> makeInternalRequest() {
      std::lock_guard lock(m_mutex);
      pruneRequestHistory();
      if (m_requests.size() >= HistoryLimit)
        return std::nullopt;

      auto id = allocate<InternalPresentId>(m_nextInternalPresentId);
      if (!id)
        return std::nullopt;

      PresentRequest request = PresentRequest::internal(*id);
      m_requests.emplace(request.identity, RequestRecord { request });
      m_requestOrder.push_back(request.identity);
      return id;
    }

    bool markQueued(const PresentRequest& request) {
      std::lock_guard lock(m_mutex);
      auto record = findRequest(request);
      if (!record || record->state != RequestState::Created)
        return false;

      record->state = RequestState::Queued;
      return true;
    }

    bool setSourceOrdinal(
      const PresentRequest& request,
      uint64_t ordinal,
      uint64_t sourceSignalValue) {
      if (request.origin != PresentOrigin::SOURCE || !ordinal || !sourceSignalValue)
        return false;

      std::lock_guard lock(m_mutex);
      auto record = findRequest(request);
      if (!record || record->state == RequestState::Terminal || record->sourcePresentOrdinal)
        return false;

      record->sourcePresentOrdinal = ordinal;
      record->sourceSignalValue = sourceSignalValue;
      return true;
    }

    bool terminalizeBeforeDispatch(
      const PresentRequest& request,
      PresentRequestTerminal reason,
      int32_t result = 0) {
      std::lock_guard lock(m_mutex);
      auto record = findRequest(request);
      if (!record || record->state == RequestState::Terminal || record->wsiId)
        return false;

      if (record->state == RequestState::Queued
       && reason != PresentRequestTerminal::QueueDeviceLost
       && reason != PresentRequestTerminal::DispatchIdExhausted
       && reason != PresentRequestTerminal::DispatchUnavailable
       && reason != PresentRequestTerminal::Abandoned)
        return false;

      record->state = RequestState::Terminal;
      record->terminal = reason;
      record->result = result;
      return true;
    }

    std::optional<WsiPresentId> beginWsiCall(
      const PresentRequest& request,
      uint64_t sourceSignalValue,
      PresentRequestTerminal* failureReason = nullptr) {
      std::lock_guard lock(m_mutex);
      if (failureReason)
        *failureReason = PresentRequestTerminal::DispatchUnavailable;

      auto record = findRequest(request);
      if (!record || record->state != RequestState::Queued || record->wsiId)
        return std::nullopt;

      if (request.origin == PresentOrigin::SOURCE && !record->sourcePresentOrdinal)
        return std::nullopt;
      if (request.origin == PresentOrigin::SOURCE
       && (!record->sourceSignalValue || *record->sourceSignalValue != sourceSignalValue))
        return std::nullopt;

      pruneWsiHistory();
      if (m_wsiRecords.size() >= HistoryLimit)
        return std::nullopt;

      auto id = allocate<WsiPresentId>(m_nextWsiPresentId);
      if (!id) {
        if (failureReason)
          *failureReason = PresentRequestTerminal::DispatchIdExhausted;
        return std::nullopt;
      }

      WsiRecord dispatch { *id, request, record->sourcePresentOrdinal,
                           request.origin == PresentOrigin::SOURCE
                             ? std::optional<uint64_t>(sourceSignalValue)
                             : std::nullopt };
      m_wsiRecords.emplace(*id, dispatch);
      record->wsiId = *id;
      record->state = RequestState::Dispatched;
      m_wsiOrder.push_back(*id);
      return id;
    }

    bool recordWsiResult(WsiPresentId id, int32_t result) {
      std::lock_guard lock(m_mutex);
      auto dispatch = m_wsiRecords.find(id);
      if (dispatch == m_wsiRecords.end() || dispatch->second.hasResult || dispatch->second.terminal)
        return false;

      dispatch->second.result = result;
      dispatch->second.hasResult = true;
      return true;
    }

    bool markWsiSignalPublished(WsiPresentId id) {
      std::lock_guard lock(m_mutex);
      auto dispatch = m_wsiRecords.find(id);
      if (dispatch == m_wsiRecords.end() || dispatch->second.signalPublished)
        return false;

      dispatch->second.signalPublished = true;
      return true;
    }

    std::optional<PresentRequestSnapshot> terminalizeWsi(WsiPresentId id) {
      std::lock_guard lock(m_mutex);
      auto dispatch = m_wsiRecords.find(id);
      if (dispatch == m_wsiRecords.end() || !dispatch->second.hasResult || dispatch->second.terminal)
        return std::nullopt;

      auto request = findRequest(dispatch->second.request);
      if (!request || request->state != RequestState::Dispatched || request->terminal)
        return std::nullopt;

      PresentRequestTerminal terminal = dispatch->second.result >= 0
        ? PresentRequestTerminal::DispatchCompleted
        : PresentRequestTerminal::DispatchFailed;

      dispatch->second.terminal = terminal;
      request->state = RequestState::Terminal;
      request->terminal = terminal;
      request->result = dispatch->second.result;

      return snapshot(*request);
    }

    std::optional<PresentRequestSnapshot> lookupWsi(WsiPresentId id) const {
      std::lock_guard lock(m_mutex);
      auto dispatch = m_wsiRecords.find(id);
      if (dispatch == m_wsiRecords.end())
        return std::nullopt;

      return PresentRequestSnapshot {
        dispatch->second.request,
        dispatch->second.sourcePresentOrdinal,
        dispatch->second.sourceSignalValue,
        dispatch->second.id,
        dispatch->second.terminal,
        dispatch->second.result,
      };
    }

    std::optional<PresentRequestSnapshot> lookupRequest(
      const PresentRequest& request) const {
      std::lock_guard lock(m_mutex);
      auto record = findRequest(request);
      return record ? std::optional<PresentRequestSnapshot>(snapshot(*record))
                    : std::nullopt;
    }

    bool recordTimingFeedback(WsiPresentId id, uint64_t presentTime) {
      std::lock_guard lock(m_mutex);
      auto dispatch = m_wsiRecords.find(id);
      if (dispatch == m_wsiRecords.end()
       || dispatch->second.request.origin != PresentOrigin::SOURCE
       || !dispatch->second.sourcePresentOrdinal
       || !std::holds_alternative<AppFrameId>(dispatch->second.request.identity))
        return false;

      const uint64_t ordinal = *dispatch->second.sourcePresentOrdinal;
      if (ordinal <= m_sourceFeedback.sourcePresentOrdinal)
        return false;

      m_sourceFeedback = PresentTimingFeedback {
        std::get<AppFrameId>(dispatch->second.request.identity), ordinal, presentTime };
      return true;
    }

    PresentTimingFeedback querySourceTiming() const {
      std::lock_guard lock(m_mutex);
      return m_sourceFeedback;
    }

    void terminalizePending() {
      std::lock_guard lock(m_mutex);
      for (auto& entry : m_requests) {
        auto& request = entry.second;
        if (request.state != RequestState::Terminal && !request.wsiId) {
          request.state = RequestState::Terminal;
          request.terminal = PresentRequestTerminal::Abandoned;
        }
      }

      for (auto& entry : m_wsiRecords) {
        auto& dispatch = entry.second;
        if (dispatch.terminal)
          continue;

        dispatch.terminal = PresentRequestTerminal::Abandoned;
        auto request = findRequest(dispatch.request);
        if (request && request->state != RequestState::Terminal) {
          request->state = RequestState::Terminal;
          request->terminal = PresentRequestTerminal::Abandoned;
          request->result = dispatch.result;
        }
      }
    }

  private:
    enum class RequestState : uint8_t {
      Created,
      Queued,
      Dispatched,
      Terminal,
    };

    struct RequestRecord {
      PresentRequest request;
      RequestState state = RequestState::Created;
      std::optional<uint64_t> sourcePresentOrdinal;
      std::optional<uint64_t> sourceSignalValue;
      std::optional<WsiPresentId> wsiId;
      std::optional<PresentRequestTerminal> terminal;
      int32_t result = 0;
    };

    struct WsiRecord {
      WsiPresentId id;
      PresentRequest request;
      std::optional<uint64_t> sourcePresentOrdinal;
      std::optional<uint64_t> sourceSignalValue;
      bool hasResult = false;
      int32_t result = 0;
      std::optional<PresentRequestTerminal> terminal;
      bool signalPublished = false;
    };

    template<typename Id>
    static std::optional<Id> allocate(uint64_t& next) {
      auto value = allocateValue(next);
      return value ? std::optional<Id>(Id(*value)) : std::nullopt;
    }

    static std::optional<uint64_t> allocateValue(uint64_t& next) {
      if (!next)
        return std::nullopt;

      uint64_t value = next;
      next = value == UINT64_MAX ? 0 : value + 1;
      return value;
    }

    RequestRecord* findRequest(const PresentRequest& request) {
      if (!request.valid())
        return nullptr;

      auto it = m_requests.find(request.identity);
      return it != m_requests.end() && it->second.request == request ? &it->second : nullptr;
    }

    const RequestRecord* findRequest(const PresentRequest& request) const {
      if (!request.valid())
        return nullptr;

      auto it = m_requests.find(request.identity);
      return it != m_requests.end() && it->second.request == request ? &it->second : nullptr;
    }

    static PresentRequestSnapshot snapshot(const RequestRecord& record) {
      return { record.request, record.sourcePresentOrdinal, record.sourceSignalValue, record.wsiId,
               record.terminal, record.result };
    }

    void pruneRequestHistory() {
      size_t remaining = m_requestOrder.size();
      while (m_requests.size() >= HistoryLimit && remaining-- && !m_requestOrder.empty()) {
        PresentRequestIdentity identity = std::move(m_requestOrder.front());
        m_requestOrder.pop_front();
        auto it = m_requests.find(identity);
        if (it == m_requests.end()) {
          continue;
        } else if (it->second.state == RequestState::Terminal) {
          m_requests.erase(it);
        } else {
          m_requestOrder.push_back(std::move(identity));
        }
      }
    }

    void pruneWsiHistory() {
      while (m_wsiRecords.size() >= HistoryLimit && !m_wsiOrder.empty()) {
        WsiPresentId id = m_wsiOrder.front();
        auto it = m_wsiRecords.find(id);
        if (it != m_wsiRecords.end() && it->second.terminal
         && it->second.signalPublished) {
          m_wsiRecords.erase(it);
          m_wsiOrder.pop_front();
        } else if (it == m_wsiRecords.end()) {
          m_wsiOrder.pop_front();
        } else {
          break;
        }
      }
    }

    mutable std::mutex m_mutex;
    uint64_t m_nextAppFrameId = 1;
    uint64_t m_nextInternalPresentId = 1;
    uint64_t m_nextWsiPresentId = 1;
    std::map<PresentRequestIdentity, RequestRecord> m_requests;
    std::deque<PresentRequestIdentity> m_requestOrder;
    std::map<WsiPresentId, WsiRecord> m_wsiRecords;
    std::deque<WsiPresentId> m_wsiOrder;
    PresentTimingFeedback m_sourceFeedback;
  };

}
