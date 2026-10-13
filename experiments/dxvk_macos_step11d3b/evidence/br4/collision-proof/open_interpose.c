#define _DARWIN_C_SOURCE 1
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <unistd.h>

#define DYLD_INTERPOSE(_replacement, _replacee) \
    __attribute__((used)) static struct { \
        const void *replacement; \
        const void *replacee; \
    } _interpose_##_replacement __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(unsigned long)&_replacement, \
        (const void *)(unsigned long)&_replacee \
    }

extern int original_open_nocancel(const char *, int, ...) __asm("_open$NOCANCEL");

static int should_log(const char *path) {
    const char *target = getenv("FG_B_MVK_TRACE_PATH");
    return path && target && strcmp(path, target) == 0;
}

static void log_open(const char *path, int flags, mode_t mode,
                     int result, int saved_errno) {
    if (!should_log(path)) return;
    char line[PATH_MAX + 512];
    int length = snprintf(line, sizeof(line),
        "DYLD_INTERPOSE pid=%d symbol=open path=%s flags_dec=%d flags_hex=0x%x "
        "mode_dec=%u mode_octal=0%o result=%d errno=%d errno_text=%s\n",
        getpid(), path, flags, (unsigned int)flags,
        (unsigned int)mode, mode, result, saved_errno, strerror(saved_errno));
    if (length > 0) {
        size_t count = (size_t)length < sizeof(line) ? (size_t)length : sizeof(line) - 1;
        (void)write(STDERR_FILENO, line, count);
    }
}

int interposed_open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags);
        mode = (mode_t)va_arg(args, int);
        va_end(args);
    }
    int target = should_log(path);
    int incoming_errno = errno;
    if (target) errno = 0;
    int result;
    result = (flags & O_CREAT)
        ? original_open_nocancel(path, flags, mode)
        : original_open_nocancel(path, flags);
    int saved_errno = errno;
    if (target) log_open(path, flags, mode, result, saved_errno);
    errno = target ? saved_errno : incoming_errno;
    return result;
}

DYLD_INTERPOSE(interposed_open, open);

__attribute__((constructor)) static void initialize_interposer(void) {
    struct utsname info;
    int translated = -1;
    size_t translated_size = sizeof(translated);
    if (uname(&info) != 0) memset(&info, 0, sizeof(info));
    if (sysctlbyname("sysctl.proc_translated", &translated, &translated_size,
                     NULL, 0) != 0) translated = -1;
    char line[256];
    int length = snprintf(line, sizeof(line),
        "DYLD_INTERPOSE_PROCESS pid=%d architecture=%s translated=%d forwarder=open$NOCANCEL\n",
        getpid(), info.machine, translated);
    if (length > 0) (void)write(STDERR_FILENO, line,
        (size_t)length < sizeof(line) ? (size_t)length : sizeof(line) - 1);
}
