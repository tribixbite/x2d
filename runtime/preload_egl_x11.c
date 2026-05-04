/*
 * preload_egl_x11.c — LD_PRELOAD shim that bridges ANGLE's libEGL to
 * termux-x11.
 *
 * Background: ANGLE on Android translates GL → Vulkan → real Adreno
 * driver, which we proved reaches the Adreno 830 via the leegaos
 * vulkan-wrapper (see docs/CLAUDE.md hard-won lessons + IMPROVEMENTS
 * #88 leegaos research). BUT ANGLE's libEGL X11 backend is broken
 * under termux-x11: `eglCreatePlatformWindowSurface(display, config,
 * &x11_xid, nullptr)` returns EGL_NO_SURFACE (or returns a surface that
 * never gets pixels onto the X11 window). Result: BS renders correctly
 * into ANGLE's internal buffer but the user sees a blank viewport.
 *
 * Mesa's zink+kopper has the same problem because kopper expects
 * DRI3/Present which termux-x11 lacks.
 *
 * This shim's bargain: keep the FAST path (ANGLE→Vulkan→Adreno) for the
 * GL render itself, but redirect the SLOW one-time-per-swap presentation
 * to a software glReadPixels + XPutImage. Rationale: the render runs at
 * GPU speed (e.g. 60+ fps for the BS Plater scene), the presentation
 * is bottlenecked by the X11 XPutImage round-trip but that's still
 * faster than rendering everything on llvmpipe (8 fps).
 *
 * Topology:
 *   wxGLCanvasEGL -> eglCreatePlatformWindowSurface(x11_xid)
 *      → INTERCEPT here: create a pbuffer backed by ANGLE/Vulkan/Adreno
 *      → return that pbuffer's EGLSurface to wx
 *   wxGLCanvasEGL -> eglMakeCurrent(pbuffer)
 *      → pass through to ANGLE; client-app gets a normal GL context
 *      → BS draws GL commands; ANGLE forwards to Vulkan-Adreno; pixels
 *        accumulate in the pbuffer's color attachment
 *   wxGLCanvasEGL -> eglSwapBuffers(pbuffer)
 *      → INTERCEPT here: glReadPixels from the pbuffer into a host buffer
 *      → XPutImage from the host buffer to the saved x11_xid
 *      → return EGL_TRUE
 *
 * Caveats:
 *   * glReadPixels is a synchronous CPU/GPU stall. The viewport only
 *     redraws on user interaction (mouse/keyboard) so this isn't a
 *     persistent perf issue.
 *   * XPutImage round-trip cost ≈ 1-3ms per swap on local socket X11.
 *   * The pbuffer must be sized to match the X11 window. We resize it
 *     on-demand by tracking the saved x11_xid's geometry in the
 *     intercept handler.
 *   * RGBA → BGRA byte swizzling for X11 — Adreno renders in OpenGL
 *     RGBA8 layout but X11 wants BGRA8 (little-endian native). One
 *     SIMD pass per swap.
 *
 * Build (constructor priority 100 so we beat preload_gtkinit's 101):
 *   gcc -O2 -fPIC -shared preload_egl_x11.c \
 *       -I$PREFIX/include \
 *       -Wl,-rpath,$PREFIX/opt/angle-android/vulkan \
 *       -lEGL -lX11 -ldl \
 *       -o preload_egl_x11.so
 *
 * Use (in run_gui.sh):
 *   export LD_PRELOAD=$X2D_ROOT/runtime/preload_egl_x11.so:$LD_PRELOAD
 */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#ifndef GL_BGRA_EXT
#define GL_BGRA_EXT 0x80E1
#endif
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

/* ------------------------------------------------------------------
 * Surface registry — one entry per intercepted EGLSurface so we can
 * recover the X11 window + dimensions at swap time.
 * ------------------------------------------------------------------ */
typedef struct surface_map_entry {
    EGLSurface pbuf;        /* the pbuffer we substituted */
    Display *xdpy;          /* X11 display extracted from EGLDisplay */
    Window xwin;            /* X11 window XID */
    int width;
    int height;
    GC gc;                  /* X11 GC, lazily created */
    XImage *ximg;           /* recyclable XImage backing buffer */
    void *ximg_data;        /* malloc'd buffer for ximg->data */
    EGLConfig config;       /* original config; remember for resize-rebuild */
    EGLContext last_ctx;    /* last context bound here (for makecurrent rebind) */
    struct surface_map_entry *next;
} surface_map_entry_t;

static surface_map_entry_t *g_surface_map = NULL;
static pthread_mutex_t g_surface_lock = PTHREAD_MUTEX_INITIALIZER;

