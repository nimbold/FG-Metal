#ifndef FRAMEGEN_API_H
#define FRAMEGEN_API_H

/*
 * Renderer-independent, provisional C ABI for Framegen.
 *
 * ABI version 1 is an initial draft, not a promise that the ABI is frozen.
 * Every structure carries size and version fields. Callers should
 * zero-initialize structures and set both fields. Structures embedded by
 * value are frozen for ABI v1; changing one requires a new ABI major or a
 * top-level extension. No C++ or platform GPU types cross this boundary.
 * All timestamps in one clock_domain use the same monotonic clock and epoch.
 * A clock_domain changes when that clock is reset or re-based. The caller
 * supplies now_ns to ticket_poll in that same clock domain. Timestamps are
 * signed nanoseconds; zero and negative values are valid. The
 * FRAMEGEN_TIMESTAMP_UNKNOWN sentinel is allowed only for diagnostic
 * render_completion_timestamp_ns; required source, scheduling, interpolation,
 * polling, and presentation timestamps must not use it.
 */

#include <stdint.h>

#define FRAMEGEN_TIMESTAMP_UNKNOWN INT64_MIN

#if defined(FRAMEGEN_SHARED) && defined(_WIN32)
#  if defined(FRAMEGEN_BUILDING_LIBRARY)
#    define FRAMEGEN_API __declspec(dllexport)
#  else
#    define FRAMEGEN_API __declspec(dllimport)
#  endif
#elif defined(FRAMEGEN_SHARED) && (defined(__GNUC__) || defined(__clang__))
#  define FRAMEGEN_API __attribute__((visibility("default")))
#else
#  define FRAMEGEN_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define FRAMEGEN_ABI_VERSION 1u
#define FRAMEGEN_BACKEND_ID_CAPACITY 128u

typedef struct framegen_context framegen_context_t;
typedef struct framegen_ticket framegen_ticket_t;

typedef uint32_t framegen_status_t;
#define FRAMEGEN_STATUS_OK 0u
#define FRAMEGEN_STATUS_INVALID_ARGUMENT 1u
#define FRAMEGEN_STATUS_ABI_MISMATCH 2u
#define FRAMEGEN_STATUS_BACKEND_UNAVAILABLE 3u
#define FRAMEGEN_STATUS_CAPABILITY_UNAVAILABLE 4u
#define FRAMEGEN_STATUS_UNSUPPORTED_RESOURCE 5u
#define FRAMEGEN_STATUS_INVALID_STATE 6u
#define FRAMEGEN_STATUS_INVALID_FRAME 7u
#define FRAMEGEN_STATUS_STALE_FRAME 8u
#define FRAMEGEN_STATUS_BACKPRESSURE 9u
#define FRAMEGEN_STATUS_BACKEND_ERROR 10u
#define FRAMEGEN_STATUS_DEVICE_LOST 11u
#define FRAMEGEN_STATUS_INTERNAL_ERROR 12u

typedef uint32_t framegen_abi_stability_t;
#define FRAMEGEN_ABI_PROVISIONAL 1u

typedef struct framegen_abi_info {
    uint32_t struct_size;
    uint32_t struct_version;
    uint32_t abi_version;
    uint32_t stability;
    uint32_t reserved[4];
} framegen_abi_info_t;

/* Capability values are stable bit positions within this provisional ABI. */
typedef uint64_t framegen_capability_flags_t;
#define FRAMEGEN_CAP_COLOR_ONLY (UINT64_C(1) << 0)
#define FRAMEGEN_CAP_MOTION_VECTORS (UINT64_C(1) << 1)
#define FRAMEGEN_CAP_DEPTH (UINT64_C(1) << 2)
#define FRAMEGEN_CAP_UI_PLANE (UINT64_C(1) << 3)
#define FRAMEGEN_CAP_UI_MASK (UINT64_C(1) << 4)
#define FRAMEGEN_CAP_REACTIVE_MASK (UINT64_C(1) << 5)
#define FRAMEGEN_CAP_CAMERA_MATRICES (UINT64_C(1) << 6)
#define FRAMEGEN_CAP_JITTER (UINT64_C(1) << 7)
#define FRAMEGEN_CAP_EXPOSURE (UINT64_C(1) << 8)
#define FRAMEGEN_CAP_HDR (UINT64_C(1) << 9)
#define FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME (UINT64_C(1) << 10)
#define FRAMEGEN_CAP_AUTOMATIC_HUD_PROTECTION (UINT64_C(1) << 11)
#define FRAMEGEN_CAP_HUD_DEBUG_VISUALIZATION (UINT64_C(1) << 12)

