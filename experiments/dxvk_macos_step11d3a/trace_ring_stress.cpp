#include "trace_ring.hpp"

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <pthread.h>
#include <mach/mach_time.h>
#include <thread>
#include <vector>
#include <chrono>

namespace {

struct Event {
  std::uint64_t event_id;
  std::uint64_t timestamp;
  std::uint64_t thread_id;
  std::uint32_t producer;
  std::uint32_t ordinal;
  std::uint64_t words[4];
};

constexpr std::size_t kCapacity = 32768;
constexpr std::uint32_t kProducerCount = 4;
constexpr std::uint32_t kEventsPerProducer = 500000;
constexpr std::uint64_t kEventCount =
    static_cast<std::uint64_t>(kProducerCount) * kEventsPerProducer;

std::uint64_t mach_ticks_to_ns(std::uint64_t ticks) {
  static const mach_timebase_info_data_t timebase = [] {
    mach_timebase_info_data_t value{};
    (void)mach_timebase_info(&value);
    return value;
  }();
  if (timebase.denom == 0) return 0;
  return static_cast<std::uint64_t>(
      (static_cast<__uint128_t>(ticks) * timebase.numer) / timebase.denom);
}

std::uint64_t payload_word(std::uint32_t producer, std::uint32_t ordinal,
                           std::uint32_t lane) {
  std::uint64_t value = (static_cast<std::uint64_t>(producer) << 48) ^
                        (static_cast<std::uint64_t>(ordinal) << 8) ^ lane;
  value ^= 0x9e3779b97f4a7c15ULL;
  value ^= value >> 30;
  value *= 0xbf58476d1ce4e5b9ULL;
  value ^= value >> 27;
  value *= 0x94d049bb133111ebULL;
  return value ^ (value >> 31);
}

Event make_event(std::uint64_t id, std::uint32_t producer,
                 std::uint32_t ordinal) {
  Event event{};
  event.event_id = id;
  event.timestamp = mach_absolute_time();
  (void)pthread_threadid_np(pthread_self(), &event.thread_id);
  event.producer = producer;
  event.ordinal = ordinal;
  for (std::uint32_t lane = 0; lane < 4; ++lane) {
    event.words[lane] = payload_word(producer, ordinal, lane);
  }
  return event;
}

bool verify_event(const Event& event) {
  if (event.producer >= kProducerCount || event.ordinal >= kEventsPerProducer ||
      event.event_id == 0 || event.thread_id == 0 || event.timestamp == 0) {
    return false;
  }
  for (std::uint32_t lane = 0; lane < 4; ++lane) {
    if (event.words[lane] != payload_word(event.producer, event.ordinal, lane)) {
      return false;
    }
  }
  return true;
}

bool test_full_and_reuse() {
  fgmetal::MpscRing<Event, 4> ring;
  if (!ring.initialized()) return false;
  for (std::uint64_t id = 1; id <= 4; ++id) {
    Event event = make_event(id, 0, static_cast<std::uint32_t>(id - 1));
    std::uint64_t ticket = 99;
    if (!ring.try_push(event, &ticket) || ticket != id - 1) return false;
  }
  std::uint64_t ticket = 99;
  Event overflow = make_event(5, 0, 4);
  if (ring.try_push(overflow, &ticket) || ticket != 99) return false;
  for (std::uint64_t id = 1; id <= 4; ++id) {
    Event event{};
    if (!ring.try_pop(event, &ticket) || ticket != id - 1 ||
        event.event_id != id || !verify_event(event)) return false;
  }
  Event after_reuse = make_event(5, 0, 4);
  if (!ring.try_push(after_reuse, &ticket) || ticket != 4) return false;
  Event popped{};
  return ring.try_pop(popped, &ticket) && ticket == 4 && popped.event_id == 5 &&
         verify_event(popped) && ring.empty();
}

bool stress(std::uint64_t* accepted_out, std::uint64_t* dropped_out,
            std::uint64_t* elapsed_ns_out) {
  if (!test_full_and_reuse()) {
    std::fprintf(stderr, "FAIL: full-ring/reuse test\n");
    return false;
  }

  fgmetal::MpscRing<Event, kCapacity> ring;
  if (!ring.initialized()) return false;
  std::vector<std::uint8_t> accepted(static_cast<std::size_t>(kEventCount), 0);
  std::vector<std::uint8_t> seen(static_cast<std::size_t>(kEventCount), 0);
  std::vector<std::uint8_t> event_ids_seen(static_cast<std::size_t>(kEventCount + 1), 0);
  std::vector<std::uint8_t> event_ids_issued(static_cast<std::size_t>(kEventCount + 1), 0);
  std::vector<std::uint64_t> row_event_ids(static_cast<std::size_t>(kEventCount), 0);
  std::atomic<std::uint64_t> next_event_id{1};
  std::atomic<std::uint64_t> accepted_count{0};
  std::atomic<std::uint64_t> dropped_count{0};
  std::atomic<std::uint32_t> producers_done{0};
  std::atomic<bool> consumer_failed{false};
  std::thread consumer([&] {
    std::this_thread::sleep_for(std::chrono::milliseconds(10));  // Force consumer lag.
    while (producers_done.load(std::memory_order_acquire) != kProducerCount ||
           !ring.empty()) {
      Event event{};
      if (!ring.try_pop(event)) {
        std::this_thread::yield();
        continue;
      }
      if (!verify_event(event) || event.event_id > kEventCount ||
          event_ids_seen[static_cast<std::size_t>(event.event_id)] != 0) {
        consumer_failed.store(true, std::memory_order_relaxed);
        continue;
      }
      const std::size_t row = static_cast<std::size_t>(event.producer) *
                              kEventsPerProducer + event.ordinal;
      if (seen[row] != 0) {
        consumer_failed.store(true, std::memory_order_relaxed);
      }
      if (row_event_ids[row] != event.event_id) {
        consumer_failed.store(true, std::memory_order_relaxed);
      }
      seen[row] = 1;
      event_ids_seen[static_cast<std::size_t>(event.event_id)] = 1;
    }
  });

  const auto start = std::chrono::steady_clock::now();
  std::vector<std::thread> producers;
  for (std::uint32_t producer = 0; producer < kProducerCount; ++producer) {
    producers.emplace_back([&, producer] {
      for (std::uint32_t ordinal = 0; ordinal < kEventsPerProducer; ++ordinal) {
        const std::uint64_t id = next_event_id.fetch_add(1, std::memory_order_relaxed);
        event_ids_issued[static_cast<std::size_t>(id)] = 1;
        Event event = make_event(id, producer, ordinal);
        const std::size_t row = static_cast<std::size_t>(producer) *
                                kEventsPerProducer + ordinal;
        row_event_ids[row] = id;
        if (ring.try_push(event)) {
          accepted[row] = 1;
          accepted_count.fetch_add(1, std::memory_order_relaxed);
        } else {
          dropped_count.fetch_add(1, std::memory_order_relaxed);
        }
      }
      producers_done.fetch_add(1, std::memory_order_release);
    });
  }
  for (auto& producer : producers) producer.join();
  const auto stop = std::chrono::steady_clock::now();
  consumer.join();

  std::uint64_t accepted_rows = 0;
  std::uint64_t seen_rows = 0;
  for (std::size_t row = 0; row < static_cast<std::size_t>(kEventCount); ++row) {
    accepted_rows += accepted[row];
    seen_rows += seen[row];
    if (seen[row] != accepted[row]) {
      std::fprintf(stderr, "FAIL: row %zu accepted=%u seen=%u\n", row,
                   accepted[row], seen[row]);
      return false;
    }
  }
  const std::uint64_t dropped = dropped_count.load(std::memory_order_relaxed);
  const std::uint64_t accepted_total = accepted_count.load(std::memory_order_relaxed);
  std::uint64_t issued_ids = 0;
  std::uint64_t missing_accepted_ids = 0;
  for (std::uint64_t id = 1; id <= kEventCount; ++id) {
    issued_ids += event_ids_issued[static_cast<std::size_t>(id)];
    missing_accepted_ids += !event_ids_seen[static_cast<std::size_t>(id)];
  }
  const auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(stop - start).count();
  if (accepted_rows != accepted_total || seen_rows != accepted_total ||
      accepted_total + dropped != kEventCount || !ring.empty() ||
      issued_ids != kEventCount || missing_accepted_ids != dropped ||
      consumer_failed.load(std::memory_order_relaxed)) {
    std::fprintf(stderr,
        "FAIL: attempted=%llu accepted=%llu dropped=%llu seen=%llu empty=%d\n",
        static_cast<unsigned long long>(kEventCount),
        static_cast<unsigned long long>(accepted_total),
        static_cast<unsigned long long>(dropped),
        static_cast<unsigned long long>(seen_rows), ring.empty());
    return false;
  }
  *accepted_out = accepted_total;
  *dropped_out = dropped;
  *elapsed_ns_out = static_cast<std::uint64_t>(elapsed);
  return true;
}

bool shutdown_while_producers_finish() {
  constexpr std::uint32_t producer_count = 4;
  constexpr std::uint32_t events_per_producer = 200000;
  constexpr std::uint64_t max_events =
      static_cast<std::uint64_t>(producer_count) * events_per_producer;
  using ShutdownRing = fgmetal::MpscRing<Event, 1024>;
  ShutdownRing ring;
  if (!ring.initialized()) return false;

  fgmetal::ProducerGate gate;
  std::atomic<std::uint64_t> attempted{0};
  std::atomic<std::uint64_t> accepted{0};
  std::atomic<std::uint64_t> full_dropped{0};
  std::atomic<std::uint64_t> shutdown_rejected{0};
  std::atomic<std::uint32_t> producers_done{0};
  std::atomic<bool> consumer_failed{false};
  std::vector<std::uint8_t> seen(static_cast<std::size_t>(max_events + 1), 0);
  std::vector<std::uint8_t> issued(static_cast<std::size_t>(max_events + 1), 0);
  std::vector<std::uint64_t> row_event_ids(
      static_cast<std::size_t>(producer_count) * events_per_producer, 0);
  std::atomic<std::uint64_t> next_event_id{1};

  std::thread consumer([&] {
    while (!gate.closed() || gate.active() != 0 ||
           producers_done.load(std::memory_order_acquire) != producer_count ||
           !ring.empty()) {
      Event event{};
      if (!ring.try_pop(event)) {
        std::this_thread::yield();
        continue;
      }
      if (!verify_event(event) || event.event_id > max_events ||
          seen[static_cast<std::size_t>(event.event_id)] != 0) {
        consumer_failed.store(true, std::memory_order_relaxed);
        continue;
      }
      const std::size_t row = static_cast<std::size_t>(event.producer) *
                              events_per_producer + event.ordinal;
      if (row >= row_event_ids.size() || row_event_ids[row] != event.event_id) {
        consumer_failed.store(true, std::memory_order_relaxed);
      }
      seen[static_cast<std::size_t>(event.event_id)] = 1;
    }
  });

  std::vector<std::thread> producers;
  for (std::uint32_t producer = 0; producer < producer_count; ++producer) {
    producers.emplace_back([&, producer] {
      for (std::uint32_t ordinal = 0; ordinal < events_per_producer; ++ordinal) {
        if (!gate.try_enter()) {
          shutdown_rejected.fetch_add(1, std::memory_order_relaxed);
          break;
        }
        attempted.fetch_add(1, std::memory_order_relaxed);
        const std::uint64_t id = next_event_id.fetch_add(1, std::memory_order_relaxed);
        issued[static_cast<std::size_t>(id)] = 1;
        row_event_ids[static_cast<std::size_t>(producer) * events_per_producer + ordinal] = id;
        Event event = make_event(id, producer, ordinal);
        if (ring.try_push(event)) {
          accepted.fetch_add(1, std::memory_order_relaxed);
        } else {
          full_dropped.fetch_add(1, std::memory_order_relaxed);
        }
        gate.leave();
      }
      producers_done.fetch_add(1, std::memory_order_release);
    });
  }

  while (attempted.load(std::memory_order_relaxed) < 10000) {
    std::this_thread::yield();
  }
  gate.close();
  for (auto& producer : producers) producer.join();
  consumer.join();

  std::uint64_t seen_count = 0;
  for (std::uint8_t value : seen) seen_count += value;
  const std::uint64_t accepted_count = accepted.load(std::memory_order_relaxed);
  const std::uint64_t dropped_count = full_dropped.load(std::memory_order_relaxed);
  const std::uint64_t rejected_count = shutdown_rejected.load(std::memory_order_relaxed);
  const std::uint64_t rejected_before_late_calls = gate.rejected_count();
  constexpr std::uint32_t late_thread_count = 8;
  constexpr std::uint32_t late_calls_per_thread = 10000;
  std::atomic<std::uint64_t> late_rejected{0};
  std::vector<std::thread> late_producers;
  for (std::uint32_t index = 0; index < late_thread_count; ++index) {
    late_producers.emplace_back([&] {
      for (std::uint32_t call = 0; call < late_calls_per_thread; ++call) {
        if (!gate.try_enter()) {
          late_rejected.fetch_add(1, std::memory_order_relaxed);
        }
      }
    });
  }
  for (auto& producer : late_producers) producer.join();
  const std::uint64_t gate_rejected_count = gate.rejected_count();
  const std::uint64_t late_attempt_count = late_rejected.load(std::memory_order_relaxed);
  std::uint64_t issued_count = 0;
  std::uint64_t missing_count = 0;
  const std::uint64_t attempted_count = attempted.load(std::memory_order_relaxed);
  for (std::uint64_t id = 1; id <= attempted_count; ++id) {
    issued_count += issued[static_cast<std::size_t>(id)];
    missing_count += !seen[static_cast<std::size_t>(id)];
  }
  if (attempted_count != accepted_count + dropped_count ||
      issued_count != attempted_count || missing_count != dropped_count ||
      seen_count != accepted_count || !ring.empty() || gate.active() != 0 ||
      !gate.closed() ||
      late_attempt_count != static_cast<std::uint64_t>(late_thread_count) * late_calls_per_thread ||
      gate_rejected_count != rejected_before_late_calls || gate_rejected_count != 0 ||
      consumer_failed.load(std::memory_order_relaxed)) {
    std::fprintf(stderr,
        "FAIL: shutdown attempted=%llu accepted=%llu full_dropped=%llu rejected=%llu seen=%llu\n",
        static_cast<unsigned long long>(attempted_count),
        static_cast<unsigned long long>(accepted_count),
        static_cast<unsigned long long>(dropped_count),
        static_cast<unsigned long long>(rejected_count),
        static_cast<unsigned long long>(seen_count));
    return false;
  }
  std::printf("SHUTDOWN_TEST=PASS attempted=%llu accepted=%llu full_dropped=%llu shutdown_rejected=%llu gate_rejections=%llu late_rejected=%llu drained=%llu\n",
      static_cast<unsigned long long>(attempted.load(std::memory_order_relaxed)),
      static_cast<unsigned long long>(accepted_count),
      static_cast<unsigned long long>(dropped_count),
      static_cast<unsigned long long>(rejected_count),
      static_cast<unsigned long long>(gate_rejected_count),
      static_cast<unsigned long long>(late_attempt_count),
      static_cast<unsigned long long>(seen_count));
  return true;
}

std::uint64_t percentile(const std::vector<std::uint64_t>& sorted,
                         double p) {
  const std::size_t index = static_cast<std::size_t>(
      p * static_cast<double>(sorted.size() - 1));
  return sorted[index];
}

bool benchmark(std::uint64_t count) {
  struct TraceLine {
    std::uint32_t length;
    char bytes[2048];
  };
  using BenchRing = fgmetal::MpscRing<TraceLine, 32768>;
  BenchRing ring;
  if (!ring.initialized() || count == 0) return false;
  fgmetal::ProducerGate gate;
  std::vector<std::uint64_t> samples;
  samples.reserve(static_cast<std::size_t>(count));
  std::atomic<std::uint64_t> event_id{1};
  std::atomic<bool> producer_done{false};
  std::atomic<std::uint64_t> consumed{0};
  std::thread consumer([&] {
    TraceLine event{};
    while (!producer_done.load(std::memory_order_acquire) || gate.active() != 0 || !ring.empty()) {
      if (ring.try_pop(event)) {
        consumed.fetch_add(1, std::memory_order_relaxed);
      } else {
        std::this_thread::yield();
      }
    }
  });

  const auto start = std::chrono::steady_clock::now();
  for (std::uint64_t index = 0; index < count; ++index) {
    const std::uint64_t emit_start_ticks = mach_absolute_time();
    if (!gate.try_enter()) {
      producer_done.store(true, std::memory_order_release);
      consumer.join();
      return false;
    }
    const std::uint64_t id = event_id.fetch_add(1, std::memory_order_relaxed);
    const std::uint64_t timestamp_ns = mach_ticks_to_ns(mach_absolute_time());
    std::uint64_t thread_id = 0;
    (void)pthread_threadid_np(pthread_self(), &thread_id);
    TraceLine line{};
    const int formatted = std::snprintf(
        line.bytes, sizeof(line.bytes),
        "{\"origin\":\"MVK_SOURCE\",\"event_id\":%llu,\"ts_mach_ns\":%llu,\"tid\":%llu,\"event\":\"next_drawable_end\",\"app_frame_id\":%llu,\"acquisition_sequence\":%llu,\"image_index\":%u,\"driver_submit_seq\":%llu}\n",
        static_cast<unsigned long long>(id),
        static_cast<unsigned long long>(timestamp_ns),
        static_cast<unsigned long long>(thread_id),
        static_cast<unsigned long long>(index + 1),
        static_cast<unsigned long long>(index + 11),
        static_cast<unsigned int>(index % 3),
        static_cast<unsigned long long>(index + 31));
    if (formatted <= 0 || static_cast<std::size_t>(formatted) >= sizeof(line.bytes)) {
      gate.leave();
      producer_done.store(true, std::memory_order_release);
      gate.close();
      consumer.join();
      return false;
    }
    line.length = static_cast<std::uint32_t>(formatted);
    const bool ok = ring.try_push(line);
    gate.leave();
    const std::uint64_t emit_end_ticks = mach_absolute_time();
    if (!ok) {
      producer_done.store(true, std::memory_order_release);
      consumer.join();
      std::fprintf(stderr, "benchmark ring unexpectedly full at %llu\n",
                   static_cast<unsigned long long>(index));
      return false;
    }
    samples.push_back(mach_ticks_to_ns(emit_end_ticks - emit_start_ticks));
  }
  gate.close();
  producer_done.store(true, std::memory_order_release);
  consumer.join();
  const auto stop = std::chrono::steady_clock::now();
  if (consumed.load(std::memory_order_relaxed) != count) return false;
  std::sort(samples.begin(), samples.end());
  const std::uint64_t median = percentile(samples, 0.50);
  const std::uint64_t p95 = percentile(samples, 0.95);
  const std::uint64_t p99 = percentile(samples, 0.99);
  const std::uint64_t max = samples.back();
  const auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(stop - start).count();
  const double throughput = static_cast<double>(count) * 1.0e9 /
                            static_cast<double>(elapsed);
  std::printf("SYNTHETIC_EMISSION_BENCH count=%llu min_ns=%llu median_ns=%llu p95_ns=%llu p99_ns=%llu max_ns=%llu throughput_events_s=%.0f payload=fixed_json_format_plus_event_id_clock_tid_gate_and_2048_byte_ring_publication\n",
      static_cast<unsigned long long>(count),
      static_cast<unsigned long long>(samples.front()),
      static_cast<unsigned long long>(median),
      static_cast<unsigned long long>(p95),
      static_cast<unsigned long long>(p99),
      static_cast<unsigned long long>(max), throughput);
  return true;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc >= 2 && std::strcmp(argv[1], "--full-only") == 0) {
    const bool ok = test_full_and_reuse();
    std::printf("FULL_RING_TEST=%s\n", ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
  }
  if (argc >= 2 && std::strcmp(argv[1], "--stress-only") == 0) {
    if (!shutdown_while_producers_finish()) return 1;
    std::uint64_t accepted = 0, dropped = 0, elapsed = 0;
    const bool ok = stress(&accepted, &dropped, &elapsed);
    std::printf("STRESS_TEST=%s producers=%u events=%llu accepted=%llu dropped=%llu elapsed_ns=%llu\n",
        ok ? "PASS" : "FAIL", kProducerCount,
        static_cast<unsigned long long>(kEventCount),
        static_cast<unsigned long long>(accepted),
        static_cast<unsigned long long>(dropped),
        static_cast<unsigned long long>(elapsed));
    return ok ? 0 : 1;
  }
  if (argc >= 2 && std::strcmp(argv[1], "--benchmark-only") == 0) {
    const std::uint64_t count = argc >= 3 ? std::strtoull(argv[2], nullptr, 10) : 100000;
    return benchmark(count) ? 0 : 1;
  }

  if (!shutdown_while_producers_finish()) return 1;

  std::uint64_t accepted = 0, dropped = 0, elapsed = 0;
  if (!stress(&accepted, &dropped, &elapsed)) return 1;
  const double throughput = static_cast<double>(accepted) * 1.0e9 /
                            static_cast<double>(elapsed);
  std::printf("STRESS_TEST=PASS producers=%u events=%llu accepted=%llu dropped=%llu elapsed_ns=%llu throughput_events_s=%.0f\n",
      kProducerCount, static_cast<unsigned long long>(kEventCount),
      static_cast<unsigned long long>(accepted),
      static_cast<unsigned long long>(dropped),
      static_cast<unsigned long long>(elapsed), throughput);
  return benchmark(100000) ? 0 : 1;
}