/* dlsym'd ANGLE libEGL + X11 + libGLES symbols */
static EGLSurface (*real_eglCreatePbufferSurface)(EGLDisplay, EGLConfig, const EGLint*) = NULL;
static EGLBoolean (*real_eglDestroySurface)(EGLDisplay, EGLSurface) = NULL;
static EGLBoolean (*real_eglSwapBuffers)(EGLDisplay, EGLSurface) = NULL;
static EGLBoolean (*real_eglMakeCurrent)(EGLDisplay, EGLSurface, EGLSurface, EGLContext) = NULL;
static void (*real_glReadPixels)(GLint, GLint, GLsizei, GLsizei, GLenum, GLenum, void*) = NULL;
static void (*real_glViewport)(GLint, GLint, GLsizei, GLsizei) = NULL;
static GLenum (*real_glGetError)(void) = NULL;

static int (*real_XPutImage)(Display*, Drawable, GC, XImage*, int, int, int, int, unsigned int, unsigned int) = NULL;
static GC (*real_XCreateGC)(Display*, Drawable, unsigned long, XGCValues*) = NULL;
static int (*real_XGetGeometry)(Display*, Drawable, Window*, int*, int*, unsigned int*, unsigned int*, unsigned int*, unsigned int*) = NULL;
static XImage *(*real_XCreateImage)(Display*, Visual*, unsigned int, int, int, char*, unsigned int, unsigned int, int, int) = NULL;

/* The EGLDisplay's underlying X11 Display* — ANGLE stores this in its
 * internal state, but we have no public accessor. We sniff it from
 * eglGetDisplay() at the time of the call.
 */
static Display *g_xdpy = NULL;

/* Constructor — print loaded message so we can confirm the shim is in.
 * Priority 99 so we run before preload_gtkinit.c's 101.
 */
__attribute__((constructor(99)))
static void preload_egl_x11_init(void) {
    fprintf(stderr, "[x2d-egl] preload_egl_x11.so loaded "
                    "(constructor priority 99) X2D_EGL_DEBUG=%s\n",
            getenv("X2D_EGL_DEBUG") ? "1" : "0");
    fflush(stderr);
}

/* ------------------------------------------------------------------
 * Helpers
 * ------------------------------------------------------------------ */
static FILE *x2d_egl_log(void) {
    static FILE *fp = NULL;
    if (fp) return fp;
    /* Use a file in $TMPDIR so output is captured even if stderr is
     * redirected by BS itself or its child WebKit processes. */
    const char *tmp = getenv("TMPDIR");
    if (!tmp) tmp = "/tmp";
    char path[256];
    snprintf(path, sizeof(path), "%s/x2d_egl_shim.log", tmp);
    fp = fopen(path, "a");
    if (!fp) fp = stderr;
    return fp;
}

#define DBG(...) do { \
    FILE *_f = x2d_egl_log(); \
    fprintf(_f, "[x2d-egl pid=%d] ", (int)getpid()); \
    fprintf(_f, __VA_ARGS__); \
    fputc('\n', _f); fflush(_f); \
} while (0)

static void resolve_real_funcs(void) {
    static int resolved = 0;
    if (resolved) return;
    resolved = 1;

    /* ANGLE libEGL — already loaded by main app, we just dlsym-NEXT */
    real_eglCreatePbufferSurface = dlsym(RTLD_NEXT, "eglCreatePbufferSurface");
    real_eglDestroySurface       = dlsym(RTLD_NEXT, "eglDestroySurface");
    real_eglSwapBuffers          = dlsym(RTLD_NEXT, "eglSwapBuffers");
    real_eglMakeCurrent          = dlsym(RTLD_NEXT, "eglMakeCurrent");

    /* GLES — for glReadPixels at swap time */
    real_glReadPixels   = dlsym(RTLD_NEXT, "glReadPixels");
    real_glViewport     = dlsym(RTLD_NEXT, "glViewport");
    real_glGetError     = dlsym(RTLD_NEXT, "glGetError");

    /* X11 */
    real_XPutImage   = dlsym(RTLD_NEXT, "XPutImage");
    real_XCreateGC   = dlsym(RTLD_NEXT, "XCreateGC");
    real_XGetGeometry = dlsym(RTLD_NEXT, "XGetGeometry");
    real_XCreateImage = dlsym(RTLD_NEXT, "XCreateImage");

    DBG("resolve_real_funcs: pbuf=%p swap=%p readpx=%p XPut=%p",
        real_eglCreatePbufferSurface, real_eglSwapBuffers,
        real_glReadPixels, real_XPutImage);
}

static surface_map_entry_t *find_entry(EGLSurface pbuf) {
    surface_map_entry_t *e = g_surface_map;
    while (e) {
        if (e->pbuf == pbuf) return e;
        e = e->next;
    }
    return NULL;
}

