/*
 * libEGL_x2dadreno.c — GLVND EGL vendor that routes most calls to ANGLE
 * but intercepts eglCreatePlatformWindowSurface + eglSwapBuffers to
 * implement the X11-pbuffer + glReadPixels + XPutImage redirect path.
 *
 * Why a vendor instead of LD_PRELOAD: GLVND's libGLdispatch caches
 * function pointers from the vendor at first use, bypassing
 * LD_PRELOAD's symbol resolution. The only way to actually intercept
 * EGL calls under GLVND is to BE the vendor.
 *
 * Vendor ABI per `$PREFIX/include/glvnd/libeglabi.h`:
 *   - Export `__egl_Main(version, exports, vendor, imports) → EGLBoolean`
 *   - Fill in `imports->{getPlatformDisplay, getSupportsAPI,
 *     getProcAddress, getDispatchAddress, setDispatchIndex}`
 *   - Vendor's getProcAddress is the routing point: GLVND calls it
 *     with EGL function names and uses the returned pointer in its
 *     dispatch table.
 *
 * Routing logic in our getProcAddress:
 *   * "eglCreatePlatformWindowSurface" → our redirect that creates a
 *     pbuffer + maps the X11 XID to it
 *   * "eglSwapBuffers" → our redirect that does glReadPixels +
 *     XPutImage to the mapped X11 window
 *   * Everything else → dlsym from ANGLE's libEGL_angle.so
 *
 * Install:
 *   1. Build into `runtime/libEGL_x2dadreno.so`
 *   2. Drop `40_x2dadreno.json` into `$PREFIX/share/glvnd/egl_vendor.d/`
 *      (40_ prefix wins over Mesa's 50_ alphabetically — GLVND uses
 *      the FIRST vendor that returns a non-NULL display).
 *   3. Run BambuStudio with `X2D_USE_ADRENO=1`. GLVND will load our
 *      vendor before Mesa's, our getProcAddress hands out our
 *      intercepts, and the wxGLCanvas EGL calls flow through.
 */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "glvnd/libeglabi.h"

#ifndef GL_BGRA_EXT
#define GL_BGRA_EXT 0x80E1
#endif

__attribute__((constructor(99)))
static void vendor_loaded_announcement(void) {
    fprintf(stderr, "[x2d-vendor pid=%d] libEGL_x2dadreno.so loaded\n", (int)getpid());
    fflush(stderr);
}

/* ------------------------------------------------------------------
 * Surface mapping — same logic as preload_egl_x11.c. Per-pbuffer
 * record of the X11 XID we substituted for.
 * ------------------------------------------------------------------ */
typedef struct surface_map_entry {
    EGLSurface pbuf;
    Display *xdpy;
    Window xwin;
    int width;
    int height;
    GC gc;
    XImage *ximg;
    void *ximg_data;
    EGLConfig config;
    struct surface_map_entry *next;
} surface_map_entry_t;

static surface_map_entry_t *g_surface_map = NULL;
static pthread_mutex_t g_surface_lock = PTHREAD_MUTEX_INITIALIZER;
static Display *g_xdpy_default = NULL;

/* Lazily-resolved ANGLE function pointers. We pull them from
 * libEGL_angle.so via dlsym — that lib is in
 * `$PREFIX/opt/angle-android/vulkan/libEGL_angle.so`. */
static void *g_angle = NULL;
static void *g_angle_gl = NULL;

/* Intercepted-or-forwarded EGL signatures we need pointers for. */
static EGLDisplay (*angle_eglGetDisplay)(EGLNativeDisplayType) = NULL;
static EGLDisplay (*angle_eglGetPlatformDisplay)(EGLenum, void*, const EGLAttrib*) = NULL;
static EGLBoolean (*angle_eglInitialize)(EGLDisplay, EGLint*, EGLint*) = NULL;
static EGLBoolean (*angle_eglBindAPI)(EGLenum) = NULL;
static EGLSurface (*angle_eglCreatePbufferSurface)(EGLDisplay, EGLConfig, const EGLint*) = NULL;
static EGLSurface (*angle_eglCreatePlatformWindowSurface)(EGLDisplay, EGLConfig, void*, const EGLAttrib*) = NULL;
static EGLBoolean (*angle_eglDestroySurface)(EGLDisplay, EGLSurface) = NULL;
static EGLBoolean (*angle_eglSwapBuffers)(EGLDisplay, EGLSurface) = NULL;
static EGLBoolean (*angle_eglMakeCurrent)(EGLDisplay, EGLSurface, EGLSurface, EGLContext) = NULL;
static __eglMustCastToProperFunctionPointerType (*angle_eglGetProcAddress)(const char*) = NULL;
static EGLint    (*angle_eglGetError)(void) = NULL;

