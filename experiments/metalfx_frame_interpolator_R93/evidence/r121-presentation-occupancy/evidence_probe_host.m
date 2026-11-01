// R119 evidence-isolation probe host.
//
// Loads the observer dylib into a disposable process so its constructor -- and
// therefore the real evidence accessor, the real path composer, the real blob
// writer and the real corpus guard -- executes without Elden Ring, without a
// Metal device, and without a single frame of gameplay.
//
// The dylib is dlopen()ed rather than DYLD-injected because the SIP-protected
// system binaries refuse DYLD_INSERT_LIBRARIES, and a self-contained host keeps
// the probe reproducible on any machine.
//
// Usage: evidence_probe_host <path-to-dylib>
// Exit:  0 = dylib loaded, 2 = bad arguments, 3 = dlopen failed.

#import <Foundation/Foundation.h>
#import <dlfcn.h>
#import <stdio.h>

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc < 2) {
            fprintf(stderr, "usage: evidence_probe_host <path-to-dylib>\n");
            return 2;
        }
        const char *path = argv[1];
        void *handle = dlopen(path, RTLD_NOW);
        if (!handle) {
            fprintf(stderr, "PROBE_HOST=FAILED dlopen=%s error=%s\n", path, dlerror());
            return 3;
        }
        printf("PROBE_HOST=OK pid=%d dylib=%s\n", getpid(), path);
        fflush(stdout);
        // The dylib's constructor has already run and written whatever it was
        // going to write. Nothing else is needed here.
        (void)dlclose(handle);
    }
    return 0;
}