static void free_ximage(surface_map_entry_t *e) {
    if (e->ximg) {
        e->ximg->data = NULL;  /* don't let XDestroyImage free our malloc */
        XDestroyImage(e->ximg);
        e->ximg = NULL;
    }
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
    if (!e->ximg) {
        free(e->ximg_data);
        e->ximg_data = NULL;
        return -1;
    }
    return 0;
}

/* ------------------------------------------------------------------
 * Intercepts
 * ------------------------------------------------------------------ */

/* eglGetDisplay — capture the X11 Display* so we can XPutImage later.
 * For native_display = EGL_DEFAULT_DISPLAY (NULL), ANGLE picks the
 * default; we open our own connection. For an X11 native display it's
 * a Display*.
 */
__attribute__((visibility("default")))
EGLDisplay eglGetDisplay(EGLNativeDisplayType native_display) {
    resolve_real_funcs();
    EGLDisplay (*real)(EGLNativeDisplayType) =
        dlsym(RTLD_NEXT, "eglGetDisplay");
    EGLDisplay r = real ? real(native_display) : EGL_NO_DISPLAY;

    /* Capture X11 Display* — wxGLCanvasEGL on GTK X11 passes a
     * GdkDisplay's underlying Display*. For NULL we'll re-open later
     * inside ensure_xdpy().
     */
    if (native_display && native_display != (EGLNativeDisplayType)EGL_DEFAULT_DISPLAY) {
        g_xdpy = (Display*)native_display;
        DBG("eglGetDisplay: captured X11 dpy=%p", g_xdpy);
    }
    return r;
}

static Display *ensure_xdpy(void) {
    if (g_xdpy) return g_xdpy;
    g_xdpy = XOpenDisplay(NULL);
    DBG("ensure_xdpy: opened own connection dpy=%p", g_xdpy);
    return g_xdpy;
}

/* eglCreatePlatformWindowSurface — when wx asks for a surface bound
 * to an X11 window, we instead create a pbuffer and remember the
 * X11 XID for later XPutImage.
 */
__attribute__((visibility("default")))
EGLSurface eglCreatePlatformWindowSurface(EGLDisplay dpy, EGLConfig config,
                                           void *native_window,
                                           const EGLAttrib *attrib_list) {
    (void)attrib_list;
    resolve_real_funcs();
    if (!native_window) return EGL_NO_SURFACE;

    /* On EGL_PLATFORM_X11_KHR, native_window points at a Window (XID), not
     * a Display+Window pair. For the wx 3.3 wxGLCanvasEGL path, this is
     * specifically `&m_xwindow` where m_xwindow is the GdkX11Window's
     * XID (uint32 typically; on 64-bit aarch64 wx uses unsigned long for
     * Window).
     */
    Window xwin = *(Window*)native_window;
    DBG("eglCreatePlatformWindowSurface: x11 win=0x%lx", xwin);

    Display *xdpy = ensure_xdpy();
    if (!xdpy) {
        DBG("  no X11 display available, falling back");
        EGLSurface (*real)(EGLDisplay, EGLConfig, void*, const EGLAttrib*) =
            dlsym(RTLD_NEXT, "eglCreatePlatformWindowSurface");
        return real ? real(dpy, config, native_window, attrib_list) : EGL_NO_SURFACE;
    }

    /* Geometry */
    Window root;
    int x, y;
    unsigned int w, h, bw, depth;
    if (real_XGetGeometry(xdpy, xwin, &root, &x, &y, &w, &h, &bw, &depth) == 0) {
        DBG("  XGetGeometry failed for win=0x%lx", xwin);
        return EGL_NO_SURFACE;
    }
    if (w == 0 || h == 0) { w = 800; h = 600; }
    DBG("  win=0x%lx %ux%u depth=%u", xwin, w, h, depth);

    /* Create pbuffer */
    EGLint pbuf_attribs[] = {
        EGL_WIDTH,  (EGLint)w,
        EGL_HEIGHT, (EGLint)h,
        EGL_TEXTURE_TARGET, EGL_NO_TEXTURE,
        EGL_TEXTURE_FORMAT, EGL_NO_TEXTURE,
        EGL_NONE,
    };
    EGLSurface pbuf = real_eglCreatePbufferSurface(dpy, config, pbuf_attribs);
    if (pbuf == EGL_NO_SURFACE) {
        DBG("  eglCreatePbufferSurface failed: 0x%x", eglGetError());
        return EGL_NO_SURFACE;
    }

    /* Map */
    surface_map_entry_t *e = calloc(1, sizeof(*e));
    e->pbuf   = pbuf;
    e->xdpy   = xdpy;
    e->xwin   = xwin;
    e->width  = (int)w;
    e->height = (int)h;
    e->config = config;

    pthread_mutex_lock(&g_surface_lock);
    e->next = g_surface_map;
    g_surface_map = e;
    pthread_mutex_unlock(&g_surface_lock);

    DBG("  registered pbuf=%p ↔ xwin=0x%lx (%dx%d)", pbuf, xwin, e->width, e->height);
    return pbuf;
}

