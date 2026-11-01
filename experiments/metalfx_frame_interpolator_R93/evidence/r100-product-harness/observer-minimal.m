#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#import <objc/runtime.h>
#import <libproc.h>
#import <dlfcn.h>
#import <mach-o/dyld.h>
#import <fcntl.h>
#import <pthread.h>
#import <sys/sysctl.h>
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
static pthread_once_t targetOnce = PTHREAD_ONCE_INIT;
static BOOL harnessProcess = NO;
static const char *const kHarnessExecutable =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r100-product-harness/fgmetal-r100-d3d11-harness.exe";

static void initLog(void) {
    char path[256];
    (void)mkdir("/tmp/fgmetal-r100-harness", 0700);
    snprintf(path, sizeof(path), "/tmp/fgmetal-r100-harness/minimal.%d.log", getpid());
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
static void identifyHarnessProcess(void) {
    int mib[] = { CTL_KERN, KERN_PROCARGS2, getpid() };
    size_t size = 0;
    if (sysctl(mib, 3, NULL, &size, NULL, 0) != 0 || size < sizeof(int)) return;
    char *buffer = malloc(size);
    if (!buffer) return;
    if (sysctl(mib, 3, buffer, &size, NULL, 0) != 0 || size < sizeof(int)) { free(buffer); return; }

    int argc = 0;
    memcpy(&argc, buffer, sizeof(argc));
    char *cursor = buffer + sizeof(argc);
    char *end = buffer + size;
    size_t remaining = (size_t)(end - cursor);
    size_t execPathLength = strnlen(cursor, remaining);
    if (execPathLength < remaining) cursor += execPathLength + 1;
    while (cursor < end && *cursor == '\0') cursor++;
    size_t targetLength = strlen(kHarnessExecutable);
    for (int i = 0; i < argc && cursor < end; ++i) {
        size_t argumentLength = strnlen(cursor, (size_t)(end - cursor));
        if (argumentLength == targetLength && memcmp(cursor, kHarnessExecutable, targetLength) == 0) {
            harnessProcess = YES;
            break;
        }
        if (argumentLength >= (size_t)(end - cursor)) break;
        cursor += argumentLength + 1;
    }
    free(buffer);
}
static BOOL isHarnessProcess(void) {
    pthread_once(&targetOnce, identifyHarnessProcess);
    return harnessProcess;
}
static id hookNextDrawable(id self, SEL _cmd) {
    id<CAMetalDrawable> result = ((id (*)(id, SEL))originalNextDrawable)(self, _cmd);
    if (isHarnessProcess()) {
        if (result) logTexture("NEXT_DRAWABLE", result);
        else logLine("NEXT_DRAWABLE_NIL pid=%d path=%s layer=%p", getpid(), procPath(), (__bridge void *)self);
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
static void hookDrawablePresent(id self, SEL _cmd) {
    IMP orig = originalFor(self, _cmd);
    if (isHarnessProcess() && [self conformsToProtocol:@protocol(CAMetalDrawable)]) {
        logTexture("DRAWABLE_PRESENT", self);
    }
    if (orig) ((void (*)(id, SEL))orig)(self, _cmd);
}
static void hookDrawablePresentAtTime(id self, SEL _cmd, CFTimeInterval when) {
    IMP orig = originalFor(self, _cmd);
    if (isHarnessProcess() && [self conformsToProtocol:@protocol(CAMetalDrawable)]) {
        logTexture("DRAWABLE_PRESENT_AT_TIME", self);
    }
    if (orig) ((void (*)(id, SEL, CFTimeInterval))orig)(self, _cmd, when);
}
static void hookDrawablePresentDuration(id self, SEL _cmd, CFTimeInterval duration) {
    IMP orig = originalFor(self, _cmd);
    if (isHarnessProcess() && [self conformsToProtocol:@protocol(CAMetalDrawable)]) {
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
    Protocol *drawableProto = objc_getProtocol("MTLDrawable");
    SEL d1 = @selector(present), d2 = @selector(presentAtTime:), d3 = @selector(presentAfterMinimumDuration:);
    for (int i = 0; i < n; ++i) {
        Class c = classes[i];
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
    logLine("LOADED pid=%d ppid=%d path=%s arch=x86_64 target=%s targetArgMatch=%d DYLD_INSERT_LIBRARIES=%s", getpid(), getppid(), procPath(), kHarnessExecutable, isHarnessProcess(), getenv("DYLD_INSERT_LIBRARIES") ?: "<unset>");
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
