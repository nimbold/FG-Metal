from pathlib import Path

R5 = Path('/private/tmp/fgmetal-step11d2-r5-diagnostic-source-c-20261006')
BRIDGE = Path('/private/tmp/fgmetal-step11d2-bridge-diagnostic-c-20261006/source')


def edit(path, replacements):
    text = path.read_text()
    for old, new in replacements:
        count = text.count(old)
        if count != 1:
            raise RuntimeError(f'{path}: expected one match, got {count}: {old[:100]!r}')
        text = text.replace(old, new, 1)
    path.write_text(text)

# Diagnostic-only protocol extension. The frozen R5 PE bridge/provider are untouched.
proto_append = '''\n  uint64_t trace_interop_job_id;\n  uint64_t trace_app_frame_id;\n  uint64_t trace_pe_begin_ns;\n  uint64_t trace_unix_entry_ns;\n  uint64_t trace_provider_entry_ns;\n  uint64_t trace_command_buffer_created_ns;\n  uint64_t trace_encoder_created_ns;\n  uint64_t trace_encode_begin_ns;\n  uint64_t trace_encode_end_ns;\n  uint64_t trace_commit_begin_ns;\n  uint64_t trace_commit_end_ns;\n  uint64_t trace_provider_return_ns;\n  uint64_t trace_unix_return_ns;\n  uint64_t trace_pe_return_ns;\n  uint64_t trace_provider_thread_id;\n  uint32_t trace_provider_queue_depth;\n  uint32_t trace_reserved;'''
for path in (BRIDGE / 'bridge_protocol.h', R5 / 'src/dxvk/dxvk_metal_bridge_protocol.h'):
    text = path.read_text()
    if text.count('uint64_t event_value;\n') != 1:
        raise RuntimeError(f'{path}: event_value field count mismatch')
    text = text.replace('uint64_t event_value;\n', 'uint64_t event_value;' + proto_append + '\n', 1)
    text = text.replace('sizeof(fgmb_packet) == 392', 'sizeof(fgmb_packet) == 520')
    path.write_text(text)

# Common timestamps use DXVK's QPC-backed nanosecond clock in the Windows build.
(R5 / 'src/util/r5_trace.h').write_text('''#pragma once\n#include <chrono>\n#include <cstdint>\n#include <functional>\n#include <thread>\n#include "util_time.h"\nnamespace dxvk {\n  inline uint64_t r5TraceNowNs() noexcept {\n    return static_cast<uint64_t>(high_resolution_clock::now().time_since_epoch().count());\n  }\n  inline uint64_t r5TraceThreadToken() noexcept {\n    return static_cast<uint64_t>(std::hash<std::thread::id>{}(std::this_thread::get_id()));\n  }\n}\n''')

# PE bridge timing around the synchronous Wine unixlib transition.
edit(BRIDGE / 'bridge_pe.c', [
    ('NTSTATUS WINAPI FgMetalBridgeCall(fgmb_packet *packet)\n{',
     '''static uint64_t bridge_qpc_ns(void)\n{\n    LARGE_INTEGER counter, frequency;\n    QueryPerformanceCounter(&counter);\n    QueryPerformanceFrequency(&frequency);\n    return (uint64_t)(((__uint128_t)counter.QuadPart * UINT64_C(1000000000)) /\n                      (uint64_t)frequency.QuadPart);\n}\n\nNTSTATUS WINAPI FgMetalBridgeCall(fgmb_packet *packet)\n{'''),
    ('    if (unixlib_status) return unixlib_status;\n    return WINE_UNIX_CALL(0, packet);',
     '''    if (unixlib_status) return unixlib_status;\n    packet->trace_pe_begin_ns = bridge_qpc_ns();\n    NTSTATUS status = WINE_UNIX_CALL(0, packet);\n    packet->trace_pe_return_ns = bridge_qpc_ns();\n    return status;'''),
])

# Native Unixlib timestamps share mach_continuous_time with Wine QPC on this pinned build.
edit(BRIDGE / 'bridge_unix.c', [
    ('#include <dlfcn.h>\n', '#include <dlfcn.h>\n#include <mach/mach_time.h>\n#include <pthread.h>\n'),
    ('extern uint32_t fgmb_metal_handle_request(fgmb_packet *packet);\n',
     '''extern uint32_t fgmb_metal_handle_request(fgmb_packet *packet);\n\nstatic uint64_t bridge_continuous_ns(void)\n{\n    mach_timebase_info_data_t timebase;\n    mach_timebase_info(&timebase);\n    return (uint64_t)(((__uint128_t)mach_continuous_time() * timebase.numer) / timebase.denom);\n}\n'''),
    ('    if (packet->request == FGMB_REQUEST_QUERY)\n',
     '''    packet->trace_unix_entry_ns = bridge_continuous_ns();\n    pthread_threadid_np(NULL, &packet->trace_provider_thread_id);\n    if (packet->request == FGMB_REQUEST_QUERY)\n'''),
    ('        return 0;\n    }\n\n    packet->status = fgmb_metal_handle_request(packet);\n    return (NTSTATUS)packet->status;',
     '''        packet->trace_unix_return_ns = bridge_continuous_ns();\n        return 0;\n    }\n\n    packet->status = fgmb_metal_handle_request(packet);\n    packet->trace_unix_return_ns = bridge_continuous_ns();\n    return (NTSTATUS)packet->status;'''),
])

