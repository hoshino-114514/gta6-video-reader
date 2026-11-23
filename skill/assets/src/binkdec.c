// Bink2Scarlett.dll host v7: adds "aud" mode = per-frame BinkGetTrackData PCM ->
// WAV (48kHz stereo s16le from track-entry bits: rate=low16, ch=((e>>29)&1)+1).
// Verified API semantics (probe): BinkGetTrackType/MaxSize/ID take (h, idx) and
// read tables at h+0x1e8/h+0x1e0 (idx < *(u32*)(h+0x38)); BinkOpenTrack(h, idx)
// requires BinkOpenXAudio2("default",0) to have run first; BinkGetTrackData
// takes (track, buf, cap) and returns bytes written (7680 B/frame at 30fps,
// 145920 B first frame).
// v6: YV12 chroma-order fix (see below). v5: raw mode. v4: mem IO + BMP.
#include <windows.h>
#include <sys/stat.h>
#include <io.h>
#include <fcntl.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef void* HBINK;
typedef struct {
    void*  ptr;     unsigned int flag;   unsigned int stride;
    void*  uptr;    unsigned int uflag;  unsigned int ustride;
    void*  vptr;    unsigned int vflag;  unsigned int vstride;
    void*  aptr;    unsigned int aflag;  unsigned int astride;
    void*  a2ptr;   unsigned int a2flag; unsigned int a2stride;
} PlaneDesc;
typedef struct {
    unsigned int num_buffers;
    unsigned int width;
    unsigned int height;
    unsigned int width_uv;
    unsigned int height_uv;
    unsigned int cur_idx;
    PlaneDesc planes[3];
} FBInfo;

typedef HBINK (__cdecl *PFN_Open)(const char*, unsigned int);
typedef void   (__cdecl *PFN_Close)(HBINK);
typedef int    (__cdecl *PFN_DoFrame)(HBINK);
typedef void   (__cdecl *PFN_GetFBI)(HBINK, FBInfo*);
typedef void   (__cdecl *PFN_RegFB)(HBINK, FBInfo*);
typedef int    (__cdecl *PFN_Summary)(HBINK, void*, unsigned int);
typedef int    (__cdecl *PFN_NextFrame)(HBINK);
typedef int    (__cdecl *PFN_Goto)(HBINK, unsigned int, int);
typedef int    (__cdecl *PFN_SetSound)(HBINK, int);
typedef void   (__cdecl *PFN_AllocFB)(HBINK, FBInfo*);
typedef int    (__cdecl *PFN_Wait)(HBINK);
typedef const char* (__cdecl *PFN_GetError)(void);
typedef void*  (__cdecl *PFN_OpenXA)(const char*, unsigned int);
typedef void*  (__cdecl *PFN_OpenTrack)(HBINK, unsigned int);
typedef unsigned (__cdecl *PFN_MaxSize)(HBINK, unsigned int);
typedef int    (__cdecl *PFN_GetData)(void*, void*, unsigned int);

static PFN_Open pOpen; static PFN_Close pClose; static PFN_DoFrame pDoFrame;
static PFN_GetFBI pGetFBI; static PFN_RegFB pRegFB;
static PFN_Summary pSummary; static PFN_NextFrame pNextFrame;
static PFN_Goto pGoto;
static PFN_SetSound pSetSound; static PFN_Wait pWait; static PFN_GetError pGetError;
static PFN_OpenXA pOpenXA; static PFN_OpenTrack pOpenTrack;
static PFN_MaxSize pMaxSize; static PFN_GetData pGetData;
static PFN_AllocFB pAllocFB;

static volatile long g_read_count = 0;

typedef struct { unsigned char* data; unsigned long long size; unsigned long long pos; } MEMFILE;
static MEMFILE g_mf;

