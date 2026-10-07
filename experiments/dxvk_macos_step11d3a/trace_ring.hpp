#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <new>
#include <type_traits>

namespace fgmetal {

// Admission and close share one atomic word. A producer's fetch_add either
// precedes close and is counted, or observes the closed bit and immediately
// balances its temporary reference. Closed-gate attempts are not added to the
// open-session rejection counter, so the shutdown summary remains stable after
// the gate closes. Every attempt uses a fixed number of atomic operations.
class ProducerGate final {
  static constexpr std::uint64_t kClosedBit = std::uint64_t{1} << 63;
  static constexpr std::uint64_t kActiveMask = kClosedBit - 1;

 public:
  ProducerGate() noexcept = default;
  ProducerGate(const ProducerGate&) = delete;
  ProducerGate& operator=(const ProducerGate&) = delete;

  bool try_enter() noexcept {
    const std::uint64_t previous =
        state_.fetch_add(1, std::memory_order_acq_rel);
    if ((previous & kClosedBit) != 0) {
      state_.fetch_sub(1, std::memory_order_release);
      return false;
    }
    if ((previous & kActiveMask) == kActiveMask) {
      rejected_.fetch_add(1, std::memory_order_relaxed);
      state_.fetch_sub(1, std::memory_order_release);
      return false;
    }
    return true;
  }

  void leave() noexcept {
    state_.fetch_sub(1, std::memory_order_release);
  }

  void close() noexcept {
    state_.fetch_or(kClosedBit, std::memory_order_acq_rel);
  }

  std::uint64_t active() const noexcept {
    return state_.load(std::memory_order_acquire) & kActiveMask;
  }

  bool closed() const noexcept {
    return (state_.load(std::memory_order_acquire) & kClosedBit) != 0;
  }

  std::uint64_t rejected_count() const noexcept {
    return rejected_.load(std::memory_order_relaxed);
  }

  // Called only before the drain thread is started and before producers are
  // admitted for a new trace session.
  void reset() noexcept {
    state_.store(0, std::memory_order_relaxed);
    rejected_.store(0, std::memory_order_relaxed);
  }

 private:
  alignas(64) std::atomic<std::uint64_t> state_{0};
  std::atomic<std::uint64_t> rejected_{0};
};

// Bounded multi-producer, single-consumer queue based on per-slot sequence
// numbers. A producer claims a slot only while it is FREE; a full queue does
// not advance enqueue_pos_, so a drop cannot leave an unpublishable hole.
// Payload publication is sequence.store(release), paired with the consumer's
// sequence.load(acquire). Reuse is sequence.store(release) after the consumer
// copies the payload, paired with the next producer's acquire load.
template <typename Payload, std::size_t Capacity, std::size_t MaxEnqueueAttempts = 32>
class MpscRing final {
  static_assert(std::is_trivially_copyable<Payload>::value,
                "MpscRing payload must be trivially copyable");
  static_assert(Capacity >= 2 && (Capacity & (Capacity - 1)) == 0,
                "MpscRing capacity must be a power of two");
  static_assert(MaxEnqueueAttempts > 0, "MpscRing needs a bounded retry budget");
  static_assert(std::atomic<std::uint64_t>::is_always_lock_free,
                "MpscRing requires lock-free 64-bit atomics");

  struct Slot {
    std::atomic<std::uint64_t> sequence{0};
    Payload payload{};
  };

 public:
  MpscRing() noexcept : slots_(new (std::nothrow) Slot[Capacity]) {
    if (!slots_) {
      return;
    }
    for (std::uint64_t index = 0; index < Capacity; ++index) {
      slots_[index].sequence.store(index, std::memory_order_relaxed);
    }
  }

  MpscRing(const MpscRing&) = delete;
  MpscRing& operator=(const MpscRing&) = delete;

  bool initialized() const noexcept { return static_cast<bool>(slots_); }

  // Bounded work: at most MaxEnqueueAttempts compare/exchange attempts. A
  // false return is an explicitly accounted trace drop (full, allocation
  // failure, or a short contention retry budget).
  bool try_push(const Payload& payload, std::uint64_t* ticket = nullptr) noexcept {
    if (!slots_) {
      return false;
    }
    std::uint64_t position = enqueue_pos_.load(std::memory_order_relaxed);
    for (std::size_t attempt = 0; attempt < MaxEnqueueAttempts; ++attempt) {
      // Stop before unsigned position arithmetic could wrap and make an old
      // generation appear current. This limit is unreachable for practical
      // experiment lengths, but keeps the sequence protocol defined.
      if (position > UINT64_MAX - Capacity - 1) {
        return false;
      }
      Slot& slot = slots_[position & (Capacity - 1)];
      const std::uint64_t sequence = slot.sequence.load(std::memory_order_acquire);
      if (sequence == position) {
        if (enqueue_pos_.compare_exchange_weak(position, position + 1,
                                                std::memory_order_relaxed,
                                                std::memory_order_relaxed)) {
          slot.payload = payload;
          slot.sequence.store(position + 1, std::memory_order_release);
          if (ticket) {
            *ticket = position;
          }
          return true;
        }
      } else if (sequence < position) {
        return false;
      } else {
        position = enqueue_pos_.load(std::memory_order_relaxed);
      }
    }
    return false;
  }

  // Exactly one consumer may call try_pop. A false return means the next
  // FIFO slot is not yet published or the queue is empty; it is never skipped.
  bool try_pop(Payload& payload, std::uint64_t* ticket = nullptr) noexcept {
    if (!slots_) {
      return false;
    }
    const std::uint64_t position = dequeue_pos_;
    Slot& slot = slots_[position & (Capacity - 1)];
    const std::uint64_t sequence = slot.sequence.load(std::memory_order_acquire);
    if (sequence != position + 1) {
      return false;
    }
    payload = slot.payload;
    slot.sequence.store(position + Capacity, std::memory_order_release);
    dequeue_pos_ = position + 1;
    if (ticket) {
      *ticket = position;
    }
    return true;
  }

  bool empty() const noexcept {
    return dequeue_pos_ == enqueue_pos_.load(std::memory_order_acquire);
  }

 private:
  std::unique_ptr<Slot[]> slots_;
  alignas(64) std::atomic<std::uint64_t> enqueue_pos_{0};
  alignas(64) std::uint64_t dequeue_pos_ = 0;
};

}  // namespace fgmetal
