// Included once by MVKImage.mm. Event producers only format into fixed stack
// buffers and publish to the bounded MPSC queue. The drain thread owns the fd.
#include "MVKNativeTraceRing.hpp"

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include <atomic>
#include <memory>

namespace {
	static constexpr size_t kMVKTraceCapacity = 32768;
	static constexpr size_t kMVKTraceLineCapacity = 2048;
	struct MVKTraceLine {
		uint32_t length = 0;
		char bytes[kMVKTraceLineCapacity]{};
	};
	using MVKTraceRing = fgmetal::MpscRing<MVKTraceLine, kMVKTraceCapacity>;

	static std::unique_ptr<MVKTraceRing> gMVKTraceRing;
	static std::atomic<bool> gMVKTraceEnabled{false};
	static std::atomic<bool> gMVKTraceRunning{false};
	static std::atomic<bool> gMVKTraceHealthy{true};
	static fgmetal::ProducerGate gMVKTraceProducerGate;
	static std::atomic<uint64_t> gMVKTraceEventID{0};
	static std::atomic<uint64_t> gMVKTraceDrawableID{0};
	static std::atomic<uint64_t> gMVKTraceDrawableRequestID{0};
	static std::atomic<uint64_t> gMVKTraceSubmitSequence{0};
	static std::atomic<uint64_t> gMVKTracePresentSequence{0};
	static std::atomic<uint64_t> gMVKTraceDropped{0};
	static std::atomic<uint64_t> gMVKTraceSerializationFailures{0};
	static std::atomic<uint64_t> gMVKTraceWriteFailures{0};
	static std::atomic<uint64_t> gMVKTracePendingPresentedCallbacks{0};
	static mach_timebase_info_data_t gMVKTraceTimebase{};
	static pthread_t gMVKTraceDrainThread{};
	static bool gMVKTraceDrainThreadCreated = false;
	static int gMVKTraceDescriptor = -1;
	static thread_local uint64_t gMVKTraceCurrentAppFrameID = 0;
	static thread_local uint64_t gMVKTraceCurrentSubmitSequence = 0;

	static uint64_t mvkTraceThreadID() {
		uint64_t threadID = 0;
		(void)pthread_threadid_np(pthread_self(), &threadID);
		return threadID;
	}

	static bool mvkTraceWriteAll(const char* bytes, size_t length) {
		size_t offset = 0;
		while (offset < length) {
			ssize_t count = write(gMVKTraceDescriptor, bytes + offset, length - offset);
			if (count < 0 && errno == EINTR) { continue; }
			if (count <= 0) { return false; }
			offset += (size_t)count;
		}
		return true;
	}

	static bool mvkTraceAppend(char* buffer, size_t capacity, size_t* length,
							   const char* format, ...) {
		if (*length >= capacity) { return false; }
		va_list args;
		va_start(args, format);
		int count = vsnprintf(buffer + *length, capacity - *length, format, args);
		va_end(args);
		if (count < 0 || (size_t)count >= capacity - *length) { return false; }
		*length += (size_t)count;
		return true;
	}

	static bool mvkTraceEnqueue(const MVKTraceLine& line) {
		const bool accepted = gMVKTraceRing && gMVKTraceRing->try_push(line);
		if (!accepted) { gMVKTraceDropped.fetch_add(1, std::memory_order_relaxed); }
		gMVKTraceProducerGate.leave();
		return accepted;
	}

	static bool mvkTraceWriteSummary() {
		char line[768]{};
		int length = snprintf(line, sizeof(line),
			"{\"origin\":\"LOGGER\",\"event_id\":%" PRIu64
			",\"ts_mach_ns\":%" PRIu64 ",\"tid\":%" PRIu64
			",\"event\":\"trace_dropped_summary\",\"dropped_count\":%" PRIu64
			",\"producer_gate_rejected_count\":%" PRIu64
			",\"serialization_failure_count\":%" PRIu64
			",\"write_failure_count_before_summary\":%" PRIu64
			",\"ring_capacity\":%zu}\n",
			gMVKTraceEventID.fetch_add(1, std::memory_order_relaxed) + 1,
			mvkNativeTraceNow(), mvkTraceThreadID(),
			gMVKTraceDropped.load(std::memory_order_relaxed),
			gMVKTraceProducerGate.rejected_count(),
			gMVKTraceSerializationFailures.load(std::memory_order_relaxed),
			gMVKTraceWriteFailures.load(std::memory_order_relaxed), kMVKTraceCapacity);
		return length > 0 && (size_t)length < sizeof(line) &&
			mvkTraceWriteAll(line, (size_t)length);
	}

