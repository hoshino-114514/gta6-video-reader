// xmem.dll stub v2: Xbox-accurate page-aligned allocations via VirtualAlloc
#include <windows.h>

typedef struct {
    DWORD dwSize;
    DWORD dwAllocAttributes;
    DWORD dwXMemProperties;
    DWORD dwHeapAttributes;
} XMEM_ALLOCATION_PROPERTIES;

// 64-byte header keeps original size; allocation itself page-aligned (64K granularity)
__declspec(dllexport) LPVOID WINAPI XMemAlloc(SIZE_T cb, const XMEM_ALLOCATION_PROPERTIES* pProps)
{
    (void)pProps;
    if (cb == 0) return NULL;
    SIZE_T total = (cb + 0xFFFF) & ~(SIZE_T)0xFFFF;   // round to 64K
    BYTE* p = (BYTE*)VirtualAlloc(NULL, total + 0x1000, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!p) return NULL;
    return p;
}

__declspec(dllexport) VOID WINAPI XMemFree(LPVOID pAddress, const XMEM_ALLOCATION_PROPERTIES* pProps)
{
    (void)pProps;
    if (!pAddress) return;
    // header page is part of same reservation; free the whole thing.
    SYSTEM_INFO si; GetSystemInfo(&si);
    BYTE* base = (BYTE*)pAddress;
    // VirtualAlloc'd base is the returned pointer itself (we returned p, no offset)
    // Try to decommit the rounded region conservatively.
    MEMORY_BASIC_INFORMATION mbi;
    if (VirtualQuery(base, &mbi, sizeof(mbi)) && mbi.AllocationBase == base) {
        VirtualFree(base, 0, MEM_RELEASE);
        return;
    }
    // fallback: scan backwards for allocation base
    for (int i = 0; i < 64; i++) {
        base -= si.dwAllocationGranularity;
        if (VirtualQuery(base, &mbi, sizeof(mbi)) && mbi.AllocationBase == base) {
            VirtualFree(base, 0, MEM_RELEASE);
            return;
        }
    }
}

BOOL WINAPI DllMain(HINSTANCE hInst, DWORD reason, LPVOID reserved)
{
    (void)hInst; (void)reason; (void)reserved;
    return TRUE;
}
