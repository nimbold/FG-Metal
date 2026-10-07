#pragma once

#include <cstddef>
#include <cstdint>

extern "C" {
// Admission and event-ID allocation are one operation. The caller must pair a
// successful BeginEvent with exactly one EnqueueJson or FinishEvent call.
uint64_t mvkNativeTraceBeginEvent();
void mvkNativeTraceFinishEvent();
uint64_t mvkNativeTraceNow();
uint64_t mvkNativeTraceCurrentAppFrameID();
void mvkNativeTraceSetCurrentAppFrameID(uint64_t appFrameID);
bool mvkNativeTraceStart(const char *path);
bool mvkNativeTraceEnqueueJson(const char *line, size_t length);
bool mvkNativeTraceIsHealthy();
uint64_t mvkNativeTraceDroppedCount();
uint64_t mvkNativeTraceWriteFailureCount();
void mvkNativeTraceRecordSerializationFailure();
void mvkNativeTraceRegisterPresentedCallback();
void mvkNativeTraceCompletePresentedCallback();
uint64_t mvkNativeTracePendingPresentedCallbacks();
bool mvkNativeTraceStop();
}