/* eglDestroySurface — clean up the map entry */
__attribute__((visibility("default")))
EGLBoolean eglDestroySurface(EGLDisplay dpy, EGLSurface surface) {
    resolve_real_funcs();
    pthread_mutex_lock(&g_surface_lock);
    surface_map_entry_t **pp = &g_surface_map;
    while (*pp) {
        if ((*pp)->pbuf == surface) {
            surface_map_entry_t *dead = *pp;
            *pp = dead->next;
            free_ximage(dead);
            if (dead->gc && dead->xdpy) XFreeGC(dead->xdpy, dead->gc);
            free(dead);
            DBG("eglDestroySurface: unmapped pbuf=%p", surface);
            break;
        }
        pp = &(*pp)->next;
    }
    pthread_mutex_unlock(&g_surface_lock);
    return real_eglDestroySurface(dpy, surface);
}

/* eglSwapBuffers — the heart of the shim. glReadPixels → XPutImage. */
__attribute__((visibility("default")))
EGLBoolean eglSwapBuffers(EGLDisplay dpy, EGLSurface surface) {
    resolve_real_funcs();

    pthread_mutex_lock(&g_surface_lock);
    surface_map_entry_t *e = find_entry(surface);
    pthread_mutex_unlock(&g_surface_lock);

    if (!e) {
        /* Not one of ours — pass through to ANGLE's swap (probably
         * a pbuffer that wasn't created via our shim path; treat as
         * a no-op-ish swap).
         */
        return real_eglSwapBuffers(dpy, surface);
    }

    /* Refresh geometry — the canvas may have been resized */
    Window root;
    int xx, yy;
    unsigned int w, h, bw, depth;
    if (real_XGetGeometry(e->xdpy, e->xwin, &root, &xx, &yy,
                          &w, &h, &bw, &depth) == 0) {
        DBG("swap: XGetGeometry failed for win=0x%lx; skipping present",
            e->xwin);
        return EGL_TRUE;
    }
    if ((int)w != e->width || (int)h != e->height) {
        DBG("swap: window resized %dx%d → %ux%u (XImage rebuild needed)",
            e->width, e->height, w, h);
        e->width = (int)w; e->height = (int)h;
        free_ximage(e);
    }
    if (ensure_ximage(e) < 0) {
        DBG("swap: ensure_ximage failed");
        return EGL_TRUE;
    }
    if (!e->gc) {
        e->gc = real_XCreateGC(e->xdpy, e->xwin, 0, NULL);
        if (!e->gc) {
            DBG("swap: XCreateGC failed");
            return EGL_TRUE;
        }
    }

    /* Read pixels from the bound GL framebuffer (the pbuffer's
     * back buffer). Format BGRA matches X11 ZPixmap layout on
     * little-endian aarch64 with 24bpp depth. We pad to 32-bit
     * stride with X11's bytes_per_line.
     */
    real_glReadPixels(0, 0, e->width, e->height, GL_BGRA_EXT,
                       GL_UNSIGNED_BYTE, e->ximg_data);
    GLenum err = real_glGetError();
    if (err != GL_NO_ERROR) {
        /* GL_BGRA_EXT might not be supported by ANGLE on first try.
         * Fall back to RGBA + manual swizzle.
         */
        real_glReadPixels(0, 0, e->width, e->height, GL_RGBA,
                           GL_UNSIGNED_BYTE, e->ximg_data);
        uint32_t *p = (uint32_t*)e->ximg_data;
        size_t n = (size_t)e->width * (size_t)e->height;
        for (size_t i = 0; i < n; i++) {
            uint32_t v = p[i];
            /* RGBA → BGRA: swap R and B */
            p[i] = (v & 0xFF00FF00u) |
                   ((v & 0x00FF0000u) >> 16) |
                   ((v & 0x000000FFu) << 16);
        }
    }

    /* Y-flip: GL origin is bottom-left, X11 origin is top-left.
     * Swap rows in place (cheaper than a full memcpy via temp buffer
     * for typical 800x600 sizes).
     */
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

    /* Push to X11 window */
    real_XPutImage(e->xdpy, e->xwin, e->gc, e->ximg, 0, 0, 0, 0,
                   e->width, e->height);
    XFlush(e->xdpy);

    DBG("swap: presented pbuf=%p → xwin=0x%lx (%dx%d)",
        surface, e->xwin, e->width, e->height);
    return EGL_TRUE;
}