# DXVK bridge/slot/copy timing and structured long-call ownership snapshot.
interop = R5 / 'src/dxvk/dxvk_metal_interop.cpp'
edit(interop, [
    ('#include "../util/util_win32_compat.h"\n', '#include "../util/util_win32_compat.h"\n#include "../util/r5_trace.h"\n'),
    ('''  uint32_t DxvkMetalInterop::callBridge(void* packet) const {\n#ifdef _WIN32\n    std::lock_guard<std::mutex> lock(m_bridgeMutex);\n    if (!m_bridgeCall)\n      return FGMB_STATUS_UNSUCCESSFUL;\n    return m_bridgeCall(packet);\n#else''',
     '''  uint32_t DxvkMetalInterop::callBridge(void* packet) const {\n#ifdef _WIN32\n    auto* request = static_cast<fgmb_packet*>(packet);\n    const uint64_t callStartNs = r5TraceNowNs();\n    std::unique_lock<std::mutex> lock(m_bridgeMutex);\n    const uint64_t bridgeLockNs = r5TraceNowNs();\n    if (!m_bridgeCall)\n      return FGMB_STATUS_UNSUCCESSFUL;\n    const uint32_t result = m_bridgeCall(packet);\n    const uint64_t callEndNs = r5TraceNowNs();\n    if (request->request == FGMB_REQUEST_METAL_INVERT || callEndNs - callStartNs >= 20'000'000ull) {\n      Logger::info(str::format(\n        "R5Timeline event=bridge_call t_begin_ns=", callStartNs,\n        " bridge_lock_ns=", bridgeLockNs, " pe_begin_ns=", request->trace_pe_begin_ns,\n        " unix_entry_ns=", request->trace_unix_entry_ns,\n        " provider_entry_ns=", request->trace_provider_entry_ns,\n        " cb_created_ns=", request->trace_command_buffer_created_ns,\n        " encoder_created_ns=", request->trace_encoder_created_ns,\n        " encode_begin_ns=", request->trace_encode_begin_ns,\n        " encode_end_ns=", request->trace_encode_end_ns,\n        " metal_commit_begin_ns=", request->trace_commit_begin_ns,\n        " metal_commit_end_ns=", request->trace_commit_end_ns,\n        " provider_return_ns=", request->trace_provider_return_ns,\n        " unix_return_ns=", request->trace_unix_return_ns,\n        " pe_return_ns=", request->trace_pe_return_ns, " t_end_ns=", callEndNs,\n        " InteropJobId=", request->trace_interop_job_id,\n        " AppFrameId=", request->trace_app_frame_id,\n        " nativeJob=", request->job_id, " READY=", request->ready_value,\n        " DONE=", request->done_value, " nativeThread=", request->trace_provider_thread_id,\n        " providerQueueDepth=", request->trace_provider_queue_depth,\n        " bridgeStatus=0x", std::hex, result));\n    }\n    if (request->request == FGMB_REQUEST_METAL_INVERT\n     && callEndNs - callStartNs >= 20'000'000ull) {\n      std::string owners;\n      {\n        std::lock_guard<std::mutex> ownerLock(m_mutex);\n        for (const auto& slot : m_slots) {\n          if (!owners.empty()) owners += ";";\n          owners += str::format("slot:", slot->slotId, ":busy=", slot->busy,\n            ":job=", slot->interopJobId, ":frame=", slot->appFrameId,\n            ":terminal=", slot->terminal, ":reclaim=", slot->reclaimValue,\n            ":generation=", slot->generation);\n        }\n      }\n      Logger::warn(str::format("R5STALL InteropJobId=", request->trace_interop_job_id,\n        " AppFrameId=", request->trace_app_frame_id, " generation=",\n        (request->trace_app_frame_id ? request->trace_app_frame_id : 0),\n        " history=0x", std::hex, request->input_texture,\n        " output=0x", request->output_texture, std::dec,\n        " bridge_us=", (callEndNs - callStartNs) / 1000,\n        " bridge_lock_us=", (bridgeLockNs - callStartNs) / 1000,\n        " providerQueueDepth=", request->trace_provider_queue_depth,\n        " thresholds_ms=20,50,100,250,500 owners=", owners));\n    }\n    return result;\n#else'''),
    ('''      selected->nextValue += 3u;\n    }\n\n    try {\n      VkImageSubresourceLayers subresource = { };''',
     '''      selected->nextValue += 3u;\n      Logger::info(str::format("R5Timeline event=history_slot_claim t_ns=",\n        r5TraceNowNs(), " InteropJobId=", job->interopJobId,\n        " AppFrameId=", job->appFrameId, " slot=", selected->slotId,\n        " generation=", selected->generation, " history=0x", std::hex,\n        selected->historyTexture, " output=0x", selected->outputTexture));\n    }\n\n    try {\n      Logger::info(str::format("R5Timeline event=vulkan_copy_record_begin t_ns=",\n        r5TraceNowNs(), " InteropJobId=", job->interopJobId,\n        " AppFrameId=", job->appFrameId, " slot=", selected->slotId));\n      VkImageSubresourceLayers subresource = { };'''),
    ('''      commandList->signalSemaphore(selected->semaphore, job->readyValue);\n      job->readyQueuedAt = std::chrono::steady_clock::now();''',
     '''      commandList->signalSemaphore(selected->semaphore, job->readyValue);\n      Logger::info(str::format("R5Timeline event=vulkan_copy_record_end_ready_recorded t_ns=",\n        r5TraceNowNs(), " InteropJobId=", job->interopJobId,\n        " AppFrameId=", job->appFrameId, " slot=", selected->slotId,\n        " READY=", job->readyValue));\n      job->readyQueuedAt = std::chrono::steady_clock::now();'''),
    ('''    if (!job || !job->slot)\n      return false;\n\n    pollSlots();\n\n    fgmb_packet request = { };''',
     '''    if (!job || !job->slot)\n      return false;\n\n    Logger::info(str::format("R5Timeline event=commit_worker_enter t_ns=",\n      r5TraceNowNs(), " InteropJobId=", job->interopJobId,\n      " AppFrameId=", job->appFrameId, " slot=", job->slot->slotId));\n    const uint64_t pollStartNs = r5TraceNowNs();\n    pollSlots();\n    Logger::info(str::format("R5Timeline event=commit_poll_slots_done t_ns=",\n      r5TraceNowNs(), " poll_begin_ns=", pollStartNs,\n      " InteropJobId=", job->interopJobId, " AppFrameId=", job->appFrameId));\n\n    fgmb_packet request = { };'''),
    ('''      request.done_value = job->doneValue;\n    }\n\n    const uint32_t result = callBridge(&request);''',
     '''      request.done_value = job->doneValue;\n      request.trace_interop_job_id = job->interopJobId;\n      request.trace_app_frame_id = job->appFrameId;\n    }\n\n    const uint32_t result = callBridge(&request);'''),
    ('''      const auto retiredAt = std::chrono::steady_clock::now();\n      if (candidate.nativeJobId)\n        Logger::info(str::format("MetalInterop: job-retired InteropJobId="''',
     '''      auto retiredAt = std::chrono::steady_clock::now();\n      Logger::info(str::format("R5Timeline event=history_output_slot_release t_ns=",\n        r5TraceNowNs(), " InteropJobId=", slot->interopJobId,\n        " AppFrameId=", slot->appFrameId, " slot=", slot->slotId,\n        " generation=", slot->generation, " reclaim=", slot->reclaimValue,\n        " historyAndOutputReleasedTogether=true"));\n      if (candidate.nativeJobId)\n        Logger::info(str::format("MetalInterop: job-retired InteropJobId="'''),
])

# The R5 poll release block uses const auto retiredAt; correct if patch's exact match changed.

# D3D11 source Present and WSI acquire/context/flush split timing.
swap = R5 / 'src/d3d11/d3d11_swapchain.cpp'
edit(swap, [
    ('#include "../util/util_env.h"\n', '#include "../util/util_env.h"\n#include "../util/r5_trace.h"\n'),
    ('''    if (tracePresent)\n      presentStart = dxvk::high_resolution_clock::now();\n    const uint64_t appFrameId = m_frameId + 1u;''',
     '''    if (tracePresent) {\n      presentStart = dxvk::high_resolution_clock::now();\n      Logger::info(str::format("R5Timeline event=source_present_begin t_ns=",\n        r5TraceNowNs(), " AppFrameId=", appFrameId,\n        " thread=", r5TraceThreadToken()));\n    }\n    const uint64_t appFrameId = m_frameId + 1u;'''),
])