/* X11 + GLES funcs we need at swap time. */
static int (*real_XPutImage)(Display*, Drawable, GC, XImage*, int, int, int, int, unsigned int, unsigned int) = NULL;
static GC (*real_XCreateGC)(Display*, Drawable, unsigned long, XGCValues*) = NULL;
static int (*real_XGetGeometry)(Display*, Drawable, Window*, int*, int*, unsigned int*, unsigned int*, unsigned int*, unsigned int*) = NULL;
static XImage *(*real_XCreateImage)(Display*, Visual*, unsigned int, int, int, char*, unsigned int, unsigned int, int, int) = NULL;
static void (*real_glReadPixels)(GLint, GLint, GLsizei, GLsizei, GLenum, GLenum, void*) = NULL;

/* ------------------------------------------------------------------
 * Logging — debug to a file so we don't fight stderr redirection
 * by BS subprocesses.
 * ------------------------------------------------------------------ */
static FILE *log_fp(void) {
    static FILE *fp = NULL;
    static pthread_mutex_t log_lock = PTHREAD_MUTEX_INITIALIZER;
    if (fp) return fp;
    pthread_mutex_lock(&log_lock);
    if (!fp) {
        const char *t = getenv("TMPDIR");
        if (!t) t = "/tmp";
        char p[256];
        snprintf(p, sizeof(p), "%s/x2d_egl_vendor.log", t);
        fp = fopen(p, "a");
        if (!fp) fp = stderr;
        fprintf(fp, "\n[x2d-vendor pid=%d] open\n", (int)getpid());
        fflush(fp);
    }
    pthread_mutex_unlock(&log_lock);
    return fp;
}
#define LOG(...) do { \
    FILE *_f = log_fp(); \
    fprintf(_f, "[x2d-vendor pid=%d] ", (int)getpid()); \
    fprintf(_f, __VA_ARGS__); fputc('\n', _f); fflush(_f); \
} while (0)

/* ------------------------------------------------------------------
 * Helpers
 * ------------------------------------------------------------------ */
static void load_angle(void) {
    if (g_angle) return;
    const char *angle_dir = getenv("X2D_ANGLE_DIR");
    if (!angle_dir) angle_dir = "/data/data/com.termux/files/usr/opt/angle-android/vulkan";
    char path[512];
    snprintf(path, sizeof(path), "%s/libEGL_angle.so", angle_dir);
    g_angle = dlopen(path, RTLD_NOW | RTLD_GLOBAL);
    if (!g_angle) {
        LOG("dlopen ANGLE EGL failed: %s", dlerror());
        return;
    }
    snprintf(path, sizeof(path), "%s/libGLESv2_angle.so", angle_dir);
    g_angle_gl = dlopen(path, RTLD_NOW | RTLD_GLOBAL);
    if (!g_angle_gl) {
        LOG("dlopen ANGLE GLES failed: %s", dlerror());
    }
    LOG("ANGLE loaded: EGL=%p GLES=%p", g_angle, g_angle_gl);

    angle_eglGetDisplay = dlsym(g_angle, "eglGetDisplay");
    angle_eglGetPlatformDisplay = dlsym(g_angle, "eglGetPlatformDisplay");
    angle_eglInitialize = dlsym(g_angle, "eglInitialize");
    angle_eglBindAPI = dlsym(g_angle, "eglBindAPI");
    angle_eglCreatePbufferSurface = dlsym(g_angle, "eglCreatePbufferSurface");
    angle_eglCreatePlatformWindowSurface = dlsym(g_angle, "eglCreatePlatformWindowSurface");
    angle_eglDestroySurface = dlsym(g_angle, "eglDestroySurface");
    angle_eglSwapBuffers = dlsym(g_angle, "eglSwapBuffers");
    angle_eglMakeCurrent = dlsym(g_angle, "eglMakeCurrent");
    angle_eglGetProcAddress = dlsym(g_angle, "eglGetProcAddress");
    angle_eglGetError = dlsym(g_angle, "eglGetError");

    /* GLES from libGLESv2_angle.so for glReadPixels at swap time. */
    if (g_angle_gl) {
        real_glReadPixels = dlsym(g_angle_gl, "glReadPixels");
    }

    /* X11 — RTLD_NEXT works here since libX11 is always in the link
     * chain when we get loaded as a vendor under libGLdispatch. */
    void *x11 = dlopen("libX11.so.6", RTLD_NOW | RTLD_GLOBAL);
    if (!x11) x11 = dlopen("libX11.so", RTLD_NOW | RTLD_GLOBAL);
    if (x11) {
        real_XPutImage = dlsym(x11, "XPutImage");
        real_XCreateGC = dlsym(x11, "XCreateGC");
        real_XGetGeometry = dlsym(x11, "XGetGeometry");
        real_XCreateImage = dlsym(x11, "XCreateImage");
    }
    LOG("X11 funcs: XPut=%p XCreateGC=%p XGetGeo=%p XCreateImage=%p",
        real_XPutImage, real_XCreateGC, real_XGetGeometry, real_XCreateImage);
}

