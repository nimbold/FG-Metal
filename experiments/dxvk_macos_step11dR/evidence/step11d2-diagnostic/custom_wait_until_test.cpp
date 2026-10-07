#include "util/thread.h"
#include "util/util_time.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <thread>

using namespace std::chrono_literals;

namespace {

int failures = 0;

void check(bool condition, const char* name) {
  std::printf("%s %s\n", condition ? "PASS" : "FAIL", name);
  failures += condition ? 0 : 1;
}

}

int main() {
  using Clock = dxvk::high_resolution_clock;
  static_assert(Clock::is_steady, "DXVK high-resolution clock must be monotonic");

  {
    dxvk::mutex mutex;
    dxvk::condition_variable condition;
    std::unique_lock<dxvk::mutex> lock(mutex);
    auto start = Clock::now();
    auto status = condition.wait_until(lock, start - 1ms);
    auto elapsed = Clock::now() - start;
    check(status == std::cv_status::timeout && elapsed < 20ms,
      "expired-deadline-times-out-immediately");
  }

  {
    dxvk::mutex mutex;
    dxvk::condition_variable condition;
    std::unique_lock<dxvk::mutex> lock(mutex);
    auto start = Clock::now();
    auto status = condition.wait_until(lock, start + 60ms);
    auto elapsed = Clock::now() - start;
    check(status == std::cv_status::timeout && elapsed >= 55ms && elapsed < 1s,
      "future-deadline-times-out-with-correct-sign");
  }

  {
    dxvk::mutex mutex;
    dxvk::condition_variable condition;
    bool ready = false;
    std::thread notifier([&] {
      std::this_thread::sleep_for(15ms);
      condition.notify_all(); // Deliberate spurious wake: predicate stays false.
    });

    std::unique_lock<dxvk::mutex> lock(mutex);
    auto start = Clock::now();
    bool result = condition.wait_until(lock, start + 80ms, [&] { return ready; });
    auto elapsed = Clock::now() - start;
    lock.unlock();
    notifier.join();
    check(!result && elapsed >= 75ms && elapsed < 1s,
      "predicate-loop-ignores-spurious-wake-until-deadline");
  }

  {
    dxvk::mutex mutex;
    dxvk::condition_variable condition;
    bool ready = false;
    std::thread notifier([&] {
      std::this_thread::sleep_for(15ms);
      {
        std::lock_guard<dxvk::mutex> lock(mutex);
        ready = true;
      }
      condition.notify_all();
    });

    std::unique_lock<dxvk::mutex> lock(mutex);
    bool result = condition.wait_until(lock, Clock::now() + 500ms, [&] { return ready; });
    lock.unlock();
    notifier.join();
    check(result, "predicate-wake-returns-success-before-deadline");
  }

  {
    dxvk::mutex mutex;
    dxvk::condition_variable condition;
    std::thread notifier([&] {
      std::this_thread::sleep_for(15ms);
      condition.notify_all();
    });

    std::unique_lock<dxvk::mutex> lock(mutex);
    auto farDeadline = Clock::now() + std::chrono::hours(24 * 50);
    auto status = condition.wait_until(lock, farDeadline);
    lock.unlock();
    notifier.join();
    check(status == std::cv_status::no_timeout,
      "far-deadline-timeout-conversion-allows-notification");
  }

  {
    dxvk::mutex mutex;
    dxvk::condition_variable condition;
    std::unique_lock<dxvk::mutex> lock(mutex);
    auto start = std::chrono::steady_clock::now();
    auto status = condition.wait_for(lock, -1ms);
    check(status == std::cv_status::timeout
        && std::chrono::steady_clock::now() - start < 20ms,
      "negative-wait-for-is-immediate-timeout");
  }

  return failures ? 1 : 0;
}