#define FRAMEGEN_INPUT_MOTION_VECTORS FRAMEGEN_CAP_MOTION_VECTORS
#define FRAMEGEN_INPUT_DEPTH FRAMEGEN_CAP_DEPTH
#define FRAMEGEN_INPUT_UI_PLANE FRAMEGEN_CAP_UI_PLANE
#define FRAMEGEN_INPUT_UI_MASK FRAMEGEN_CAP_UI_MASK
#define FRAMEGEN_INPUT_REACTIVE_MASK FRAMEGEN_CAP_REACTIVE_MASK
#define FRAMEGEN_INPUT_CAMERA_MATRICES FRAMEGEN_CAP_CAMERA_MATRICES
#define FRAMEGEN_INPUT_JITTER FRAMEGEN_CAP_JITTER
#define FRAMEGEN_INPUT_EXPOSURE FRAMEGEN_CAP_EXPOSURE

typedef uint32_t framegen_gpu_backend_type_t;
#define FRAMEGEN_GPU_BACKEND_METAL 1u
/* Reserved until API-specific import/layout/queue-ownership descriptors exist. */
#define FRAMEGEN_GPU_BACKEND_D3D12 2u
#define FRAMEGEN_GPU_BACKEND_VULKAN 3u
#define FRAMEGEN_GPU_BACKEND_TEST 0x7fff0001u

typedef uint32_t framegen_resource_type_t;
#define FRAMEGEN_RESOURCE_DEVICE 1u
#define FRAMEGEN_RESOURCE_TEXTURE_2D 2u

typedef uint32_t framegen_sync_type_t;
#define FRAMEGEN_SYNC_NONE_READY 0u
#define FRAMEGEN_SYNC_METAL_SHARED_EVENT 1u
/* Reserved until D3D12/Vulkan resource and queue-ownership interop is defined. */
#define FRAMEGEN_SYNC_D3D12_FENCE 2u
#define FRAMEGEN_SYNC_VULKAN_TIMELINE_SEMAPHORE 3u
#define FRAMEGEN_SYNC_TEST 0x7fff0001u

typedef void (*framegen_handle_retain_fn)(void *user_data, void *native_handle);
typedef void (*framegen_handle_release_fn)(void *user_data, void *native_handle);

/*
 * Native handles are borrowed for the duration of the call. If supplied,
 * retain/release must be supplied as a pair. Framegen invokes retain when it
 * accepts the resource and invokes release after queued work and any reported
 * consumer use are complete; either callback may run on a worker thread.
 * Callbacks must be thread-safe, nonblocking, and must not re-enter this API.
 * They must not throw through the C boundary or use longjmp.
 * If callbacks are omitted, the selected backend must take ownership during
 * import and the caller must keep the native resource alive until that import
 * has done so. Image/device resources require a non-null native handle; a sync
 * point may omit one only when its type is FRAMEGEN_SYNC_NONE_READY.
 */
typedef struct framegen_gpu_handle {
    uint32_t struct_size;
    uint32_t struct_version;
    uint32_t backend_type;
    uint32_t resource_type;
    uint64_t device_id;
    /* Stable identity token for underlying storage; zero asks the backend to infer it. */
    uint64_t resource_identity;
    void *native_handle;
    void *user_data;
    framegen_handle_retain_fn retain;
    framegen_handle_release_fn release;
    uint32_t reserved[4];
} framegen_gpu_handle_t;

typedef struct framegen_sync_point {
    uint32_t struct_size;
    uint32_t struct_version;
    uint32_t backend_type;
    uint32_t sync_type;
    uint64_t device_id;
    uint64_t value;
    void *native_handle;
    void *user_data;
    framegen_handle_retain_fn retain;
    framegen_handle_release_fn release;
    uint32_t reserved[4];
} framegen_sync_point_t;