	static void* mvkTraceDrainWorker(void*) {
		pthread_setname_np("mvk_trace_drain");
		MVKTraceLine line{};
		while (gMVKTraceRunning.load(std::memory_order_acquire) ||
			   gMVKTraceProducerGate.active() != 0 ||
			   (gMVKTraceRing && !gMVKTraceRing->empty())) {
			if (!gMVKTraceRing || !gMVKTraceRing->try_pop(line)) {
				usleep(500);
				continue;
			}
			if (!mvkTraceWriteAll(line.bytes, line.length)) {
				gMVKTraceWriteFailures.fetch_add(1, std::memory_order_relaxed);
				gMVKTraceHealthy.store(false, std::memory_order_release);
			}
		}
		return nullptr;
	}
}  // namespace

extern "C" __attribute__((visibility("default"))) uint64_t mvkNativeTraceBeginEvent() {
	if (!gMVKTraceProducerGate.try_enter()) { return 0; }
	return gMVKTraceEventID.fetch_add(1, std::memory_order_relaxed) + 1;
}

extern "C" __attribute__((visibility("default"))) void mvkNativeTraceFinishEvent() {
	gMVKTraceProducerGate.leave();
}

extern "C" __attribute__((visibility("default"))) uint64_t mvkNativeTraceNow() {
	const uint64_t ticks = mach_absolute_time();
	if (gMVKTraceTimebase.denom == 0) { return 0; }
	return (uint64_t)(((__uint128_t)ticks * gMVKTraceTimebase.numer) /
					  gMVKTraceTimebase.denom);
}

extern "C" __attribute__((visibility("default"))) uint64_t mvkNativeTraceNextDrawableID() {
	return gMVKTraceDrawableID.fetch_add(1, std::memory_order_relaxed) + 1;
}

extern "C" __attribute__((visibility("default"))) uint64_t mvkNativeTraceNextDrawableRequestID() {
	return gMVKTraceDrawableRequestID.fetch_add(1, std::memory_order_relaxed) + 1;
}

extern "C" __attribute__((visibility("default"))) uint64_t mvkNativeTraceNextSubmitSequence() {
	return gMVKTraceSubmitSequence.fetch_add(1, std::memory_order_relaxed) + 1;
}

extern "C" __attribute__((visibility("default"))) uint64_t mvkNativeTraceNextPresentSequence() {
	return gMVKTracePresentSequence.fetch_add(1, std::memory_order_relaxed) + 1;
}

extern "C" __attribute__((visibility("default"))) uint64_t mvkNativeTraceCurrentAppFrameID() {
	return gMVKTraceCurrentAppFrameID;
}

extern "C" __attribute__((visibility("default"))) void mvkNativeTraceSetCurrentAppFrameID(uint64_t appFrameID) {
	gMVKTraceCurrentAppFrameID = appFrameID;
}

extern "C" __attribute__((visibility("default"))) uint64_t mvkNativeTraceCurrentSubmitSequence() {
	return gMVKTraceCurrentSubmitSequence;
}

extern "C" __attribute__((visibility("default"))) void mvkNativeTraceSetCurrentSubmitSequence(uint64_t submitSequence) {
	gMVKTraceCurrentSubmitSequence = submitSequence;
}

extern "C" __attribute__((visibility("default"))) bool mvkNativeTraceStart(const char* path) {
	if (path == nullptr || path[0] == '\0' || gMVKTraceEnabled.load(std::memory_order_acquire)) {
		return false;
	}
	if (mach_timebase_info(&gMVKTraceTimebase) != KERN_SUCCESS || gMVKTraceTimebase.denom == 0) {
		return false;
	}
	gMVKTraceDescriptor = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
	if (gMVKTraceDescriptor < 0) { return false; }
	gMVKTraceRing.reset(new (std::nothrow) MVKTraceRing());
	if (!gMVKTraceRing || !gMVKTraceRing->initialized()) {
		close(gMVKTraceDescriptor);
		gMVKTraceDescriptor = -1;
		gMVKTraceRing.reset();
		return false;
	}
	gMVKTraceHealthy.store(true, std::memory_order_relaxed);
	gMVKTracePendingPresentedCallbacks.store(0, std::memory_order_relaxed);
	gMVKTraceRunning.store(true, std::memory_order_relaxed);
	gMVKTraceProducerGate.reset();
	pthread_attr_t attributes;
	pthread_attr_init(&attributes);
	(void)pthread_attr_set_qos_class_np(&attributes, QOS_CLASS_BACKGROUND, 0);
	const int created = pthread_create(&gMVKTraceDrainThread, &attributes, mvkTraceDrainWorker, nullptr);
	pthread_attr_destroy(&attributes);
	if (created != 0) {
		gMVKTraceProducerGate.close();
		gMVKTraceRunning.store(false, std::memory_order_release);
		close(gMVKTraceDescriptor);
		gMVKTraceDescriptor = -1;
		gMVKTraceRing.reset();
		return false;
	}
	gMVKTraceDrainThreadCreated = true;
	gMVKTraceEnabled.store(true, std::memory_order_release);
	return true;
}

