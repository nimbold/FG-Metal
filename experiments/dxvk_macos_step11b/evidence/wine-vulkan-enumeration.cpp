#define VK_ENABLE_BETA_EXTENSIONS
#define VK_USE_PLATFORM_WIN32_KHR
#include <windows.h>
#include <vulkan/vulkan.h>
#include <cstdio>
#include <vector>
#include <cstring>
int main(){
 HMODULE lib=LoadLibraryA("vulkan-1.dll");if(!lib)return 2;
 auto g=(PFN_vkGetInstanceProcAddr)GetProcAddress(lib,"vkGetInstanceProcAddr");if(!g)return 3;
 auto enumerate=(PFN_vkEnumerateInstanceExtensionProperties)g(nullptr,"vkEnumerateInstanceExtensionProperties");
 uint32_t n=0;enumerate(nullptr,&n,nullptr);std::vector<VkExtensionProperties> exts(n);enumerate(nullptr,&n,exts.data());bool portability=false;for(auto& x:exts){if(!strcmp(x.extensionName,VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME))portability=true;}
 printf("wine_portability_enumeration=%d\n",portability);
 const char* ie[]={VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME};VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};app.apiVersion=VK_API_VERSION_1_3;VkInstanceCreateInfo ci{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};ci.pApplicationInfo=&app;ci.flags=portability?VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR:0;ci.enabledExtensionCount=portability?1:0;ci.ppEnabledExtensionNames=ie;
 auto create=(PFN_vkCreateInstance)g(nullptr,"vkCreateInstance");VkInstance instance{};auto r=create(&ci,nullptr,&instance);printf("instance=%d\n",r);if(r)return 4;
 #define GET(name) auto name=(PFN_##name)g(instance,#name)
 GET(vkEnumeratePhysicalDevices);GET(vkGetPhysicalDeviceProperties2);GET(vkEnumerateDeviceExtensionProperties);GET(vkGetPhysicalDeviceQueueFamilyProperties);GET(vkCreateDevice);GET(vkDestroyDevice);GET(vkDestroyInstance);
 n=0;vkEnumeratePhysicalDevices(instance,&n,nullptr);std::vector<VkPhysicalDevice> devices(n);vkEnumeratePhysicalDevices(instance,&n,devices.data());bool success=false;
 for(auto p:devices){VkPhysicalDeviceDriverProperties driver{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES};VkPhysicalDeviceProperties2 props{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2,&driver};vkGetPhysicalDeviceProperties2(p,&props);printf("device=%s driverInfo=%s\n",props.properties.deviceName,driver.driverInfo);
 uint32_t ne=0;vkEnumerateDeviceExtensionProperties(p,nullptr,&ne,nullptr);std::vector<VkExtensionProperties> de(ne);vkEnumerateDeviceExtensionProperties(p,nullptr,&ne,de.data());bool subset=false;for(auto& x:de)if(!strcmp(x.extensionName,VK_KHR_PORTABILITY_SUBSET_EXTENSION_NAME))subset=true;printf("portability_subset=%d\n",subset);
 uint32_t nq=0;vkGetPhysicalDeviceQueueFamilyProperties(p,&nq,nullptr);std::vector<VkQueueFamilyProperties> qs(nq);vkGetPhysicalDeviceQueueFamilyProperties(p,&nq,qs.data());uint32_t q=0;while(q<nq && !(qs[q].queueFlags&VK_QUEUE_GRAPHICS_BIT))q++;
 float priority=1;VkDeviceQueueCreateInfo qi{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};qi.queueFamilyIndex=q;qi.queueCount=1;qi.pQueuePriorities=&priority;const char* dex[]={VK_KHR_PORTABILITY_SUBSET_EXTENSION_NAME};VkDeviceCreateInfo dc{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};dc.queueCreateInfoCount=1;dc.pQueueCreateInfos=&qi;dc.enabledExtensionCount=subset?1:0;dc.ppEnabledExtensionNames=dex;VkDevice d{};r=vkCreateDevice(p,&dc,nullptr,&d);printf("device_create=%d\n",r);if(!r){success=true;vkDestroyDevice(d,nullptr);}}
 vkDestroyInstance(instance,nullptr);puts("clean_teardown=yes");return success?0:5;
}