typedef uint32_t framegen_pixel_format_t;
#define FRAMEGEN_PIXEL_FORMAT_UNKNOWN 0u
#define FRAMEGEN_PIXEL_FORMAT_R8_UNORM 1u
#define FRAMEGEN_PIXEL_FORMAT_RG8_UNORM 2u
#define FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM 3u
#define FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM_SRGB 4u
#define FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM 5u
#define FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM_SRGB 6u
#define FRAMEGEN_PIXEL_FORMAT_RGB10A2_UNORM 7u
#define FRAMEGEN_PIXEL_FORMAT_BGR10A2_UNORM 8u
#define FRAMEGEN_PIXEL_FORMAT_RG11B10_FLOAT 9u
#define FRAMEGEN_PIXEL_FORMAT_RGBA16_FLOAT 10u
#define FRAMEGEN_PIXEL_FORMAT_RGBA32_FLOAT 11u
#define FRAMEGEN_PIXEL_FORMAT_R16_FLOAT 12u
#define FRAMEGEN_PIXEL_FORMAT_RG16_FLOAT 13u
#define FRAMEGEN_PIXEL_FORMAT_RG32_FLOAT 14u
#define FRAMEGEN_PIXEL_FORMAT_R32_FLOAT 15u
#define FRAMEGEN_PIXEL_FORMAT_D16_UNORM 16u
#define FRAMEGEN_PIXEL_FORMAT_D32_FLOAT 17u
#define FRAMEGEN_PIXEL_FORMAT_D24_UNORM_S8_UINT 18u
#define FRAMEGEN_PIXEL_FORMAT_BIT(format) (UINT64_C(1) << (format))

typedef uint32_t framegen_color_space_t;
#define FRAMEGEN_COLOR_SPACE_UNKNOWN 0u
#define FRAMEGEN_COLOR_SPACE_SRGB 1u
#define FRAMEGEN_COLOR_SPACE_DISPLAY_P3 2u
#define FRAMEGEN_COLOR_SPACE_REC2020 3u

typedef uint32_t framegen_transfer_function_t;
#define FRAMEGEN_TRANSFER_UNKNOWN 0u
#define FRAMEGEN_TRANSFER_LINEAR 1u
#define FRAMEGEN_TRANSFER_SRGB 2u
#define FRAMEGEN_TRANSFER_PQ 3u
#define FRAMEGEN_TRANSFER_HLG 4u

typedef uint32_t framegen_dynamic_range_t;
#define FRAMEGEN_DYNAMIC_RANGE_UNKNOWN 0u
#define FRAMEGEN_DYNAMIC_RANGE_SDR 1u
#define FRAMEGEN_DYNAMIC_RANGE_HDR 2u

typedef uint32_t framegen_alpha_mode_t;
#define FRAMEGEN_ALPHA_UNKNOWN 0u
#define FRAMEGEN_ALPHA_OPAQUE 1u
#define FRAMEGEN_ALPHA_STRAIGHT 2u
#define FRAMEGEN_ALPHA_PREMULTIPLIED 3u

typedef struct framegen_image {
    uint32_t struct_size;
    uint32_t struct_version;
    framegen_gpu_handle_t resource;
    uint32_t width;
    uint32_t height;
    uint32_t pixel_format;
    uint32_t color_space;
    uint32_t transfer_function;
    uint32_t dynamic_range;
    uint32_t alpha_mode;
    uint32_t reserved[4];
} framegen_image_t;

typedef struct framegen_device_ref {
    uint32_t struct_size;
    uint32_t struct_version;
    uint32_t backend_type;
    uint32_t resource_type;
    /* Optional host-side device token; use context_info.device_id in resources. */
    uint64_t device_id;
    void *native_handle;
    void *user_data;
    framegen_handle_retain_fn retain;
    framegen_handle_release_fn release;
    uint32_t reserved[4];
} framegen_device_ref_t;

typedef struct framegen_backend_info {
    uint32_t struct_size;
    uint32_t struct_version;
    char backend_id[FRAMEGEN_BACKEND_ID_CAPACITY];
    uint32_t backend_type;
    uint32_t available;
    framegen_capability_flags_t supported_capabilities;
    framegen_capability_flags_t required_capabilities;
    framegen_capability_flags_t required_input_capabilities;
    uint64_t supported_pixel_formats;
    uint32_t max_width;
    uint32_t max_height;
    uint32_t max_in_flight_requests;
    uint32_t reserved[8];
} framegen_backend_info_t;