static unsigned __int64 __cdecl my_open(void* slot, const char* name, unsigned __int64* sizeout) {
    FILE* f = fopen(name, "rb");
    if (!f) { printf("my_open: cannot open %s\n", name); return 0; }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    g_mf.data = (unsigned char*)malloc(sz);
    g_mf.size = (unsigned __int64)sz;
    g_mf.pos = 0;
    if (fread(g_mf.data, 1, sz, f) != (size_t)sz) printf("my_open: short read\n");
    fclose(f);
    *(void**)slot = &g_mf;
    if (sizeout) *sizeout = (unsigned __int64)sz;
    printf("my_open: %s (%ld bytes)\n", name, sz);
    fflush(stdout);
    return 1;
}
static __int64 __cdecl my_seek(void* slot, long long off, int a, int b) {
    (void)slot; (void)a; (void)b;
    g_mf.pos = (unsigned __int64)off;
    return 0;
}
static unsigned __int64 __cdecl my_read(void* slot, void* dst, unsigned __int64 bytes) {
    (void)slot;
    unsigned __int64 avail = (g_mf.size > g_mf.pos) ? g_mf.size - g_mf.pos : 0;
    unsigned __int64 n = bytes < avail ? bytes : avail;
    if (n) memcpy(dst, g_mf.data + g_mf.pos, n);
    g_mf.pos += n;
    InterlockedIncrement(&g_read_count);
    if (n != bytes) printf("  read short: ask=%llu give=%llu\n",
        (unsigned long long)bytes, (unsigned long long)n);
    return n;
}
static void __cdecl my_close(void* slot) { (void)slot; }

typedef struct {
    void* fn; void* a1; void* a2; void* a3; void* a4; void* a5; void* a6; void* a7;
    volatile int done; unsigned __int64 ret;
} CALLJOB;
static DWORD WINAPI call_thread(LPVOID p) {
    CALLJOB* j = (CALLJOB*)p;
    typedef unsigned __int64 (__cdecl *F7)(void*,void*,void*,void*,void*,void*,void*);
    j->ret = ((F7)j->fn)(j->a1,j->a2,j->a3,j->a4,j->a5,j->a6,j->a7);
    j->done = 1;
    return 0;
}
static unsigned __int64 guarded(void* fn, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7,
                                unsigned int timeout_ms, const char* what) {
    CALLJOB j; memset(&j,0,sizeof(j));
    j.fn=fn; j.a1=a1; j.a2=a2; j.a3=a3; j.a4=a4; j.a5=a5; j.a6=a6; j.a7=a7;
    HANDLE th = CreateThread(NULL, 1<<20, call_thread, &j, 0, NULL);
    DWORD w = WaitForSingleObject(th, timeout_ms);
    if (w != WAIT_OBJECT_0) {
        printf("[TIMEOUT] %s after %u ms\n", what, timeout_ms);
        fflush(stdout);
        CloseHandle(th);
        return 0xDEADDEADDEADDEADULL;
    }
    CloseHandle(th);
    printf("[ok] %s -> %lld\n", what, (long long)j.ret);
    fflush(stdout);
    return j.ret;
}

static double plane_std(const unsigned char* p, int n) {
    double s = 0; for (int i=0;i<n;i++) s += p[i];
    double m = s/n; double v=0;
    for (int i=0;i<n;i++){ double d=p[i]-m; v+=d*d; }
    return sqrt(v/n);
}

