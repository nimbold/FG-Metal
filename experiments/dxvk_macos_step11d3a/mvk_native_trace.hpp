#pragma once

#include <cstddef>
#include <cstdint>

struct MVKNativeTraceRecord {
	uint64_t appFrameID = 0;
	bool hasAppFrameID = false;
	const void* swapchain = nullptr;
	const void* image = nullptr;
	uint32_t imageIndex = 0;
	bool hasImageIndex = false;
	uint64_t acquisitionSequence = 0;
	bool hasAcquisitionSequence = false;
	const void* drawable = nullptr;
	const void* texture = nullptr;
	const void* queue = nullptr;
	uint64_t drawableID = 0;
	bool hasDrawableID = false;
	uint64_t requestID = 0;
	bool hasRequestID = false;
	uint64_t submitSequence = 0;
	bool hasSubmitSequence = false;
	uint64_t presentSequence = 0;
	bool hasPresentSequence = false;
	uint64_t driverPresentSequence = 0;
	bool hasDriverPresentSequence = false;
	uint64_t presentId = 0;
	bool hasPresentId = false;
	uint32_t presentIDGoogle = 0;
	bool hasPresentIDGoogle = false;
	uint32_t attemptIndex = 0;
	bool hasAttemptIndex = false;
	uint64_t durationNs = 0;
	bool hasDurationNs = false;
	uint64_t callBeginNs = 0;
	bool hasCallBeginNs = false;
	uint64_t callEndNs = 0;
	bool hasCallEndNs = false;
	uint64_t actualPresentTimeNs = 0;
	bool hasActualPresentTimeNs = false;
	int32_t result = 0;
	bool hasResult = false;
	const char* detail = nullptr;
	bool isDisplayed = false;
	bool hasIsDisplayed = false;
};

extern "C" {
uint64_t mvkNativeTraceBeginEvent();
void mvkNativeTraceFinishEvent();
uint64_t mvkNativeTraceNow();
uint64_t mvkNativeTraceNextDrawableID();
uint64_t mvkNativeTraceNextDrawableRequestID();
uint64_t mvkNativeTraceNextSubmitSequence();
uint64_t mvkNativeTraceNextPresentSequence();
uint64_t mvkNativeTraceCurrentAppFrameID();
void mvkNativeTraceSetCurrentAppFrameID(uint64_t appFrameID);
uint64_t mvkNativeTraceCurrentSubmitSequence();
void mvkNativeTraceSetCurrentSubmitSequence(uint64_t submitSequence);
bool mvkNativeTraceStart(const char* path);
bool mvkNativeTraceEnqueueJson(const char* line, size_t length);
bool mvkNativeTraceIsHealthy();
uint64_t mvkNativeTraceDroppedCount();
uint64_t mvkNativeTraceWriteFailureCount();
void mvkNativeTraceRecordSerializationFailure();
void mvkNativeTraceRegisterPresentedCallback();
void mvkNativeTraceCompletePresentedCallback();
uint64_t mvkNativeTracePendingPresentedCallbacks();
bool mvkNativeTraceStop();
}

bool mvkNativeTraceEnabled();
void mvkNativeTraceLog(const char* event, const MVKNativeTraceRecord& record);