typedef struct framegen_context_desc {
    uint32_t struct_size;
    uint32_t struct_version;
    const char *backend_id;
    framegen_device_ref_t device;
    uint32_t reserved[8];
} framegen_context_desc_t;

typedef struct framegen_context_info {
    uint32_t struct_size;
    uint32_t struct_version;
    /* Canonical ID to use in all source image and sync handles for this context. */
    uint64_t device_id;
    uint64_t history_generation;
    uint32_t configured;
    /* Set by get_info even when the context has lost its device. */
    uint32_t device_lost;
    uint32_t reserved[8];
} framegen_context_info_t;

typedef struct framegen_context_config {
    uint32_t struct_size;
    uint32_t struct_version;
    framegen_capability_flags_t preferred_capabilities;
    framegen_capability_flags_t required_capabilities;
    framegen_capability_flags_t available_input_capabilities;
    /* Also bounds tickets retained until notification, GPU completion, and release. */
    uint32_t max_in_flight_requests;
    uint32_t history_capacity;
    uint32_t reserved[8];
} framegen_context_config_t;

/*
 * HUD options are a separate top-level extension so ABI v1's existing
 * by-value structures remain unchanged. Apply after context_configure and
 * before submitting new source frames. For explicit UI-plane mode, include
 * FRAMEGEN_CAP_UI_PLANE in preferred_capabilities and
 * available_input_capabilities when configuring the context. Automatic mode
 * and nontrivial debug views also require their respective capability bits in
 * preferred_capabilities (or required_capabilities). Changing options
 * invalidates history; set them before submitting frames for the new mode.
 * ui_temporal_source selects previous, current, or the endpoint nearest
 * desired_presentation_timestamp_ns; exact ties choose current. A debug view
 * replaces the normal generated image.
 */
typedef uint32_t framegen_hud_mode_t;
#define FRAMEGEN_HUD_MODE_NO_KNOWLEDGE 0u
#define FRAMEGEN_HUD_MODE_EXPLICIT_UI_PLANE 1u
#define FRAMEGEN_HUD_MODE_AUTOMATIC_PROTECTION 2u

typedef uint32_t framegen_ui_temporal_source_t;
#define FRAMEGEN_UI_SOURCE_PREVIOUS 0u
#define FRAMEGEN_UI_SOURCE_CURRENT 1u
#define FRAMEGEN_UI_SOURCE_NEAREST_PRESENTATION 2u

typedef uint32_t framegen_hud_debug_visualization_t;
#define FRAMEGEN_HUD_DEBUG_DISABLED 0u
#define FRAMEGEN_HUD_DEBUG_RAW_MASK 1u
#define FRAMEGEN_HUD_DEBUG_STABILIZED_MASK 2u
#define FRAMEGEN_HUD_DEBUG_PROTECTED_REGIONS 3u
#define FRAMEGEN_HUD_DEBUG_INTERPOLATION_CONFIDENCE 4u
#define FRAMEGEN_HUD_DEBUG_FINAL_COMPOSITE 5u

typedef struct framegen_hud_options {
    uint32_t struct_size;
    uint32_t struct_version;
    framegen_hud_mode_t mode;
    framegen_ui_temporal_source_t ui_temporal_source;
    framegen_hud_debug_visualization_t debug_visualization;
    uint32_t reserved[8];
} framegen_hud_options_t;

typedef struct framegen_capability_result {
    uint32_t struct_size;
    uint32_t struct_version;
    framegen_capability_flags_t supported_capabilities;
    framegen_capability_flags_t backend_required_capabilities;
    framegen_capability_flags_t backend_required_input_capabilities;
    framegen_capability_flags_t negotiated_capabilities;
    framegen_capability_flags_t unmet_capabilities;
    uint32_t max_in_flight_requests;
    uint32_t reserved[8];
} framegen_capability_result_t;

