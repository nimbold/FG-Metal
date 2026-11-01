#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <MetalFX/MetalFX.h>

int main(void) {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (device == nil) {
            puts("device=none");
            return 2;
        }
        printf("device=%s\n", device.name.UTF8String);
        printf("supportsDevice=%s\n",
               [MTLFXFrameInterpolatorDescriptor supportsDevice:device] ? "true" : "false");
        printf("supportsMetal4FX=%s\n",
               [MTLFXFrameInterpolatorDescriptor supportsMetal4FX:device] ? "true" : "false");
        return 0;
    }
}