static surface_map_entry_t *find_entry_locked(EGLSurface pbuf) {
    surface_map_entry_t *e = g_surface_map;
    while (e) { if (e->pbuf == pbuf) return e; e = e->next; }
    return NULL;
}

static void free_ximage(surface_map_entry_t *e) {
    if (e->ximg) { e->ximg->data = NULL; XDestroyImage(e->ximg); e->ximg = NULL; }
    free(e->ximg_data);
    e->ximg_data = NULL;
}

static int ensure_ximage(surface_map_entry_t *e) {
    if (e->ximg && e->ximg->width == e->width && e->ximg->height == e->height)
        return 0;
    free_ximage(e);
    size_t sz = (size_t)e->width * (size_t)e->height * 4;
    e->ximg_data = malloc(sz);
    if (!e->ximg_data) return -1;
    Visual *v = DefaultVisual(e->xdpy, DefaultScreen(e->xdpy));
    e->ximg = real_XCreateImage(e->xdpy, v, 24, ZPixmap, 0,
                                (char*)e->ximg_data,
                                e->width, e->height, 32, 0);
    if (!e->ximg) { free(e->ximg_data); e->ximg_data = NULL; return -1; }
    return 0;
}

/* ------------------------------------------------------------------
 * Our intercepts — implementations that GLVND will see via getProcAddress
 * ------------------------------------------------------------------ */
static EGLSurface my_eglCreatePlatformWindowSurface(EGLDisplay dpy, EGLConfig config,
                                                    void *native_window,
                                                    const EGLAttrib *attrib_list) {
    load_angle();
    if (!native_window || !g_angle || !angle_eglCreatePbufferSurface) {
        LOG("eglCreatePlatformWindowSurface: not initialised, falling back");
        if (angle_eglCreatePlatformWindowSurface)
            return angle_eglCreatePlatformWindowSurface(dpy, config, native_window, attrib_list);
        return EGL_NO_SURFACE;
    }
    Window xwin = *(Window*)native_window;
    LOG("eglCreatePlatformWindowSurface(xwin=0x%lx)", xwin);

    if (!g_xdpy_default) g_xdpy_default = XOpenDisplay(NULL);
    Display *xdpy = g_xdpy_default;
    if (!xdpy) { LOG("  no X11 dpy"); return EGL_NO_SURFACE; }

    Window root; int x, y; unsigned int w, h, bw, depth;
    if (!real_XGetGeometry || real_XGetGeometry(xdpy, xwin, &root, &x, &y, &w, &h, &bw, &depth) == 0) {
        LOG("  XGetGeometry failed");
        return EGL_NO_SURFACE;
    }
    if (w == 0 || h == 0) { w = 800; h = 600; }
    EGLint pbuf_attribs[] = {
        EGL_WIDTH,  (EGLint)w,
        EGL_HEIGHT, (EGLint)h,
        EGL_TEXTURE_TARGET, EGL_NO_TEXTURE,
        EGL_TEXTURE_FORMAT, EGL_NO_TEXTURE,
        EGL_NONE,
    };
    EGLSurface pbuf = angle_eglCreatePbufferSurface(dpy, config, pbuf_attribs);
    if (pbuf == EGL_NO_SURFACE) {
        LOG("  CreatePbuffer failed (egl err 0x%x)",
            angle_eglGetError ? angle_eglGetError() : 0);
        return EGL_NO_SURFACE;
    }
    surface_map_entry_t *e = calloc(1, sizeof(*e));
    e->pbuf = pbuf; e->xdpy = xdpy; e->xwin = xwin;
    e->width = (int)w; e->height = (int)h; e->config = config;
    pthread_mutex_lock(&g_surface_lock);
    e->next = g_surface_map; g_surface_map = e;
    pthread_mutex_unlock(&g_surface_lock);
    LOG("  registered pbuf=%p ↔ xwin=0x%lx (%dx%d)", pbuf, xwin, e->width, e->height);
    return pbuf;
}

/* Wrap eglInitialize to debug what ANGLE returns inside libGLdispatch's dispatch
 * (vs. when called directly — direct works, dispatched fails with EGL_BAD_DISPLAY). */