typedef struct framegen_optional_inputs {
    uint32_t struct_size;
    uint32_t struct_version;
    framegen_capability_flags_t present_mask;
    framegen_image_t motion_vectors;
    framegen_image_t depth;
    framegen_image_t ui_texture;
    framegen_image_t ui_mask;
    framegen_image_t reactive_mask;
    float camera_to_world[16];
    float projection[16];
    float jitter_xy[2];
    float exposure;
    uint32_t motion_vector_encoding;
    uint32_t motion_vector_units;
    uint32_t motion_vector_direction;
    uint32_t depth_encoding;
    uint32_t reserved[8];
} framegen_optional_inputs_t;

/*
 * In FRAMEGEN_HUD_MODE_EXPLICIT_UI_PLANE, source color must contain opaque
 * scene color without UI and ui_texture must contain the corresponding UI
 * layer. The placeholder Metal backend interpolates scene color in linear
 * light, then composites the selected endpoint UI using its declared alpha
 * mode. UI dimensions must match color; RGBA8 SDR is currently supported.
 * Scene and UI may each use sRGB or linear-sRGB transfer, but their effective
 * transfer must match across the two source endpoints. UI alpha is the soft
 * coverage mask. Premultiplied RGB is premultiplied in linear light and then
 * encoded by the declared transfer function. In automatic-protection mode,
 * color is already composited; the backend estimates and stabilizes a soft
 * confidence mask from image and temporal cues.
 * The generated image returned by polling is the final image, or the selected
 * optional debug visualization when enabled.
 */

#define FRAMEGEN_MOTION_VECTOR_ENCODING_SIGNED_XY 1u
#define FRAMEGEN_MOTION_VECTOR_ENCODING_UNORM_XY 2u
#define FRAMEGEN_MOTION_VECTOR_UNITS_PIXELS 1u
#define FRAMEGEN_MOTION_VECTOR_UNITS_NORMALIZED_VIEWPORT 2u
#define FRAMEGEN_MOTION_VECTOR_DIRECTION_PREVIOUS_TO_CURRENT 1u
#define FRAMEGEN_MOTION_VECTOR_DIRECTION_CURRENT_TO_PREVIOUS 2u
#define FRAMEGEN_DEPTH_ENCODING_DEVICE_Z 1u
#define FRAMEGEN_DEPTH_ENCODING_REVERSE_DEVICE_Z 2u
#define FRAMEGEN_DEPTH_ENCODING_LINEAR_VIEW_DISTANCE 3u

/*
 * Convention for camera_to_world and projection: column-major matrices,
 * column vectors, right-handed camera space looking down -Z, clip-space depth
 * in [0, 1]. Jitter is in pixel units; exposure is log2 exposure in EV stops.
 * Motion-vector direction and units are explicit in the fields above; positive
 * vectors point from the named origin frame's pixel to its corresponding pixel
 * in the destination frame. Normalized viewport units scale X by image width
 * and Y by image height. Device-Z depth uses the projection's [0, 1] clip depth;
 * linear view distance uses the same scene units as the camera transform.
 * RG16/RG32 float store signed XY vectors; RG8 UNORM XY maps each channel from
 * [0, 1] to [-1, 1]. UI/reactive masks use one-channel values in [0, 1].
 */

typedef struct framegen_source_frame {
    uint32_t struct_size;
    uint32_t struct_version;
    uint64_t stream_id;
    /* Strictly increases for the lifetime of this context, including resets. */
    uint64_t source_frame_id;
    framegen_image_t color;
    uint64_t clock_domain;
    int64_t source_timestamp_ns;
    int64_t source_duration_ns;
    /* Diagnostic only; use FRAMEGEN_TIMESTAMP_UNKNOWN when unavailable. */
    int64_t render_completion_timestamp_ns;
    int64_t desired_presentation_timestamp_ns;
    framegen_sync_point_t render_completion;
    framegen_optional_inputs_t optional_inputs;
    uint32_t reserved[8];
} framegen_source_frame_t;

/*
 * Source images and every present optional image are immutable from accepted
 * submission until all backend GPU reads finish. render_completion orders
 * writes to every image attached to this frame, not just its color image.
 * FRAMEGEN_SYNC_NONE_READY asserts that all attached images are already ready.
 */

typedef struct framegen_frame_receipt {
    uint32_t struct_size;
    uint32_t struct_version;
    uint64_t source_frame_id;
    uint64_t history_generation;
    framegen_capability_flags_t available_input_capabilities;
    uint32_t reserved[8];
} framegen_frame_receipt_t;