int main(int argc, char** argv) {
    const char* path   = argc > 1 ? argv[1] : "a.bk2";
    int want_frames    = argc > 2 ? atoi(argv[2]) : 1;
    const char* outdir = argc > 3 ? argv[3] : ".";
    const char* tag    = argc > 4 ? argv[4] : "v";

    /* raw mode: argv[5] == "raw" (stdout) or "raw:<file.i420>" (file).
       Reroute stdout->stderr so all log printf()s leave the data stream;
       the real stdout dup is written to as packed I420. */
    int raw_mode = 0; FILE* rawout = NULL;
    if (argc > 5 && strncmp(argv[5], "raw", 3) == 0) {
        raw_mode = 1;
        int real_out;
        if (argv[5][3] == 0) {                       /* raw -> stdout pipe */
            real_out = _dup(1);
            _setmode(real_out, _O_BINARY);           /* fd1 from bash may be text-mode; force binary */
            _dup2(2, 1);                             /* printf logs -> stderr */
        } else {                                     /* raw:<path> -> file */
            real_out = _open(argv[5] + 4, _O_WRONLY | _O_CREAT | _O_TRUNC | _O_BINARY, _S_IREAD | _S_IWRITE);
            if (real_out < 0) { fprintf(stderr, "cannot open %s\n", argv[5] + 4); return 1; }
        }
        rawout = _fdopen(real_out, "wb");
        if (!rawout) { fprintf(stderr, "fdopen fail\n"); return 1; }
        setvbuf(rawout, NULL, _IOFBF, 1 << 20);
    }

    /* aud mode: argv[5] == "aud"; WAV path from argv[j]=="wav:<path>" (default out.wav) */
    int aud_mode = 0; const char* wavpath = NULL;
    if (argc > 5 && strcmp(argv[5], "aud") == 0) aud_mode = 1;
    for (int ai = 6; ai < argc; ai++) if (strncmp(argv[ai], "wav:", 4) == 0) wavpath = argv[ai] + 4;
    FILE* wav = NULL; unsigned wavrate = 48000, wavch = 2;

    HMODULE dll = LoadLibraryA("Bink2Scarlett.dll");
    if (!dll) { printf("load fail %u\n", GetLastError()); return 1; }

    #define TSTR(name, var, ty) p##var = (ty)GetProcAddress(dll, name); if(!p##var){printf("missing %s\n",name); return 1;}
    TSTR("BinkOpen", Open, PFN_Open) TSTR("BinkClose", Close, PFN_Close) TSTR("BinkDoFrame", DoFrame, PFN_DoFrame)
    TSTR("BinkGetFrameBuffersInfo", GetFBI, PFN_GetFBI)
    TSTR("BinkRegisterFrameBuffers", RegFB, PFN_RegFB) TSTR("BinkGetSummary", Summary, PFN_Summary)
    TSTR("BinkNextFrame", NextFrame, PFN_NextFrame)
    TSTR("BinkGoto", Goto, PFN_Goto)
    TSTR("BinkSetSoundOnOff", SetSound, PFN_SetSound) TSTR("BinkWait", Wait, PFN_Wait)
    TSTR("BinkGetError", GetError, PFN_GetError)
    pAllocFB = (PFN_AllocFB)GetProcAddress(dll, "BinkAllocateFrameBuffers");
    pOpenXA = (PFN_OpenXA)GetProcAddress(dll, "BinkOpenXAudio2");
    pOpenTrack = (PFN_OpenTrack)GetProcAddress(dll, "BinkOpenTrack");
    pMaxSize = (PFN_MaxSize)GetProcAddress(dll, "BinkGetTrackMaxSize");
    pGetData = (PFN_GetData)GetProcAddress(dll, "BinkGetTrackData");

    /* override Win32 file dispatch with memory-backed IO (kills the EOF spin hang) */
    *(void**)((char*)dll + 0x35000) = (void*)my_open;
    *(void**)((char*)dll + 0x35008) = (void*)my_read;
    *(void**)((char*)dll + 0x35010) = (void*)my_seek;
    *(void**)((char*)dll + 0x35018) = (void*)my_close;

    HBINK h = (HBINK)guarded(pOpen, (void*)path, 0, 0,0,0,0,0, 8000, "BinkOpen");
    if (!h || h == (HBINK)-1) { printf("open failed: %s\n", pGetError ? pGetError() : "?"); return 1; }

    /* aud mode: XAudio2 singleton must exist before BinkOpenTrack (empirically;
       without it OpenTrack crashes). Sound output itself stays off via SetSound0. */
    void* audsys = NULL;
    if (aud_mode && pOpenXA) {
        audsys = (void*)guarded(pOpenXA, (void*)"default", (void*)(size_t)0,0,0,0,0,0, 3000, "BinkOpenXAudio2");
        if (!audsys) { fprintf(stderr, "NO XAUDIO2 SYSTEM, audio disabled\n"); aud_mode = 0; }
    }
    guarded(pSetSound, h, 0, 0,0,0,0,0, 2000, "BinkSetSoundOnOff(h,0)");

    static FBInfo info;
    memset(&info, 0, sizeof(info));
    guarded(pGetFBI, h, &info, 0,0,0,0,0, 3000, "BinkGetFrameBuffersInfo");
    {
        int W0 = info.width, H0 = info.height;
        printf("%s: %u bufs %dx%d (uv %dx%d)\n", tag, info.num_buffers, W0, H0, info.width_uv, info.height_uv);
    }
    if (info.num_buffers == 0 || info.num_buffers > 3) return 1;

    int W = info.width, H = info.height, WU = info.width_uv, HU = info.height_uv;
    unsigned int strideY = (W + 63u) & ~63u;
    unsigned int strideU = (WU + 63u) & ~63u;

    /* "allocfb" flag: let the DLL allocate frame buffers natively (KB2j layout
       hypothesis: our hand-built PlaneDesc metadata triggers a different
       write path). Strides come from the returned descriptor. */
    int use_allocfb = 0;
    for (int ai = 5; ai < argc; ai++) if (strcmp(argv[ai], "allocfb") == 0) use_allocfb = 1;
    if (use_allocfb && pAllocFB) {
        guarded(pAllocFB, h, &info, 0,0,0,0,0, 3000, "BinkAllocateFrameBuffers");
        strideY = info.planes[0].stride; strideU = info.planes[0].ustride;
        fprintf(stderr, "ALLOCFB bufs=%u strideY=%u strideU=%u uptr?%d vptr?%d\n",
                info.num_buffers, strideY, strideU,
                info.planes[0].uptr != NULL, info.planes[0].vptr != NULL);
        fflush(stderr);
    } else {
    for (unsigned int b = 0; b < info.num_buffers; b++) {
        PlaneDesc* e = &info.planes[b];
        e->ptr  = VirtualAlloc(NULL, (SIZE_T)strideY * H, MEM_COMMIT, PAGE_READWRITE);
        e->flag = 1; e->stride = strideY;
        e->uptr = VirtualAlloc(NULL, (SIZE_T)strideU * HU, MEM_COMMIT, PAGE_READWRITE);
        e->uflag = 1; e->ustride = strideU;
        e->vptr = VirtualAlloc(NULL, (SIZE_T)strideU * HU, MEM_COMMIT, PAGE_READWRITE);
        e->vflag = 1; e->vstride = strideU;
        if (e->aflag) { e->aptr = VirtualAlloc(NULL, (SIZE_T)strideY * H, MEM_COMMIT, PAGE_READWRITE); e->astride = strideY; }
        if (e->a2flag) { e->a2ptr = VirtualAlloc(NULL, (SIZE_T)strideY * H, MEM_COMMIT, PAGE_READWRITE); e->a2stride = strideY; }
        if (!e->ptr || !e->uptr || !e->vptr) { printf("alloc fail\n"); return 1; }
    }
    }
    guarded(pRegFB, h, &info, 0,0,0,0,0, 3000, "BinkRegisterFrameBuffers");

    /* optional raw crop (after alloc/RegFB so buffers keep full padded size):
       argv[6] == "crop:WxH" — planes may be 16-edge-padded, e.g. portrait 720
       videos report 736 in fbinfo; write only the first W columns */
    if (raw_mode && argc > 6 && strncmp(argv[6], "crop:", 5) == 0) {
        int cw = 0, ch = 0;
        if (sscanf(argv[6] + 5, "%dx%d", &cw, &ch) == 2 && cw > 0 && ch > 0 && cw <= W && ch <= H) {
            W = cw; H = ch; WU = (cw + 1) / 2; HU = (ch + 1) / 2;
            fprintf(stderr, "CROP to %dx%d (uv %dx%d)\n", W, H, WU, HU);
        } else fprintf(stderr, "BAD CROP SPEC %s\n", argv[6]);
    }

    /* optional seek: argv[7] == "goto:N" (BinkGoto(h, frame, prevgoto=0)) */
    if (argc > 7 && strncmp(argv[7], "goto:", 5) == 0) {
        int gf = atoi(argv[7] + 5);
        if (gf > 0) {
            unsigned __int64 gr = guarded(pGoto, h, (void*)(size_t)gf, (void*)(size_t)0,0,0,0,0, 8000, "BinkGoto");
            fprintf(stderr, "GOTO %d rc=%lld\n", gf, (long long)gr);
        }
    }

    /* audio track setup (aud mode): table at h+0x1e8, count at h+0x38;
       entry: bit28=audio, bit29=stereo(+1 ch), low16=sample rate */
    unsigned ntrk = 0; void* tk[8]; unsigned tcap[8]; unsigned char* tbuf[8];
    unsigned long long wavbytes = 0;
    if (aud_mode && pOpenTrack && pGetData) {
        unsigned count = *(unsigned*)((char*)h + 0x38);
        unsigned* ttab = *(unsigned**)((char*)h + 0x1e8);
        for (unsigned i = 0; i < count && ntrk < 8; i++) {
            unsigned e = ttab[i];
            if (!((e >> 28) & 1)) continue;
            if (!pMaxSize || !pOpenTrack) break;
            void* t = pOpenTrack(h, i);
            if (!t) continue;
            unsigned mx = pMaxSize(h, i);
            tk[ntrk] = t; tcap[ntrk] = mx; tbuf[ntrk] = (unsigned char*)malloc(mx);
            unsigned rt = e & 0xFFFF, ch = ((e >> 29) & 1) + 1;
            fprintf(stderr, "AUD trk%u rate=%u ch=%u maxsize=%u\n", i, rt, ch, mx);
            if (!ntrk) { wavrate = rt; wavch = ch; }
            ntrk++;
        }
        wav = fopen(wavpath ? wavpath : "out.wav", "wb");
        if (wav && ntrk) {
            unsigned sr = wavrate, br = sr * wavch * 2;
            char hdr[44] = {'R','I','F','F',0,0,0,0,'W','A','V','E','f','m','t',' ',
                16,0,0,0,1,0,(char)wavch,0,(int)(sr&255),(int)((sr>>8)&255),(int)((sr>>16)&255),(int)((sr>>24)&255),
                (int)(br&255),(int)((br>>8)&255),(int)((br>>16)&255),(int)((br>>24)&255),
                (char)(wavch*2),0,16,0,'d','a','t','a',0,0,0,0};
            fwrite(hdr, 1, 44, wav);
        }
    }

    unsigned char* rgbbuf = (unsigned char*)malloc((size_t)W * H * 4);

    int okframes = 0;
    for (int fr = 0; fr < want_frames; fr++) {
        unsigned __int64 r = guarded(pDoFrame, h, 0,0,0,0,0,0, 10000, "BinkDoFrame");
        if (r == 0xDEADDEADDEADDEADULL) { printf("DoFrame timeout at frame %d\n", fr); break; }
        if (r != 0) { printf("DoFrame err=%lld at frame %d\n", (long long)r, fr); break; }

        unsigned b = info.cur_idx < 3 ? info.cur_idx : 0;
        PlaneDesc* e = &info.planes[b];
        if (aud_mode) {
            for (unsigned i = 0; i < ntrk; i++) {
                int n = pGetData(tk[i], tbuf[i], tcap[i]);
                if (n > 0 && wav) { fwrite(tbuf[i], 1, (size_t)n, wav); wavbytes += (unsigned)n; }
            }
            if (fr % 250 == 0 || fr + 1 == want_frames) {
                fprintf(stderr, "audframe %d bytes=%llu\n", fr, wavbytes);
                fflush(wav);
            }
            okframes++;
            if (fr + 1 < want_frames) {
                unsigned __int64 nr = guarded(pNextFrame, h, 0,0,0,0,0,0, 3000, "BinkNextFrame");
                if (nr == 0xDEADDEADDEADDEADULL) break;
                guarded(pWait, h, 0,0,0,0,0,0, 5000, "BinkWait");
            }
            continue;
        }
        if (raw_mode) {
            /* emit tightly-packed I420: W*H luma + WU*HU chroma U + V,
               copying row-by-row to drop the 64B-aligned stride padding */
            int bad = 0;
            for (int y = 0; y < H; y++)
                if (fwrite((unsigned char*)e->ptr + (size_t)y * strideY, 1, W, rawout) != (size_t)W) { bad = 1; break; }
            if (!bad) {
                /* decoder slot mapping: uptr holds V, vptr holds U (YV12 internal).
                   Emit I420 = Y + U(vptr) + V(uptr). */
                for (int y = 0; y < HU; y++)
                    if (fwrite((unsigned char*)e->vptr + (size_t)y * strideU, 1, WU, rawout) != (size_t)WU) { bad = 1; break; }
                if (!bad)
                    for (int y = 0; y < HU; y++)
                        if (fwrite((unsigned char*)e->uptr + (size_t)y * strideU, 1, WU, rawout) != (size_t)WU) { bad = 1; break; }
            }
            if (bad) { fprintf(stderr, "RAW WRITE FAIL at frame %d\n", fr); break; }
            fflush(rawout);
            if (fr % 250 == 0 || fr + 1 == want_frames)
                fprintf(stderr, "rawframe %d buf%u\n", fr, b);
            okframes++;
            if (fr + 1 < want_frames) {
                unsigned __int64 nr = guarded(pNextFrame, h, 0,0,0,0,0,0, 3000, "BinkNextFrame");
                if (nr == 0xDEADDEADDEADDEADULL) break;
                guarded(pWait, h, 0,0,0,0,0,0, 5000, "BinkWait");
            }
            continue;
        }
        double ys = plane_std((unsigned char*)e->ptr, (int)(strideY * H));
        for (int y = 0; y < H; y++) {
            const unsigned char* yl = (unsigned char*)e->ptr + (size_t)y * strideY;
            const unsigned char* ul = (unsigned char*)e->uptr + (size_t)(y>>1) * strideU;
            const unsigned char* vl = (unsigned char*)e->vptr + (size_t)(y>>1) * strideU;
            unsigned char* out = rgbbuf + (size_t)y * W * 4;
            for (int x = 0; x < W; x++) {
                double Yv = (double)yl[x];
                /* slot mapping fix (see top): uptr=V data, vptr=U data */
                double Uv = (double)vl[x>>1] - 128.0;
                double Vv = (double)ul[x>>1] - 128.0;
                double yy = 1.164383 * (Yv - 16.0);
                double rr = yy + 1.596027 * Vv;
                double gg = yy - 0.391762 * Uv - 0.812968 * Vv;
                double bb2 = yy + 2.017232 * Uv;
                out[x*4+0] = (unsigned char)(bb2<0?0:(bb2>255?255:bb2));
                out[x*4+1] = (unsigned char)(gg<0?0:(gg>255?255:gg));
                out[x*4+2] = (unsigned char)(rr<0?0:(rr>255?255:rr));
                out[x*4+3] = 255;
            }
        }
        char pth[MAX_PATH];
        snprintf(pth, sizeof pth, "%s\\%s_f%03d.bmp", outdir, tag, fr);
        FILE* f = fopen(pth, "wb");
        if (f) {
            int row = W*4;
            unsigned int datasz = (unsigned)row*H;
            unsigned int off = 54;
            unsigned int hdr[2] = { 54 + datasz, 0 };
            unsigned short resv=0;
            fwrite("BM",1,2,f); fwrite(hdr,4,1,f);
            fwrite(&resv,2,1,f); fwrite(&resv,2,1,f);
            fwrite(&off,4,1,f);
            unsigned int bi=40; fwrite(&bi,4,1,f);
            int ww=W, hh=-H; unsigned short one=1, bb=32;
            fwrite(&ww,4,1,f); fwrite(&hh,4,1,f);
            fwrite(&one,2,1,f); fwrite(&bb,2,1,f);
            unsigned int z=0; fwrite(&z,4,1,f);
            fwrite(&datasz,4,1,f);
            int ppm=2835; fwrite(&ppm,4,1,f); fwrite(&ppm,4,1,f);
            fwrite(&z,4,1,f); fwrite(&z,4,1,f);
            fwrite(rgbbuf,1,datasz,f);
            fclose(f);
        }
        printf("frame %d buf%u Ystd=%.1f -> %s\n", fr, b, ys, pth);
        fflush(stdout);
        okframes++;
        if (fr + 1 < want_frames) {
            unsigned __int64 nr = guarded(pNextFrame, h, 0,0,0,0,0,0, 3000, "BinkNextFrame");
            if (nr == 0xDEADDEADDEADDEADULL) break;
            guarded(pWait, h, 0,0,0,0,0,0, 5000, "BinkWait");
        }
    }
    if (wav) {
        if (ntrk) {
            unsigned dsz = (unsigned)(wavbytes > 0xFFFFFFFFull ? 0xFFFFFFFFu : wavbytes);
            unsigned rsz = dsz + 36;
            fseek(wav, 4, SEEK_SET); fwrite(&rsz, 4, 1, wav);
            fseek(wav, 40, SEEK_SET); fwrite(&dsz, 4, 1, wav);
        }
        fflush(wav); fclose(wav);
        fprintf(stderr, "WAV %s bytes=%llu rate=%u ch=%u\n", wavpath ? wavpath : "out.wav", wavbytes, wavrate, wavch);
    }
    guarded(pClose, h, 0,0,0,0,0,0, 3000, "BinkClose");
    if (rawout) { fflush(rawout); fclose(rawout); }
    fprintf(raw_mode ? stderr : stdout, "DONE %s okframes=%d reads=%ld\n", tag, okframes, g_read_count);
    return okframes > 0 ? 0 : 2;
}