static EGLBoolean my_eglInitialize(EGLDisplay dpy, EGLint *major, EGLint *minor) {
    load_angle();
    if (!angle_eglInitialize) { LOG("eglInitialize: ANGLE not loaded"); return EGL_FALSE; }
    EGLint pre_err = angle_eglGetError ? angle_eglGetError() : 0;
    EGLBoolean rc = angle_eglInitialize(dpy, major, minor);
    EGLint post_err = angle_eglGetError ? angle_eglGetError() : 0;
    LOG("eglInitialize(dpy=%p) = %d, ver=%d.%d, pre_err=0x%x post_err=0x%x",
        dpy, (int)rc, major ? *major : -1, minor ? *minor : -1, pre_err, post_err);
    return rc;
}

static const char *my_eglQueryString(EGLDisplay dpy, EGLint name) {
    load_angle();
    const char *(*f)(EGLDisplay, EGLint) = dlsym(g_angle, "eglQueryString");
    const char *r = f ? f(dpy, name) : NULL;
    LOG("eglQueryString(dpy=%p, name=0x%x) = %s (err 0x%x)",
        dpy, name, r ? r : "(null)", angle_eglGetError ? angle_eglGetError() : 0);
    return r;
}

static EGLBoolean my_eglChooseConfig(EGLDisplay dpy, const EGLint *attrib_list,
                                      EGLConfig *configs, EGLint config_size,
                                      EGLint *num_config) {
    load_angle();
    EGLBoolean (*f)(EGLDisplay, const EGLint*, EGLConfig*, EGLint, EGLint*) =
        dlsym(g_angle, "eglChooseConfig");
    EGLBoolean r = f ? f(dpy, attrib_list, configs, config_size, num_config) : EGL_FALSE;
    LOG("eglChooseConfig(dpy=%p) = %d, nc=%d (err 0x%x)",
        dpy, (int)r, num_config ? *num_config : -1,
        angle_eglGetError ? angle_eglGetError() : 0);
    return r;
}

static EGLBoolean my_eglBindAPI(EGLenum api) {
    load_angle();
    EGLBoolean rc = angle_eglBindAPI ? angle_eglBindAPI(api) : EGL_FALSE;
    LOG("eglBindAPI(0x%x) = %d (err 0x%x)", api, (int)rc,
        angle_eglGetError ? angle_eglGetError() : 0);
    return rc;
}

static EGLSurface my_eglCreatePbufferSurface(EGLDisplay dpy, EGLConfig config,
                                              const EGLint *attrib_list) {
    load_angle();
    EGLSurface s = angle_eglCreatePbufferSurface ?
        angle_eglCreatePbufferSurface(dpy, config, attrib_list) : EGL_NO_SURFACE;
    LOG("eglCreatePbufferSurface(dpy=%p, cfg=%p) = %p (err 0x%x)",
        dpy, config, s, angle_eglGetError ? angle_eglGetError() : 0);
    return s;
}

/* Wrap eglMakeCurrent + eglCreateContext for diagnostics */
static EGLContext my_eglCreateContext(EGLDisplay dpy, EGLConfig config,
                                       EGLContext share, const EGLint *attribs) {
    load_angle();
    EGLContext (*f)(EGLDisplay, EGLConfig, EGLContext, const EGLint*) =
        dlsym(g_angle, "eglCreateContext");
    if (!f) return EGL_NO_CONTEXT;
    EGLContext c = f(dpy, config, share, attribs);
    LOG("eglCreateContext(dpy=%p, cfg=%p) = %p (err 0x%x)", dpy, config, c,
        angle_eglGetError ? angle_eglGetError() : 0);
    return c;
}

static EGLBoolean my_eglMakeCurrent(EGLDisplay dpy, EGLSurface draw,
                                     EGLSurface read, EGLContext ctx) {
    load_angle();
    EGLBoolean rc = angle_eglMakeCurrent ? angle_eglMakeCurrent(dpy, draw, read, ctx) : EGL_FALSE;
    LOG("eglMakeCurrent(dpy=%p, draw=%p, read=%p, ctx=%p) = %d (err 0x%x)",
        dpy, draw, read, ctx, (int)rc, angle_eglGetError ? angle_eglGetError() : 0);
    return rc;
}

/* ----- Generic pass-through wrappers for the rest of EGL 1.5 ------------
 * Why each function needs its own wrapper instead of returning ANGLE's
 * pointer directly from getProcAddress: libglvnd's static dispatch via
 * ANGLE function pointers fails for many EGL functions with
 * EGL_BAD_DISPLAY at call time (see Lesson 2 in IMPROVEMENTS #95). The
 * fix is to have the call site live in OUR library — bionic's namespace
 * resolution behaves differently for cross-library function-pointer calls.
 *
 * The macro below generates a static wrapper named my_<func> that
 * dlsym's ANGLE at first use and forwards. No logging in the hot path. */
