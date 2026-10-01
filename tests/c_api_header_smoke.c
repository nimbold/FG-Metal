#include "framegen/api.h"

int main(void) {
    framegen_abi_info_t info = {0};
    info.struct_size = (uint32_t)sizeof(info);
    info.struct_version = FRAMEGEN_ABI_VERSION;
    if (framegen_get_abi_info(&info) != FRAMEGEN_STATUS_OK) {
        return 1;
    }
    return info.abi_version == FRAMEGEN_ABI_VERSION &&
           info.stability == FRAMEGEN_ABI_PROVISIONAL ? 0 : 2;
}