typedef struct framegen_interpolation_request {
    uint32_t struct_size;
    uint32_t struct_version;
    uint64_t previous_source_frame_id;
    uint64_t current_source_frame_id;
    uint64_t history_generation;
    uint64_t clock_domain;
    /* Bracketed by the two source timestamps; determines interpolation. */
    int64_t interpolation_timestamp_ns;
    /* Renderer scheduling target, independent of the interpolation sample. */
    int64_t desired_presentation_timestamp_ns;
    int64_t presentation_deadline_ns;
    uint32_t reserved[8];
} framegen_interpolation_request_t;

typedef uint32_t framegen_ticket_status_t;
#define FRAMEGEN_TICKET_PENDING 0u
#define FRAMEGEN_TICKET_READY 1u
#define FRAMEGEN_TICKET_DEADLINE_MISSED 2u
#define FRAMEGEN_TICKET_FAILED 3u
#define FRAMEGEN_TICKET_INVALIDATED 4u
#define FRAMEGEN_TICKET_BYPASSED 5u

typedef struct framegen_generated_frame {
    uint32_t struct_size;
    uint32_t struct_version;
    uint64_t ticket_id;
    uint64_t previous_source_frame_id;
    uint64_t current_source_frame_id;
    int64_t interpolation_timestamp_ns;
    int64_t desired_presentation_timestamp_ns;
    int64_t presentation_deadline_ns;
    framegen_image_t image;
    framegen_sync_point_t completion;
    uint32_t reserved[8];
} framegen_generated_frame_t;

typedef struct framegen_ticket_result {
    uint32_t struct_size;
    uint32_t struct_version;
    uint32_t status;
    uint32_t error_status;
    uint64_t ticket_id;
    framegen_generated_frame_t generated_frame;
    uint32_t reserved[8];
} framegen_ticket_result_t;

/*
 * ticket_id is valid for every ticket status. For PENDING and READY, the
 * generated image and completion point are also available. The host may
 * inspect them while pending, but must not enqueue a consumer wait or commit
 * presentation until it chooses the generated frame before its deadline.
 * Polling never waits on the CPU. The returned native output handle remains
 * owned by Framegen while the ticket is retained and until consumer completion
 * has been reported. Exported output handles are borrowed and their callback
 * fields are null; the host must report all consumer use before ticket
 * retirement and must not use handles afterward.
 */

typedef uint32_t framegen_presentation_disposition_t;
#define FRAMEGEN_PRESENTED_REAL_FRAME 1u
#define FRAMEGEN_PRESENTED_GENERATED_FRAME 2u
#define FRAMEGEN_BYPASSED_GENERATED_FRAME 3u
#define FRAMEGEN_DROPPED_FRAME 4u

typedef struct framegen_presentation_event {
    uint32_t struct_size;
    uint32_t struct_version;
    uint32_t disposition;
    uint32_t reserved0;
    uint64_t source_frame_id;
    uint64_t ticket_id;
    uint64_t clock_domain;
    int64_t presentation_timestamp_ns;
    framegen_sync_point_t consumer_completion;
    uint32_t reserved[8];
} framegen_presentation_event_t;

typedef uint32_t framegen_invalidation_reason_t;
#define FRAMEGEN_INVALIDATE_RESIZE 1u
#define FRAMEGEN_INVALIDATE_FORMAT_CHANGE 2u
#define FRAMEGEN_INVALIDATE_COLORSPACE_CHANGE 3u
#define FRAMEGEN_INVALIDATE_DEVICE_LOSS 4u
#define FRAMEGEN_INVALIDATE_HISTORY_RESET 5u

typedef struct framegen_statistics {
    uint32_t struct_size;
    uint32_t struct_version;
    uint64_t source_frames_submitted;
    uint64_t interpolation_requests;
    uint64_t generated_frames_ready;
    /* Counts BYPASSED_GENERATED_FRAME dispositions; drops are not bypasses. */
    uint64_t bypassed_frames;
    uint64_t deadline_misses;
    uint64_t failed_requests;
    uint64_t real_frames_presented;
    uint64_t generated_frames_presented;
    /* Producer GPU requests not yet terminal; retained tickets may also apply backpressure. */
    uint64_t active_in_flight_requests;
    uint64_t total_cpu_enqueue_time_ns;
    uint64_t gpu_execution_time_ns;
    uint64_t gpu_execution_time_samples;
    uint32_t reserved[8];
} framegen_statistics_t;