extern "C" __attribute__((visibility("default"))) bool mvkNativeTraceEnqueueJson(
	const char* text, size_t length) {
	if (text == nullptr || length < 2 || length > kMVKTraceLineCapacity || text[length - 1] != '\n') {
		mvkNativeTraceRecordSerializationFailure();
		mvkNativeTraceFinishEvent();
		return false;
	}
	MVKTraceLine line{};
	line.length = (uint32_t)length;
	memcpy(line.bytes, text, length);
	return mvkTraceEnqueue(line);
}

extern "C" __attribute__((visibility("default"))) bool mvkNativeTraceIsHealthy() {
	return gMVKTraceEnabled.load(std::memory_order_acquire) &&
		gMVKTraceHealthy.load(std::memory_order_acquire);
}

extern "C" __attribute__((visibility("default"))) uint64_t mvkNativeTraceDroppedCount() {
	return gMVKTraceDropped.load(std::memory_order_relaxed);
}

extern "C" __attribute__((visibility("default"))) uint64_t mvkNativeTraceWriteFailureCount() {
	return gMVKTraceWriteFailures.load(std::memory_order_relaxed);
}

extern "C" __attribute__((visibility("default"))) void mvkNativeTraceRecordSerializationFailure() {
	gMVKTraceSerializationFailures.fetch_add(1, std::memory_order_relaxed);
	gMVKTraceDropped.fetch_add(1, std::memory_order_relaxed);
}

extern "C" __attribute__((visibility("default"))) void mvkNativeTraceRegisterPresentedCallback() {
	gMVKTracePendingPresentedCallbacks.fetch_add(1, std::memory_order_relaxed);
}

extern "C" __attribute__((visibility("default"))) void mvkNativeTraceCompletePresentedCallback() {
	gMVKTracePendingPresentedCallbacks.fetch_sub(1, std::memory_order_relaxed);
}

extern "C" __attribute__((visibility("default"))) uint64_t mvkNativeTracePendingPresentedCallbacks() {
	return gMVKTracePendingPresentedCallbacks.load(std::memory_order_acquire);
}

extern "C" __attribute__((visibility("default"))) bool mvkNativeTraceStop() {
	if (!gMVKTraceEnabled.exchange(false, std::memory_order_acq_rel)) { return false; }
	gMVKTraceProducerGate.close();
	gMVKTraceRunning.store(false, std::memory_order_release);
	if (gMVKTraceDrainThreadCreated) {
		pthread_join(gMVKTraceDrainThread, nullptr);
		gMVKTraceDrainThreadCreated = false;
	}
	const bool summaryWritten = gMVKTraceDescriptor >= 0 && mvkTraceWriteSummary();
	if (!summaryWritten) {
		gMVKTraceWriteFailures.fetch_add(1, std::memory_order_relaxed);
		gMVKTraceHealthy.store(false, std::memory_order_release);
	}
	if (gMVKTraceDescriptor >= 0) {
		if (fsync(gMVKTraceDescriptor) != 0) {
			gMVKTraceWriteFailures.fetch_add(1, std::memory_order_relaxed);
			gMVKTraceHealthy.store(false, std::memory_order_release);
		}
		close(gMVKTraceDescriptor);
		gMVKTraceDescriptor = -1;
	}
	gMVKTraceRing.reset();
	return gMVKTraceHealthy.load(std::memory_order_acquire);
}

bool mvkNativeTraceEnabled() {
	return gMVKTraceEnabled.load(std::memory_order_acquire);
}

