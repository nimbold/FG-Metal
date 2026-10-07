#define VK_ENABLE_BETA_EXTENSIONS
#include <vulkan/vulkan.h>
#include <dlfcn.h>
#include <cstdio>
#include <vector>
#include <fstream>
#include <cstring>
// Explicit library argument and dladdr identity prevent rpath/architecture ambiguity.
int main(int argc,char** argv) {
 if(argc!=4) return 2;
 void* lib=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL); if(!lib){puts(dlerror());return 3;}
 auto gipa=(PFN_vkGetInstanceProcAddr)dlsym(lib,"vkGetInstanceProcAddr");
 Dl_info di{};dladdr((void*)gipa,&di);printf("loaded=%s\n",di.dli_fname);
 #define GLOBAL(n) auto n=(PFN_##n)gipa(nullptr,#n)
 GLOBAL(vkCreateInstance); GLOBAL(vkEnumerateInstanceExtensionProperties);
 uint32_t ne=0;vkEnumerateInstanceExtensionProperties(nullptr,&ne,nullptr);std::vector<VkExtensionProperties> exts(ne);vkEnumerateInstanceExtensionProperties(nullptr,&ne,exts.data());bool portability=false;for(auto& e:exts)if(!strcmp(e.extensionName,VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME))portability=true;
 const char* ie[]={VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME};
 VkApplicationInfo ai{VK_STRUCTURE_TYPE_APPLICATION_INFO};ai.apiVersion=VK_API_VERSION_1_1;
 VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};ici.flags=portability?VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR:0;ici.pApplicationInfo=&ai;ici.enabledExtensionCount=portability?1:0;ici.ppEnabledExtensionNames=ie;
 VkInstance inst{};auto r=vkCreateInstance(&ici,nullptr,&inst);if(r){printf("instance=%d\n",r);return 4;}
 #define INST(n) auto n=(PFN_##n)gipa(inst,#n)
 INST(vkEnumeratePhysicalDevices);INST(vkGetPhysicalDeviceProperties2);INST(vkGetPhysicalDeviceFeatures2);INST(vkGetPhysicalDeviceQueueFamilyProperties);INST(vkCreateDevice);INST(vkDestroyInstance);INST(vkGetDeviceProcAddr);
 uint32_t count=1;VkPhysicalDevice p{};vkEnumeratePhysicalDevices(inst,&count,&p);
 VkPhysicalDeviceDriverProperties driver{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES};VkPhysicalDeviceProperties2 props{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2,&driver};vkGetPhysicalDeviceProperties2(p,&props);printf("device=%s driverInfo=%s\n",props.properties.deviceName,driver.driverInfo);
 VkPhysicalDeviceShaderDrawParametersFeatures draw{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SHADER_DRAW_PARAMETERS_FEATURES};VkPhysicalDeviceFeatures2 f{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2,&draw};vkGetPhysicalDeviceFeatures2(p,&f);printf("shaderDrawParameters=%u\n",draw.shaderDrawParameters);
 uint32_t nq=0;vkGetPhysicalDeviceQueueFamilyProperties(p,&nq,nullptr);std::vector<VkQueueFamilyProperties> qs(nq);vkGetPhysicalDeviceQueueFamilyProperties(p,&nq,qs.data());uint32_t q=0;while(q<nq && !(qs[q].queueFlags&VK_QUEUE_GRAPHICS_BIT))q++;
 float priority=1;VkDeviceQueueCreateInfo qi{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};qi.queueFamilyIndex=q;qi.queueCount=1;qi.pQueuePriorities=&priority;
 const char* de[]={VK_KHR_PORTABILITY_SUBSET_EXTENSION_NAME};VkDeviceCreateInfo dci{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,&draw};dci.queueCreateInfoCount=1;dci.pQueueCreateInfos=&qi;dci.enabledExtensionCount=1;dci.ppEnabledExtensionNames=de;VkDevice dev{};r=vkCreateDevice(p,&dci,nullptr,&dev);if(r){printf("device_create=%d\n",r);vkDestroyInstance(inst,nullptr);return 5;}
 #define DEV(n) auto n=(PFN_##n)vkGetDeviceProcAddr(dev,#n)
 DEV(vkCreateShaderModule);DEV(vkDestroyShaderModule);DEV(vkCreateRenderPass);DEV(vkDestroyRenderPass);DEV(vkCreatePipelineLayout);DEV(vkDestroyPipelineLayout);DEV(vkCreateGraphicsPipelines);DEV(vkDestroyPipeline);DEV(vkDeviceWaitIdle);DEV(vkDestroyDevice);
 auto shader=[&](const char* path){std::ifstream in(path,std::ios::binary|std::ios::ate);size_t size=in.tellg();std::vector<uint32_t> code((size+3)/4);in.seekg(0);in.read((char*)code.data(),size);VkShaderModuleCreateInfo ci{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};ci.codeSize=size;ci.pCode=code.data();VkShaderModule s{};auto sr=vkCreateShaderModule(dev,&ci,nullptr,&s);printf("shader=%s result=%d\n",path,sr);return s;};
 VkShaderModule vs=shader(argv[2]),fs=shader(argv[3]);
 VkAttachmentDescription a{};a.format=VK_FORMAT_B8G8R8A8_UNORM;a.samples=VK_SAMPLE_COUNT_1_BIT;a.loadOp=VK_ATTACHMENT_LOAD_OP_DONT_CARE;a.storeOp=VK_ATTACHMENT_STORE_OP_STORE;a.initialLayout=VK_IMAGE_LAYOUT_UNDEFINED;a.finalLayout=VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
 VkAttachmentReference ar{0,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};VkSubpassDescription sub{};sub.pipelineBindPoint=VK_PIPELINE_BIND_POINT_GRAPHICS;sub.colorAttachmentCount=1;sub.pColorAttachments=&ar;VkRenderPassCreateInfo rpci{VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO};rpci.attachmentCount=1;rpci.pAttachments=&a;rpci.subpassCount=1;rpci.pSubpasses=&sub;VkRenderPass rp{};vkCreateRenderPass(dev,&rpci,nullptr,&rp);
 VkPipelineLayoutCreateInfo lci{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};VkPipelineLayout layout{};vkCreatePipelineLayout(dev,&lci,nullptr,&layout);
 VkPipelineShaderStageCreateInfo stages[2]{};for(int i=0;i<2;i++){stages[i].sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;stages[i].pName="main";}stages[0].stage=VK_SHADER_STAGE_VERTEX_BIT;stages[0].module=vs;stages[1].stage=VK_SHADER_STAGE_FRAGMENT_BIT;stages[1].module=fs;
 VkPipelineVertexInputStateCreateInfo vi{VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO};VkPipelineInputAssemblyStateCreateInfo ia{VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO};ia.topology=VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
 VkViewport vp{0,0,16,16,0,1};VkRect2D sc{{0,0},{16,16}};VkPipelineViewportStateCreateInfo vpi{VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO};vpi.viewportCount=1;vpi.pViewports=&vp;vpi.scissorCount=1;vpi.pScissors=&sc;
 VkPipelineRasterizationStateCreateInfo rs{VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO};rs.polygonMode=VK_POLYGON_MODE_FILL;rs.cullMode=VK_CULL_MODE_NONE;rs.frontFace=VK_FRONT_FACE_COUNTER_CLOCKWISE;rs.lineWidth=1;
 VkPipelineMultisampleStateCreateInfo ms{VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO};ms.rasterizationSamples=VK_SAMPLE_COUNT_1_BIT;
 VkPipelineColorBlendAttachmentState ba{};ba.colorWriteMask=15;VkPipelineColorBlendStateCreateInfo cb{VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO};cb.attachmentCount=1;cb.pAttachments=&ba;
 VkGraphicsPipelineCreateInfo pc{VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO};pc.stageCount=2;pc.pStages=stages;pc.pVertexInputState=&vi;pc.pInputAssemblyState=&ia;pc.pViewportState=&vpi;pc.pRasterizationState=&rs;pc.pMultisampleState=&ms;pc.pColorBlendState=&cb;pc.layout=layout;pc.renderPass=rp;
 VkPipeline pipeline{};r=vkCreateGraphicsPipelines(dev,VK_NULL_HANDLE,1,&pc,nullptr,&pipeline);printf("pipeline_result=%d\n",r);
 vkDeviceWaitIdle(dev);if(pipeline)vkDestroyPipeline(dev,pipeline,nullptr);vkDestroyPipelineLayout(dev,layout,nullptr);vkDestroyRenderPass(dev,rp,nullptr);vkDestroyShaderModule(dev,fs,nullptr);vkDestroyShaderModule(dev,vs,nullptr);vkDestroyDevice(dev,nullptr);vkDestroyInstance(inst,nullptr);
 // Do not unload Objective-C implementation code while runtime classes remain registered.
 printf("clean_teardown=yes\n");return r==VK_SUCCESS?0:10;
}
