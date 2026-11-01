#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 2) return 2;
        NSError *error = nil;
        NSString *path = [NSString stringWithUTF8String:argv[1]];
        NSString *source = [NSString stringWithContentsOfFile:path
            encoding:NSUTF8StringEncoding error:&error];
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        id<MTLLibrary> library = source
            ? [device newLibraryWithSource:source options:nil error:&error] : nil;
        if (!library) {
            fprintf(stderr, "R107_METAL_LIBRARY=FAILED error=%s\n",
                error.localizedDescription.UTF8String ?: "unknown");
            return 1;
        }
        const char *names[] = {"r107BlockMatch", "r107Propagate",
            "r107Upsample", "r107Prepare", "r107ClearMotion"};
        for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
            NSString *name = [NSString stringWithUTF8String:names[i]];
            id<MTLFunction> function = [library newFunctionWithName:name];
            id<MTLComputePipelineState> pipeline = function
                ? [device newComputePipelineStateWithFunction:function error:&error] : nil;
            if (!pipeline) {
                fprintf(stderr, "R107_METAL_PIPELINE=FAILED name=%s error=%s\n",
                    names[i], error.localizedDescription.UTF8String ?: "missing function");
                return 1;
            }
            printf("R107_METAL_PIPELINE=READY name=%s maxThreads=%lu\n",
                names[i], (unsigned long)pipeline.maxTotalThreadsPerThreadgroup);
        }
        return 0;
    }
}
