#ifndef FRAMEGEN_METAL_API_H
#define FRAMEGEN_METAL_API_H

/* C entry point for registering the Metal backend. The argument
 * and resource handles in api.h remain opaque and Objective-C-free. */
#include "framegen/api.h"

#ifdef __cplusplus
extern "C" {
#endif

FRAMEGEN_API framegen_status_t framegen_metal_register_backend(void);

#ifdef __cplusplus
}
#endif

#endif /* FRAMEGEN_METAL_API_H */
