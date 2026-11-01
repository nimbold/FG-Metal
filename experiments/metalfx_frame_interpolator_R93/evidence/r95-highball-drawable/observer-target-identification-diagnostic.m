#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#import <objc/runtime.h>
#import <libproc.h>
#import <dlfcn.h>
#import <mach-o/dyld.h>
#import <fcntl.h>
#import <pthread.h>
#import <stdio.h>
#import <stdlib.h>
#import <string.h>
#import <unistd.h>
#include <math.h>

#define MAX_HOOKS 256
typedef struct { Class cls; SEL sel; IMP original; } Hook;
static Hook hooks[MAX_HOOKS];
static size_t hookCount = 0;
static pthread_mutex_t hookLock = PTHREAD_MUTEX_INITIALIZER;
static IMP originalNextDrawable = NULL;
static int logFd = -1;
static pthread_once_t logOnce = PTHREAD_ONCE_INIT;
static void *selectedExperimentLayer = NULL;
static BOOL framebufferOnlyExperimentAttempted = NO;
static BOOL framebufferOnlyExperimentActive = NO;
static NSUInteger experimentDrawableCount = 0;
static NSUInteger experimentPresentCount = 0;

static void initLog(void) {
    char path[256];
    snprintf(path, sizeof(path), "/tmp/fgmetal-r95-trackc/observer.%d.log", getpid());
    logFd = open(path, O_CREAT | O_APPEND | O_WRONLY | O_CLOEXEC, 0600);
}
static void logLine(const char *fmt, ...) {
    pthread_once(&logOnce, initLog);
    if (logFd < 0) return;
    char body[1800];
    va_list ap; va_start(ap, fmt);
    int n = vsnprintf(body, sizeof(body), fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if ((size_t)n >= sizeof(body)) n = (int)sizeof(body) - 1;
    char line[2048];
    int m = snprintf(line, sizeof(line), "%s\n", body);
    if (m > 0) (void)write(logFd, line, (size_t)m);
}
static const char *className(id obj) { return obj ? class_getName(object_getClass(obj)) : "nil"; }
static const char *procPath(void) {
    static char path[PROC_PIDPATHINFO_MAXSIZE];
    if (!path[0] && proc_pidpath(getpid(), path, sizeof(path)) <= 0) snprintf(path, sizeof(path), "unknown");
    return path;
}
static void logTexture(const char *tag, id<CAMetalDrawable> d) {
    id<MTLTexture> t = d.texture;
    id<MTLDevice> td = t.device;
    id<MTLDevice> ld = d.layer.device;
    logLine("%s pid=%d path=%s drawable=%p drawableClass=%s drawableID=%lu layer=%p layerClass=%s layerDevice=%p texture=%p textureDevice=%p layerTextureDeviceMatch=%d deviceName=%s deviceRegistryID=%llu width=%lu height=%lu pixelFormat=%lu usage=%lu storage=%lu textureFramebufferOnly=%d layerFramebufferOnly=%d", tag, getpid(), procPath(), (__bridge void *)d, className(d), (unsigned long)d.drawableID, (__bridge void *)d.layer, className(d.layer), (__bridge void *)ld, (__bridge void *)t, (__bridge void *)td, ld == td, td.name.UTF8String ?: "unknown", (unsigned long long)td.registryID, (unsigned long)t.width, (unsigned long)t.height, (unsigned long)t.pixelFormat, (unsigned long)t.usage, (unsigned long)t.storageMode, t.framebufferOnly, d.layer.framebufferOnly);
}
static BOOL isCandidateGameLayer(id self) {
    const char *path = procPath();
    if (!path || !strstr(path,
            "/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r29/engine/bin/wine"))
        return NO;
    CAMetalLayer *layer = (CAMetalLayer *)self;
    id<MTLDevice> device = layer.device;
    return device.registryID == 4294968213ULL && layer.pixelFormat == (MTLPixelFormat)80 &&
        layer.framebufferOnly;
}
static void recordSelectedPresent(id<CAMetalDrawable> drawable, const char *selector) {
    if (!drawable) return;
    pthread_mutex_lock(&hookLock);
    if (framebufferOnlyExperimentActive &&
        (__bridge void *)drawable.layer == selectedExperimentLayer) {
        ++experimentPresentCount;
        logLine("FRAMEBUFFER_ONLY_TEST_PRESENT pid=%d layer=%p drawableID=%lu selector=%s count=%lu framebufferOnly=%d",
                getpid(), selectedExperimentLayer, (unsigned long)drawable.drawableID, selector,
                (unsigned long)experimentPresentCount, drawable.layer.framebufferOnly);
    }
    pthread_mutex_unlock(&hookLock);
}
static id hookNextDrawable(id self, SEL _cmd) {
    CAMetalLayer *layer = (CAMetalLayer *)self;
    BOOL selected = NO;
    pthread_mutex_lock(&hookLock);
    if (!framebufferOnlyExperimentAttempted &&
        selectedExperimentLayer == (__bridge void *)self) {
        framebufferOnlyExperimentAttempted = YES;
        BOOL before = layer.framebufferOnly;
        @try {
            layer.framebufferOnly = NO;
            BOOL after = layer.framebufferOnly;
            framebufferOnlyExperimentActive = !after;
            logLine("FRAMEBUFFER_ONLY_EXPERIMENT pid=%d path=%s layer=%p deviceRegistryID=%llu pixelFormat=%lu width=%ld height=%ld before=%d requested=0 after=%d result=%s",
                    getpid(), procPath(), selectedExperimentLayer,
                    (unsigned long long)layer.device.registryID, (unsigned long)layer.pixelFormat,
                    lround(layer.drawableSize.width), lround(layer.drawableSize.height), before,
                    after, framebufferOnlyExperimentActive ? "PASS" : "FAIL");
            if (!framebufferOnlyExperimentActive && before)
                layer.framebufferOnly = YES;
        } @catch (NSException *exception) {
            @try { layer.framebufferOnly = before; } @catch (__unused NSException *restoreException) {}
            logLine("FRAMEBUFFER_ONLY_EXPERIMENT pid=%d layer=%p result=EXCEPTION name=%s reason=%s restored=%d",
                    getpid(), selectedExperimentLayer, exception.name.UTF8String ?: "unknown",
                    exception.reason.UTF8String ?: "unknown", layer.framebufferOnly == before);
        }
    }
    selected = framebufferOnlyExperimentActive && selectedExperimentLayer == (__bridge void *)self;
    pthread_mutex_unlock(&hookLock);
    id<CAMetalDrawable> result = ((id (*)(id, SEL))originalNextDrawable)(self, _cmd);
    if (result) {
        if (selected) {
            pthread_mutex_lock(&hookLock);
            ++experimentDrawableCount;
            logTexture("FRAMEBUFFER_ONLY_TEST_DRAWABLE", result);
            BOOL restore = experimentDrawableCount >= 30;
            if (restore) {
                @try {
                    layer.framebufferOnly = YES;
                    framebufferOnlyExperimentActive = NO;
                    logLine("FRAMEBUFFER_ONLY_RESTORED pid=%d layer=%p restored=%d testDrawables=%lu matchingPresents=%lu",
                            getpid(), selectedExperimentLayer, layer.framebufferOnly,
                            (unsigned long)experimentDrawableCount, (unsigned long)experimentPresentCount);
                } @catch (NSException *exception) {
                    logLine("FRAMEBUFFER_ONLY_RESTORE_FAILED pid=%d layer=%p name=%s reason=%s",
                            getpid(), selectedExperimentLayer, exception.name.UTF8String ?: "unknown",
                            exception.reason.UTF8String ?: "unknown");
                }
            }
            pthread_mutex_unlock(&hookLock);
        } else {
            logTexture("NEXT_DRAWABLE", result);
            id<MTLTexture> texture = result.texture;
            id<MTLDevice> device = layer.device;
            if (!framebufferOnlyExperimentAttempted && isCandidateGameLayer(self) &&
                result.layer == layer && texture.width == 1470 && texture.height == 956 &&
                texture.pixelFormat == (MTLPixelFormat)80 &&
                texture.device.registryID == 4294968213ULL &&
                texture.device.registryID == device.registryID) {
                pthread_mutex_lock(&hookLock);
                if (!framebufferOnlyExperimentAttempted && selectedExperimentLayer == NULL) {
                    selectedExperimentLayer = (__bridge void *)self;
                    logLine("FRAMEBUFFER_ONLY_TARGET_IDENTIFIED pid=%d path=%s layer=%p drawable=%p drawableID=%lu deviceRegistryID=%llu drawableSize=%.3fx%.3f texture=%lux%lu pixelFormat=%lu usage=%lu layerFramebufferOnly=%d textureFramebufferOnly=%d",
                            getpid(), procPath(), selectedExperimentLayer, (__bridge void *)result,
                            (unsigned long)result.drawableID, (unsigned long long)device.registryID,
                            layer.drawableSize.width, layer.drawableSize.height,
                            (unsigned long)texture.width, (unsigned long)texture.height,
                            (unsigned long)texture.pixelFormat, (unsigned long)texture.usage,
                            layer.framebufferOnly, texture.framebufferOnly);
                }
                pthread_mutex_unlock(&hookLock);
            }
        }
    }
    else {
        logLine("NEXT_DRAWABLE_NIL pid=%d path=%s layer=%p", getpid(), procPath(), (__bridge void *)self);
        if (selected) {
            pthread_mutex_lock(&hookLock);
            @try {
                layer.framebufferOnly = YES;
                framebufferOnlyExperimentActive = NO;
                logLine("FRAMEBUFFER_ONLY_TEST_NIL_RESTORED pid=%d layer=%p restored=%d testDrawables=%lu matchingPresents=%lu",
                        getpid(), selectedExperimentLayer, layer.framebufferOnly,
                        (unsigned long)experimentDrawableCount, (unsigned long)experimentPresentCount);
            } @catch (NSException *exception) {
                logLine("FRAMEBUFFER_ONLY_RESTORE_FAILED pid=%d layer=%p name=%s reason=%s",
                        getpid(), selectedExperimentLayer, exception.name.UTF8String ?: "unknown",
                        exception.reason.UTF8String ?: "unknown");
            }
            pthread_mutex_unlock(&hookLock);
        }
    }
    return result;
}
static IMP originalFor(id self, SEL sel) {
    Class c = object_getClass(self);
    pthread_mutex_lock(&hookLock);
    IMP found = NULL;
    for (Class walk = c; walk && !found; walk = class_getSuperclass(walk))
        for (size_t i = 0; i < hookCount; ++i)
            if (hooks[i].cls == walk && hooks[i].sel == sel) { found = hooks[i].original; break; }
    pthread_mutex_unlock(&hookLock);
    return found;
}
static void logCompletion(id<MTLCommandBuffer> cb, id<CAMetalDrawable> d, SEL sel) {
    if (!d) return;
    logTexture("PRESENT_BOUNDARY", d);
    logLine("PRESENT_COMMAND pid=%d path=%s cmd=%p cmdClass=%s sel=%s status=%lu", getpid(), procPath(), (__bridge void *)cb, className(cb), sel_getName(sel), (unsigned long)cb.status);
    [cb addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        logLine("PRODUCER_COMPLETED pid=%d path=%s cmd=%p cmdClass=%s status=%lu error=%s drawable=%p", getpid(), procPath(), (__bridge void *)completed, className(completed), (unsigned long)completed.status, completed.error ? completed.error.localizedDescription.UTF8String : "none", (__bridge void *)d);
    }];
}
static void hookPresentDrawable(id self, SEL _cmd, id drawable) {
    IMP orig = originalFor(self, _cmd);
    if (drawable && [drawable conformsToProtocol:@protocol(CAMetalDrawable)]) logCompletion(self, drawable, _cmd);
    else logLine("PRESENT_NON_CAMETAL pid=%d path=%s cmd=%p cmdClass=%s sel=%s drawable=%p drawableClass=%s", getpid(), procPath(), (__bridge void *)self, className(self), sel_getName(_cmd), (__bridge void *)drawable, className(drawable));
    if (orig) ((void (*)(id, SEL, id))orig)(self, _cmd, drawable);
}
static void hookPresentDrawableAtTime(id self, SEL _cmd, id drawable, CFTimeInterval when) {
    IMP orig = originalFor(self, _cmd);
    if (drawable && [drawable conformsToProtocol:@protocol(CAMetalDrawable)]) logCompletion(self, drawable, _cmd);
    else logLine("PRESENT_NON_CAMETAL pid=%d path=%s cmd=%p sel=%s drawable=%p", getpid(), procPath(), (__bridge void *)self, sel_getName(_cmd), (__bridge void *)drawable);
    if (orig) ((void (*)(id, SEL, id, CFTimeInterval))orig)(self, _cmd, drawable, when);
}
static void hookPresentDrawableDuration(id self, SEL _cmd, id drawable, CFTimeInterval duration) {
    IMP orig = originalFor(self, _cmd);
    if (drawable && [drawable conformsToProtocol:@protocol(CAMetalDrawable)]) logCompletion(self, drawable, _cmd);
    else logLine("PRESENT_NON_CAMETAL pid=%d path=%s cmd=%p sel=%s drawable=%p", getpid(), procPath(), (__bridge void *)self, sel_getName(_cmd), (__bridge void *)drawable);
    if (orig) ((void (*)(id, SEL, id, CFTimeInterval))orig)(self, _cmd, drawable, duration);
}
static void hookDrawablePresent(id self, SEL _cmd) {
    IMP orig = originalFor(self, _cmd);
    if ([self conformsToProtocol:@protocol(CAMetalDrawable)]) {
        recordSelectedPresent(self, sel_getName(_cmd));
        logTexture("DRAWABLE_PRESENT", self);
    }
    if (orig) ((void (*)(id, SEL))orig)(self, _cmd);
}
static void hookDrawablePresentAtTime(id self, SEL _cmd, CFTimeInterval when) {
    IMP orig = originalFor(self, _cmd);
    if ([self conformsToProtocol:@protocol(CAMetalDrawable)]) {
        recordSelectedPresent(self, sel_getName(_cmd));
        logTexture("DRAWABLE_PRESENT_AT_TIME", self);
    }
    if (orig) ((void (*)(id, SEL, CFTimeInterval))orig)(self, _cmd, when);
}
static void hookDrawablePresentDuration(id self, SEL _cmd, CFTimeInterval duration) {
    IMP orig = originalFor(self, _cmd);
    if ([self conformsToProtocol:@protocol(CAMetalDrawable)]) {
        recordSelectedPresent(self, sel_getName(_cmd));
        logTexture("DRAWABLE_PRESENT_AFTER_DURATION", self);
    }
    if (orig) ((void (*)(id, SEL, CFTimeInterval))orig)(self, _cmd, duration);
}
static BOOL directMethod(Class c, SEL sel, const char **types) {
    unsigned count = 0; Method *methods = class_copyMethodList(c, &count); BOOL yes = NO;
    for (unsigned i = 0; i < count; ++i) if (method_getName(methods[i]) == sel) { if (types) *types = method_getTypeEncoding(methods[i]); yes = YES; break; }
    free(methods); return yes;
}
static BOOL alreadyHooked(Class c, SEL sel) {
    for (size_t i = 0; i < hookCount; ++i) if (hooks[i].cls == c && hooks[i].sel == sel) return YES;
    return NO;
}
static void installHook(Class c, SEL sel, IMP replacement) {
    const char *types = NULL;
    if (!directMethod(c, sel, &types) || !types) return;
    pthread_mutex_lock(&hookLock);
    if (!alreadyHooked(c, sel) && hookCount < MAX_HOOKS) {
        Method m = class_getInstanceMethod(c, sel);
        IMP orig = method_getImplementation(m);
        method_setImplementation(m, replacement);
        hooks[hookCount++] = (Hook){ .cls = c, .sel = sel, .original = orig };
        logLine("HOOK_INSTALLED pid=%d path=%s class=%s selector=%s", getpid(), procPath(), class_getName(c), sel_getName(sel));
    }
    pthread_mutex_unlock(&hookLock);
}
static void scanMetalClasses(void) {
    int n = objc_getClassList(NULL, 0); if (n <= 0) return;
    Class __unsafe_unretained *classes = (Class __unsafe_unretained *)calloc((size_t)n, sizeof(Class)); if (!classes) return;
    n = objc_getClassList(classes, n);
    Protocol *cbProto = objc_getProtocol("MTLCommandBuffer");
    Protocol *drawableProto = objc_getProtocol("MTLDrawable");
    SEL p1 = @selector(presentDrawable:), p2 = @selector(presentDrawable:atTime:), p3 = @selector(presentDrawable:afterMinimumDuration:);
    SEL d1 = @selector(present), d2 = @selector(presentAtTime:), d3 = @selector(presentAfterMinimumDuration:);
    for (int i = 0; i < n; ++i) {
        Class c = classes[i];
        if (cbProto && class_conformsToProtocol(c, cbProto)) {
            installHook(c, p1, (IMP)hookPresentDrawable);
            installHook(c, p2, (IMP)hookPresentDrawableAtTime);
            installHook(c, p3, (IMP)hookPresentDrawableDuration);
        }
        if (drawableProto && class_conformsToProtocol(c, drawableProto)) {
            installHook(c, d1, (IMP)hookDrawablePresent);
            installHook(c, d2, (IMP)hookDrawablePresentAtTime);
            installHook(c, d3, (IMP)hookDrawablePresentDuration);
        }
    }
    free(classes);
}