#define ANGLE_FWD0(NAME, RET) \
    static RET my_##NAME(void) { \
        static RET (*f)(void) = NULL; \
        if (!f) { load_angle(); f = dlsym(g_angle, #NAME); } \
        return f ? f() : (RET)0; \
    }
#define ANGLE_FWD1(NAME, RET, T1) \
    static RET my_##NAME(T1 a) { \
        static RET (*f)(T1) = NULL; \
        if (!f) { load_angle(); f = dlsym(g_angle, #NAME); } \
        return f ? f(a) : (RET)0; \
    }
#define ANGLE_FWD2(NAME, RET, T1, T2) \
    static RET my_##NAME(T1 a, T2 b) { \
        static RET (*f)(T1, T2) = NULL; \
        if (!f) { load_angle(); f = dlsym(g_angle, #NAME); } \
        return f ? f(a, b) : (RET)0; \
    }
#define ANGLE_FWD3(NAME, RET, T1, T2, T3) \
    static RET my_##NAME(T1 a, T2 b, T3 c) { \
        static RET (*f)(T1, T2, T3) = NULL; \
        if (!f) { load_angle(); f = dlsym(g_angle, #NAME); } \
        return f ? f(a, b, c) : (RET)0; \
    }
#define ANGLE_FWD4(NAME, RET, T1, T2, T3, T4) \
    static RET my_##NAME(T1 a, T2 b, T3 c, T4 d) { \
        static RET (*f)(T1, T2, T3, T4) = NULL; \
        if (!f) { load_angle(); f = dlsym(g_angle, #NAME); } \
        return f ? f(a, b, c, d) : (RET)0; \
    }

ANGLE_FWD1(eglTerminate,           EGLBoolean, EGLDisplay)
ANGLE_FWD0(eglGetError,            EGLint)
ANGLE_FWD0(eglGetCurrentDisplay,   EGLDisplay)
ANGLE_FWD0(eglGetCurrentContext,   EGLContext)
ANGLE_FWD1(eglGetCurrentSurface,   EGLSurface, EGLint)
ANGLE_FWD0(eglWaitClient,          EGLBoolean)
ANGLE_FWD0(eglWaitGL,              EGLBoolean)
ANGLE_FWD1(eglWaitNative,          EGLBoolean, EGLint)
ANGLE_FWD0(eglReleaseThread,       EGLBoolean)
ANGLE_FWD0(eglQueryAPI,            EGLenum)
ANGLE_FWD2(eglDestroyContext,      EGLBoolean, EGLDisplay, EGLContext)
ANGLE_FWD2(eglSwapInterval,        EGLBoolean, EGLDisplay, EGLint)
ANGLE_FWD2(eglCopyBuffers,         EGLBoolean, EGLDisplay, EGLSurface)
ANGLE_FWD4(eglGetConfigs,          EGLBoolean, EGLDisplay, EGLConfig*, EGLint, EGLint*)
ANGLE_FWD4(eglGetConfigAttrib,     EGLBoolean, EGLDisplay, EGLConfig, EGLint, EGLint*)
ANGLE_FWD4(eglQuerySurface,        EGLBoolean, EGLDisplay, EGLSurface, EGLint, EGLint*)
ANGLE_FWD4(eglQueryContext,        EGLBoolean, EGLDisplay, EGLContext, EGLint, EGLint*)
ANGLE_FWD4(eglSurfaceAttrib,       EGLBoolean, EGLDisplay, EGLSurface, EGLint, EGLint)
ANGLE_FWD3(eglBindTexImage,        EGLBoolean, EGLDisplay, EGLSurface, EGLint)
ANGLE_FWD3(eglReleaseTexImage,     EGLBoolean, EGLDisplay, EGLSurface, EGLint)
ANGLE_FWD2(eglDestroyImage,        EGLBoolean, EGLDisplay, EGLImage)
ANGLE_FWD2(eglDestroyImageKHR,     EGLBoolean, EGLDisplay, EGLImage)
ANGLE_FWD2(eglDestroySync,         EGLBoolean, EGLDisplay, EGLSync)
ANGLE_FWD2(eglDestroySyncKHR,      EGLBoolean, EGLDisplay, EGLSync)
ANGLE_FWD3(eglCreateWindowSurface, EGLSurface, EGLDisplay, EGLConfig, EGLNativeWindowType)
ANGLE_FWD3(eglCreatePixmapSurface, EGLSurface, EGLDisplay, EGLConfig, EGLNativePixmapType)
ANGLE_FWD3(eglCreateImage,         EGLImage,   EGLDisplay, EGLContext, EGLenum)
ANGLE_FWD3(eglCreateImageKHR,      EGLImage,   EGLDisplay, EGLContext, EGLenum)
ANGLE_FWD3(eglCreateSync,          EGLSync,    EGLDisplay, EGLenum, const EGLAttrib*)
ANGLE_FWD3(eglCreateSyncKHR,       EGLSync,    EGLDisplay, EGLenum, const EGLint*)
ANGLE_FWD4(eglClientWaitSync,      EGLint,     EGLDisplay, EGLSync, EGLint, EGLTime)
ANGLE_FWD4(eglClientWaitSyncKHR,   EGLint,     EGLDisplay, EGLSync, EGLint, EGLTimeKHR)
ANGLE_FWD4(eglGetSyncAttrib,       EGLBoolean, EGLDisplay, EGLSync, EGLint, EGLAttrib*)

static EGLBoolean my_eglDestroySurface(EGLDisplay dpy, EGLSurface surface) {
    load_angle();
    pthread_mutex_lock(&g_surface_lock);
    surface_map_entry_t **pp = &g_surface_map;
    while (*pp) {
        if ((*pp)->pbuf == surface) {
            surface_map_entry_t *dead = *pp;
            *pp = dead->next;
            free_ximage(dead);
            if (dead->gc && dead->xdpy) XFreeGC(dead->xdpy, dead->gc);
            free(dead);
            LOG("eglDestroySurface: unmapped pbuf=%p", surface);
            break;
        }
        pp = &(*pp)->next;
    }
    pthread_mutex_unlock(&g_surface_lock);
    return angle_eglDestroySurface ? angle_eglDestroySurface(dpy, surface) : EGL_TRUE;
}

static EGLBoolean my_eglSwapBuffers(EGLDisplay dpy, EGLSurface surface) {
    load_angle();
    pthread_mutex_lock(&g_surface_lock);
    surface_map_entry_t *e = find_entry_locked(surface);
    pthread_mutex_unlock(&g_surface_lock);
    if (!e) {
        return angle_eglSwapBuffers ? angle_eglSwapBuffers(dpy, surface) : EGL_TRUE;
    }
    Window root; int xx, yy; unsigned int w, h, bw, depth;
    if (real_XGetGeometry(e->xdpy, e->xwin, &root, &xx, &yy, &w, &h, &bw, &depth) == 0) {
        LOG("swap: XGetGeometry failed");
        return EGL_TRUE;
    }
    if ((int)w != e->width || (int)h != e->height) {
        e->width = (int)w; e->height = (int)h;
        free_ximage(e);
    }
    if (ensure_ximage(e) < 0) { LOG("swap: ensure_ximage failed"); return EGL_TRUE; }
    if (!e->gc) {
        e->gc = real_XCreateGC(e->xdpy, e->xwin, 0, NULL);
        if (!e->gc) return EGL_TRUE;
    }
    if (!real_glReadPixels) { LOG("swap: glReadPixels NULL"); return EGL_TRUE; }
    real_glReadPixels(0, 0, e->width, e->height, GL_RGBA, GL_UNSIGNED_BYTE, e->ximg_data);
    /* RGBA → BGRA + Y-flip in one pass. */
    int row_bytes = e->width * 4;
    char *tmp = malloc(row_bytes);
    if (tmp) {
        for (int row = 0; row < e->height / 2; row++) {
            char *top = (char*)e->ximg_data + row * row_bytes;
            char *bot = (char*)e->ximg_data + (e->height - 1 - row) * row_bytes;
            memcpy(tmp, top, row_bytes);
            memcpy(top, bot, row_bytes);
            memcpy(bot, tmp, row_bytes);
        }
        free(tmp);
    }
    uint32_t *pp = (uint32_t*)e->ximg_data;
    size_t n = (size_t)e->width * (size_t)e->height;
    for (size_t i = 0; i < n; i++) {
        uint32_t v = pp[i];
        pp[i] = (v & 0xFF00FF00u)
              | ((v & 0x00FF0000u) >> 16)
              | ((v & 0x000000FFu) << 16);
    }
    real_XPutImage(e->xdpy, e->xwin, e->gc, e->ximg, 0, 0, 0, 0, e->width, e->height);
    XFlush(e->xdpy);
    return EGL_TRUE;
}

/* ------------------------------------------------------------------
 * Vendor ABI implementation
 * ------------------------------------------------------------------ */
/* ANGLE platform constants from $PREFIX/include/EGL/eglext_angle.h.
 * Defined as macros so we don't need that header in the build. */
#ifndef EGL_PLATFORM_ANGLE_ANGLE
#define EGL_PLATFORM_ANGLE_ANGLE             0x3202
#endif
#ifndef EGL_PLATFORM_ANGLE_TYPE_ANGLE
#define EGL_PLATFORM_ANGLE_TYPE_ANGLE        0x3203
#endif
#ifndef EGL_PLATFORM_ANGLE_TYPE_VULKAN_ANGLE
#define EGL_PLATFORM_ANGLE_TYPE_VULKAN_ANGLE 0x3450
#endif

static EGLDisplay my_getPlatformDisplay(EGLenum platform, void *nativeDisplay,
                                         const EGLAttrib *attrib_list) {
    (void)attrib_list;
    load_angle();
    LOG("getPlatformDisplay platform=0x%x native=%p", platform, nativeDisplay);

    /* Capture the X11 Display* so swap can use it later. ANGLE doesn't
     * understand X11 Display* handles — they're meaningful only to us. */
    if ((platform == EGL_PLATFORM_X11_KHR || platform == EGL_PLATFORM_X11_EXT)
        && nativeDisplay && nativeDisplay != EGL_DEFAULT_DISPLAY) {
        g_xdpy_default = (Display*)nativeDisplay;
        LOG("  captured X11 dpy=%p for swap path", g_xdpy_default);
    }

    /* CRUCIAL: ANGLE on Android only accepts EGL_PLATFORM_ANGLE_ANGLE and
     * EGL_DEFAULT_DISPLAY. Calling angle_eglGetDisplay with an X11 Display*
     * returns a poisoned display that fails eglInitialize with
     * EGL_BAD_DISPLAY. Always force the ANGLE-Vulkan platform. */
    EGLAttrib angle_attribs[] = {
        EGL_PLATFORM_ANGLE_TYPE_ANGLE,
        EGL_PLATFORM_ANGLE_TYPE_VULKAN_ANGLE,
        EGL_NONE,
    };
    EGLDisplay r = EGL_NO_DISPLAY;
    if (angle_eglGetPlatformDisplay) {
        r = angle_eglGetPlatformDisplay(EGL_PLATFORM_ANGLE_ANGLE,
                                         EGL_DEFAULT_DISPLAY, angle_attribs);
        LOG("  ANGLE GetPlatformDisplay(ANGLE_ANGLE, default, Vulkan) = %p (err 0x%x)",
            r, angle_eglGetError ? angle_eglGetError() : 0);
    }
    /* If GetPlatformDisplay isn't available, fall back to plain GetDisplay
     * with EGL_DEFAULT_DISPLAY (NOT the X11 dpy — see comment above). */
    if (r == EGL_NO_DISPLAY && angle_eglGetDisplay) {
        r = angle_eglGetDisplay(EGL_DEFAULT_DISPLAY);
        LOG("  ANGLE GetDisplay(default) fallback = %p", r);
    }
    return r;
}

static EGLBoolean my_getSupportsAPI(EGLenum api) {
    return (api == EGL_OPENGL_ES_API || api == EGL_OPENGL_API) ? EGL_TRUE : EGL_FALSE;
}

static const char *my_getVendorString(int name) {
    if (name == __EGL_VENDOR_STRING_PLATFORM_EXTENSIONS) {
        return "EGL_KHR_platform_x11 EGL_EXT_platform_x11";
    }
    return NULL;
}

static void *my_getProcAddress(const char *procName) {
    if (!procName) return NULL;
    /* Our intercepts win for these specific names */
    if (!strcmp(procName, "eglCreatePlatformWindowSurface"))
        return (void*)my_eglCreatePlatformWindowSurface;
    if (!strcmp(procName, "eglCreatePlatformWindowSurfaceEXT"))
        return (void*)my_eglCreatePlatformWindowSurface;
    if (!strcmp(procName, "eglSwapBuffers"))
        return (void*)my_eglSwapBuffers;
    if (!strcmp(procName, "eglDestroySurface"))
        return (void*)my_eglDestroySurface;
    if (!strcmp(procName, "eglInitialize"))
        return (void*)my_eglInitialize;
    if (!strcmp(procName, "eglCreateContext"))
        return (void*)my_eglCreateContext;
    if (!strcmp(procName, "eglMakeCurrent"))
        return (void*)my_eglMakeCurrent;
    if (!strcmp(procName, "eglQueryString"))
        return (void*)my_eglQueryString;
    if (!strcmp(procName, "eglChooseConfig"))
        return (void*)my_eglChooseConfig;
    if (!strcmp(procName, "eglBindAPI"))
        return (void*)my_eglBindAPI;
    if (!strcmp(procName, "eglCreatePbufferSurface"))
        return (void*)my_eglCreatePbufferSurface;
    /* Generic forwarders — see ANGLE_FWD* table above. */
    #define R(name) if (!strcmp(procName, #name)) return (void*)my_##name;
    R(eglTerminate)             R(eglGetError)
    R(eglGetCurrentDisplay)     R(eglGetCurrentContext)
    R(eglGetCurrentSurface)
    R(eglWaitClient)            R(eglWaitGL)             R(eglWaitNative)
    R(eglReleaseThread)         R(eglQueryAPI)
    R(eglDestroyContext)        R(eglSwapInterval)       R(eglCopyBuffers)
    R(eglGetConfigs)            R(eglGetConfigAttrib)
    R(eglQuerySurface)          R(eglQueryContext)       R(eglSurfaceAttrib)
    R(eglBindTexImage)          R(eglReleaseTexImage)
    R(eglDestroyImage)          R(eglDestroyImageKHR)
    R(eglDestroySync)           R(eglDestroySyncKHR)
    R(eglCreateWindowSurface)   R(eglCreatePixmapSurface)
    R(eglCreateImage)           R(eglCreateImageKHR)
    R(eglCreateSync)            R(eglCreateSyncKHR)
    R(eglClientWaitSync)        R(eglClientWaitSyncKHR)
    R(eglGetSyncAttrib)
    #undef R
    /* Everything else → ANGLE */
    load_angle();
    if (!g_angle) {
        LOG("getProcAddress(%s) → NULL (ANGLE not loaded)", procName);
        return NULL;
    }
    /* GL functions live in libGLESv2_angle.so, EGL functions in libEGL_angle.so.
     * Search both before falling back to ANGLE's eglGetProcAddress. */
    void *p = NULL;
    if (g_angle_gl) p = dlsym(g_angle_gl, procName);
    if (!p) p = dlsym(g_angle, procName);
    if (p) {
        if (getenv("X2D_VENDOR_TRACE"))
            LOG("getProcAddress(%s) → %p (dlsym)", procName, p);
        return p;
    }
    /* Fallback: ANGLE's own getProcAddress for extensions */
    if (angle_eglGetProcAddress) {
        p = (void*)angle_eglGetProcAddress(procName);
        if (p && getenv("X2D_VENDOR_TRACE"))
            LOG("getProcAddress(%s) → %p (ANGLE GPA)", procName, p);
        if (!p)
            LOG("getProcAddress(%s) → NULL (ANGLE GPA returned NULL)", procName);
        return p;
    }
    LOG("getProcAddress(%s) → NULL (no resolver)", procName);
    return NULL;
}

static void *my_getDispatchAddress(const char *procName) {
    /* For dispatched extensions — none we need to intercept here. */
    (void)procName;
    return NULL;
}

static void my_setDispatchIndex(const char *procName, int index) {
    (void)procName; (void)index;
}

static EGLenum my_findNativeDisplayPlatform(void *native_display) {
    /* termux-x11: only X11 Display ptrs come through eglGetDisplay. */
    if (!native_display || native_display == EGL_DEFAULT_DISPLAY)
        return EGL_NONE;
    return EGL_PLATFORM_X11_KHR;
}

/* ------------------------------------------------------------------
 * The vendor entry point — GLVND calls this to handshake.
 * ------------------------------------------------------------------ */
__attribute__((visibility("default")))
EGLBoolean __egl_Main(uint32_t version, const __EGLapiExports *exports,
                       __EGLvendorInfo *vendor, __EGLapiImports *imports) {
    (void)exports; (void)vendor;
    LOG("__egl_Main version=0x%08x major=%u minor=%u",
        version,
        EGL_VENDOR_ABI_GET_MAJOR_VERSION(version),
        EGL_VENDOR_ABI_GET_MINOR_VERSION(version));
    if (EGL_VENDOR_ABI_GET_MAJOR_VERSION(version) != EGL_VENDOR_ABI_MAJOR_VERSION) {
        LOG("  abi major mismatch (we want %u)", EGL_VENDOR_ABI_MAJOR_VERSION);
        return EGL_FALSE;
    }
    if (!imports) return EGL_FALSE;

    imports->getPlatformDisplay      = my_getPlatformDisplay;
    imports->getSupportsAPI          = my_getSupportsAPI;
    imports->getVendorString         = my_getVendorString;
    imports->getProcAddress          = my_getProcAddress;
    imports->getDispatchAddress      = my_getDispatchAddress;
    imports->setDispatchIndex        = my_setDispatchIndex;
    imports->findNativeDisplayPlatform = my_findNativeDisplayPlatform;
    /* Optional patch entry points left NULL */
    imports->isPatchSupported  = NULL;
    imports->initiatePatch     = NULL;
    imports->releasePatch      = NULL;
    imports->patchThreadAttach = NULL;
    return EGL_TRUE;
}
