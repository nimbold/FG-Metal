#include <dlfcn.h>
#include <mach-o/dyld.h>
#include <sys/sysctl.h>
#include <sys/utsname.h>
#include <unistd.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <limits.h>

int main(int argc, char **argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: collision_harness ABSOLUTE_PROXY_PATH\n");
        return 2;
    }
    struct utsname info{};
    int translated = -1;
    size_t translated_size = sizeof(translated);
    if (uname(&info) != 0) return 3;
    if (sysctlbyname("sysctl.proc_translated", &translated, &translated_size,
                     nullptr, 0) != 0) translated = -1;
    std::printf("harness_pid=%d\nprocess_architecture=%s\nprocess_translated=%d\n",
                getpid(), info.machine, translated);
    std::fflush(stdout);
    if (std::strcmp(info.machine, "x86_64") != 0 || translated != 1) return 4;

    void *handle = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        std::fprintf(stderr, "proxy_dlopen_error=%s\n", dlerror());
        return 5;
    }
    auto gipa = dlsym(handle, "vkGetInstanceProcAddr");
    if (!gipa) {
        std::fprintf(stderr, "proxy_export_error=%s\n", dlerror());
        return 6;
    }
    Dl_info loaded{};
    if (!dladdr(gipa, &loaded) || !loaded.dli_fname) return 7;
    char resolved[PATH_MAX]{};
    if (!realpath(loaded.dli_fname, resolved)) return 8;
    std::printf("proxy_loaded_path=%s\nproxy_loaded=1\n", resolved);
    std::printf("READY pid=%d\n", getpid());
    std::fflush(stdout);

    std::string release;
    std::getline(std::cin, release);
    return 0;
}