static BOOL relevantImage(const char *path) {
    if (!path) return NO;
    return strstr(path, "D3DMetal") || strstr(path, "d3dmetal") || strstr(path, "lsfg") ||
           strstr(path, "Lossless Scaling") || strstr(path, "libhbaudiobuf") ||
           strstr(path, "libfgmetal_observer");
}
static void imageAdded(const struct mach_header *mh, intptr_t slide) {
    (void)slide;
    uint32_t count = _dyld_image_count();
    for (uint32_t i = 0; i < count; ++i) {
        if (_dyld_get_image_header(i) == mh) {
            const char *path = _dyld_get_image_name(i);
            if (relevantImage(path)) logLine("DYLD_IMAGE pid=%d path=%s image=%s", getpid(), procPath(), path);
            return;
        }
    }
}

static void *pollClasses(void *unused) {
    (void)unused;
    for (int i = 0; i < 240; ++i) { scanMetalClasses(); usleep(250000); }
    while (1) { scanMetalClasses(); sleep(2); }
    return NULL;
}
__attribute__((constructor)) static void observerInit(void) {
    logLine("LOADED pid=%d ppid=%d path=%s arch=x86_64 DYLD_INSERT_LIBRARIES=%s LSFGM_DLL_PATH=%s", getpid(), getppid(), procPath(), getenv("DYLD_INSERT_LIBRARIES") ?: "<unset>", getenv("LSFGM_DLL_PATH") ?: "<unset>");
    _dyld_register_func_for_add_image(imageAdded);
    (void)dlopen("/System/Library/Frameworks/QuartzCore.framework/QuartzCore", RTLD_LAZY | RTLD_LOCAL);
    (void)dlopen("/System/Library/Frameworks/Metal.framework/Metal", RTLD_LAZY | RTLD_LOCAL);
    Class layer = objc_getClass("CAMetalLayer");
    SEL next = @selector(nextDrawable);
    Method m = layer ? class_getInstanceMethod(layer, next) : NULL;
    if (m) { originalNextDrawable = method_setImplementation(m, (IMP)hookNextDrawable); logLine("HOOK_INSTALLED pid=%d path=%s class=CAMetalLayer selector=nextDrawable", getpid(), procPath()); }
    else logLine("HOOK_UNAVAILABLE pid=%d path=%s class=CAMetalLayer selector=nextDrawable", getpid(), procPath());
    pthread_t thread; if (pthread_create(&thread, NULL, pollClasses, NULL) == 0) pthread_detach(thread);
}