void mvkNativeTraceLog(const char* event, const MVKNativeTraceRecord& record) {
	if (!mvkNativeTraceEnabled()) { return; }
	const uint64_t eventID = mvkNativeTraceBeginEvent();
	if (eventID == 0) { return; }
	char line[kMVKTraceLineCapacity]{};
	size_t length = 0;
	bool ok = mvkTraceAppend(line, sizeof(line), &length,
		"{\"origin\":\"MVK_SOURCE\",\"event_id\":%" PRIu64
		",\"ts_mach_ns\":%" PRIu64 ",\"tid\":%" PRIu64 ",\"event\":\"%s\"",
		eventID, mvkNativeTraceNow(), mvkTraceThreadID(),
		event == nullptr ? "unknown" : event);
#define MVK_TRACE_FIELD(condition, ...) \
	if (ok && (condition)) { ok = mvkTraceAppend(line, sizeof(line), &length, __VA_ARGS__); }
	MVK_TRACE_FIELD(record.swapchain, ",\"swapchain_id\":%" PRIuPTR, (uintptr_t)record.swapchain);
	MVK_TRACE_FIELD(record.hasAppFrameID, ",\"app_frame_id\":%" PRIu64, record.appFrameID);
	MVK_TRACE_FIELD(record.image, ",\"image_id\":%" PRIuPTR, (uintptr_t)record.image);
	MVK_TRACE_FIELD(record.hasImageIndex, ",\"image_index\":%u", record.imageIndex);
	MVK_TRACE_FIELD(record.hasAcquisitionSequence,
		",\"acquisition_sequence\":%" PRIu64, record.acquisitionSequence);
	MVK_TRACE_FIELD(record.drawable, ",\"drawable_identity\":%" PRIuPTR, (uintptr_t)record.drawable);
	MVK_TRACE_FIELD(record.hasDrawableID, ",\"drawable_id\":%" PRIu64, record.drawableID);
	MVK_TRACE_FIELD(record.texture, ",\"texture_id\":%" PRIuPTR, (uintptr_t)record.texture);
	MVK_TRACE_FIELD(record.queue, ",\"queue_id\":%" PRIuPTR, (uintptr_t)record.queue);
	MVK_TRACE_FIELD(record.hasRequestID, ",\"request_id\":%" PRIu64, record.requestID);
	MVK_TRACE_FIELD(record.hasSubmitSequence,
		",\"driver_submit_seq\":%" PRIu64, record.submitSequence);
	MVK_TRACE_FIELD(record.hasPresentSequence,
		",\"present_seq\":%" PRIu64, record.presentSequence);
	MVK_TRACE_FIELD(record.hasDriverPresentSequence,
		",\"driver_present_seq\":%" PRIu64, record.driverPresentSequence);
	MVK_TRACE_FIELD(record.hasPresentId, ",\"present_id\":%" PRIu64, record.presentId);
	MVK_TRACE_FIELD(record.hasPresentIDGoogle,
		",\"present_id_google\":%u", record.presentIDGoogle);
	MVK_TRACE_FIELD(record.hasAttemptIndex,
		",\"attempt_index\":%u", record.attemptIndex);
	MVK_TRACE_FIELD(record.hasCallBeginNs,
		",\"call_begin_mach_ns\":%" PRIu64, record.callBeginNs);
	MVK_TRACE_FIELD(record.hasCallEndNs,
		",\"call_end_mach_ns\":%" PRIu64, record.callEndNs);
	MVK_TRACE_FIELD(record.hasDurationNs, ",\"duration_ns\":%" PRIu64, record.durationNs);
	MVK_TRACE_FIELD(record.hasActualPresentTimeNs,
		",\"presented_time_mach_ns\":%" PRIu64
		",\"presentation_clock_domain\":\"mach_absolute_time_ns\"",
		record.actualPresentTimeNs);
	MVK_TRACE_FIELD(record.hasResult, ",\"vk_result\":%d", record.result);
	MVK_TRACE_FIELD(record.hasIsDisplayed,
		",\"is_displayed\":%s", record.isDisplayed ? "true" : "false");
	if (ok && record.detail != nullptr) {
		ok = mvkTraceAppend(line, sizeof(line), &length, ",\"detail\":\"%s\"", record.detail);
	}
#undef MVK_TRACE_FIELD
	if (!ok || !mvkTraceAppend(line, sizeof(line), &length, "}\n")) {
		mvkNativeTraceRecordSerializationFailure();
		mvkNativeTraceFinishEvent();
		return;
	}
	MVKTraceLine traceLine{};
	traceLine.length = (uint32_t)length;
	memcpy(traceLine.bytes, line, length);
	(void)mvkTraceEnqueue(traceLine);
}