FRAMEGEN_API framegen_status_t framegen_get_abi_info(framegen_abi_info_t *out_info);
/* output_stride is the byte distance between entries and must be at least the
 * current framegen_backend_info_t size when capacity is non-zero. */
FRAMEGEN_API framegen_status_t framegen_enumerate_backends(
    uint32_t capacity, uint32_t output_stride, void *backends,
    uint32_t *out_count);
FRAMEGEN_API framegen_status_t framegen_query_backend(
    const char *backend_id, framegen_backend_info_t *out_info);
FRAMEGEN_API framegen_status_t framegen_context_create(
    const framegen_context_desc_t *desc, framegen_context_t **out_context);
FRAMEGEN_API void framegen_context_destroy(framegen_context_t *context);
FRAMEGEN_API framegen_status_t framegen_context_get_info(
    framegen_context_t *context, framegen_context_info_t *out_info);
FRAMEGEN_API framegen_status_t framegen_context_query_capabilities(
    framegen_context_t *context, framegen_capability_result_t *out_result);
FRAMEGEN_API framegen_status_t framegen_context_configure(
    framegen_context_t *context, const framegen_context_config_t *config,
    framegen_capability_result_t *out_result);
FRAMEGEN_API framegen_status_t framegen_context_set_hud_options(
    framegen_context_t *context, const framegen_hud_options_t *options);
FRAMEGEN_API framegen_status_t framegen_submit_source_frame(
    framegen_context_t *context, const framegen_source_frame_t *frame,
    framegen_frame_receipt_t *out_receipt);
FRAMEGEN_API framegen_status_t framegen_request_interpolation(
    framegen_context_t *context, const framegen_interpolation_request_t *request,
    framegen_ticket_t **out_ticket);
FRAMEGEN_API framegen_status_t framegen_ticket_poll(
    framegen_ticket_t *ticket, int64_t now_ns, framegen_ticket_result_t *out_result);
FRAMEGEN_API void framegen_ticket_release(framegen_ticket_t *ticket);
/*
 * Report one accepted terminal disposition for each returned ticket. A
 * generated output may be reported while its producer completion is pending,
 * but only while its ticket is PENDING or READY and the presentation timestamp
 * is before the deadline. The consumer completion must order after the
 * producer wait and all sampling. A failed, invalidated, or deadline-missed
 * ticket cannot be presented as generated.
 * A ticket-associated drop may use FRAMEGEN_DROPPED_FRAME with its ticket ID,
 * including after history invalidation. Pass a non-NONE consumer_completion
 * whenever consumer GPU work may still reference the output; NONE_READY asserts
 * that use has already finished. It is valid to release the client ticket
 * before notifying by ticket ID; Framegen retains internal resources until the
 * notification and both GPU completion points are finished. A rejected
 * generated-presentation notification does not commit its disposition or
 * consumer completion; report a bypass or drop with the same ticket ID to
 * retire it. Deadline misses are counted once, when polling or a late
 * presentation timestamp first establishes that the deadline was missed.
 * Duplicate accepted notifications are rejected.
 */
FRAMEGEN_API framegen_status_t framegen_notify_presentation(
    framegen_context_t *context, const framegen_presentation_event_t *event);
FRAMEGEN_API framegen_status_t framegen_context_reset_history(framegen_context_t *context);
FRAMEGEN_API framegen_status_t framegen_context_invalidate(
    framegen_context_t *context, framegen_invalidation_reason_t reason,
    uint64_t *out_history_generation);
FRAMEGEN_API framegen_status_t framegen_context_collect_statistics(
    framegen_context_t *context, framegen_statistics_t *out_statistics);
FRAMEGEN_API framegen_status_t framegen_get_last_error(
    uint32_t capacity, char *buffer, uint32_t *out_required_size);

/* Context destruction does not wait for GPU completion. It invalidates
 * outstanding tickets and defers resource retirement until known producer
 * and consumer completion points finish. Notify queued consumers first.
 * Callers must serialize context destruction against all calls using that
 * context pointer, and ticket release against calls using that ticket pointer. */

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* FRAMEGEN_API_H */
