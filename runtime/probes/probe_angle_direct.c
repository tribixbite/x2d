/* Probe ANGLE directly via dlopen, bypassing libEGL.so.1 / libGLdispatch.
 * Build: clang -O2 -o probe_angle_direct probe_angle_direct.c -ldl -lX11
 */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <X11/Xlib.h>
#include <dlfcn.h>
#include <stdio.h>

int main(void) {
    Display *xdpy = XOpenDisplay(NULL);
    if (!xdpy) { fprintf(stderr, "XOpenDisplay failed\n"); return 1; }

    void *lib = dlopen("/data/data/com.termux/files/usr/opt/angle-android/vulkan/libEGL_angle.so", RTLD_NOW | RTLD_GLOBAL);
    if (!lib) { fprintf(stderr, "dlopen ANGLE: %s\n", dlerror()); return 2; }

    void *gles = dlopen("/data/data/com.termux/files/usr/opt/angle-android/vulkan/libGLESv2_angle.so", RTLD_NOW | RTLD_GLOBAL);
    if (!gles) { fprintf(stderr, "dlopen GLES: %s\n", dlerror()); return 2; }

    EGLDisplay (*aGetDpy)(EGLNativeDisplayType) = dlsym(lib, "eglGetDisplay");
    EGLDisplay (*aGetPlatDpy)(EGLenum, void*, const EGLAttrib*) = dlsym(lib, "eglGetPlatformDisplay");
    EGLBoolean (*aInit)(EGLDisplay, EGLint*, EGLint*) = dlsym(lib, "eglInitialize");
    EGLint (*aErr)(void) = dlsym(lib, "eglGetError");
    const char* (*aQueryStr)(EGLDisplay, EGLint) = dlsym(lib, "eglQueryString");

    fprintf(stderr, "ANGLE syms: GetDpy=%p GetPlatDpy=%p Init=%p Err=%p\n",
        aGetDpy, aGetPlatDpy, aInit, aErr);

    /* Approach 1: plain eglGetDisplay(NULL) */
    EGLDisplay d1 = aGetDpy(EGL_DEFAULT_DISPLAY);
    fprintf(stderr, "GetDisplay(EGL_DEFAULT_DISPLAY) = %p (err 0x%x)\n", d1, aErr());
    if (d1 != EGL_NO_DISPLAY) {
        EGLint maj=0, min=0;
        EGLBoolean rc = aInit(d1, &maj, &min);
        fprintf(stderr, "Initialize → %d, ver=%d.%d (err 0x%x)\n", rc, maj, min, aErr());
        if (rc) {
            fprintf(stderr, "  vendor=%s\n", aQueryStr(d1, EGL_VENDOR));
            fprintf(stderr, "  version=%s\n", aQueryStr(d1, EGL_VERSION));
            fprintf(stderr, "  apis=%s\n", aQueryStr(d1, EGL_CLIENT_APIS));
        }
    }

    /* Approach 2: eglGetPlatformDisplay with ANGLE Vulkan attribs */
    EGLAttrib attrs[] = {
        0x3203, /*EGL_PLATFORM_ANGLE_TYPE_ANGLE*/
        0x3450, /*EGL_PLATFORM_ANGLE_TYPE_VULKAN_ANGLE*/
        EGL_NONE,
    };
    EGLDisplay d2 = aGetPlatDpy(0x3202 /*EGL_PLATFORM_ANGLE_ANGLE*/,
                                EGL_DEFAULT_DISPLAY, attrs);
    fprintf(stderr, "GetPlatformDisplay(ANGLE_ANGLE, default, type=Vulkan) = %p (err 0x%x)\n",
        d2, aErr());
    if (d2 != EGL_NO_DISPLAY) {
        EGLint maj=0, min=0;
        EGLBoolean rc = aInit(d2, &maj, &min);
        fprintf(stderr, "Initialize → %d, ver=%d.%d (err 0x%x)\n", rc, maj, min, aErr());
        if (rc) {
            fprintf(stderr, "  vendor=%s\n", aQueryStr(d2, EGL_VENDOR));
            fprintf(stderr, "  version=%s\n", aQueryStr(d2, EGL_VERSION));
            fprintf(stderr, "  apis=%s\n", aQueryStr(d2, EGL_CLIENT_APIS));
            fprintf(stderr, "  extensions=%s\n", aQueryStr(d2, EGL_EXTENSIONS));
        }
    }
    return 0;
}
