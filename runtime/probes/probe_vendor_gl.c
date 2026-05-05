/*
 * probe_vendor_gl.c — minimal sanity check that mirrors what BS does:
 *   EGL_DEFAULT_DISPLAY → init → choose config → create context → make
 *   current → glGetString(GL_VERSION).
 *
 * Build:
 *   clang -O2 -o probe_vendor_gl probe_vendor_gl.c -lEGL -lGL -lX11
 *
 * Run with our vendor active:
 *   __EGL_VENDOR_LIBRARY_FILENAMES=$PREFIX/share/glvnd/egl_vendor.d/40_x2dadreno.json \
 *     ./probe_vendor_gl
 */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GL/gl.h>
#include <X11/Xlib.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    Display *xdpy = XOpenDisplay(NULL);
    if (!xdpy) { fprintf(stderr, "XOpenDisplay failed\n"); return 1; }

    EGLDisplay dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    fprintf(stderr, "eglGetDisplay(DEFAULT) = %p (err 0x%x)\n", dpy, eglGetError());
    if (dpy == EGL_NO_DISPLAY) {
        /* Fall back to passing X dpy */
        dpy = eglGetDisplay((EGLNativeDisplayType)xdpy);
        fprintf(stderr, "eglGetDisplay(xdpy) = %p (err 0x%x)\n", dpy, eglGetError());
        if (dpy == EGL_NO_DISPLAY) return 2;
    }
    EGLint maj=0, min=0;
    if (!eglInitialize(dpy, &maj, &min)) {
        fprintf(stderr, "eglInitialize failed err=0x%x\n", eglGetError());
        return 3;
    }
    fprintf(stderr, "EGL initialised %d.%d\n", maj, min);
    fprintf(stderr, "  vendor=%s\n", eglQueryString(dpy, EGL_VENDOR));
    fprintf(stderr, "  version=%s\n", eglQueryString(dpy, EGL_VERSION));
    fprintf(stderr, "  apis=%s\n", eglQueryString(dpy, EGL_CLIENT_APIS));

    EGLint cfg_attribs[] = {
        EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8,
        EGL_NONE,
    };
    EGLConfig cfg; EGLint nc = 0;
    if (!eglChooseConfig(dpy, cfg_attribs, &cfg, 1, &nc) || nc < 1) {
        fprintf(stderr, "eglChooseConfig failed err=0x%x nc=%d\n", eglGetError(), nc);
        return 4;
    }
    fprintf(stderr, "config picked, nc=%d\n", nc);

    eglBindAPI(EGL_OPENGL_ES_API);

    EGLint pbuf_attribs[] = { EGL_WIDTH, 64, EGL_HEIGHT, 64, EGL_NONE };
    EGLSurface surf = eglCreatePbufferSurface(dpy, cfg, pbuf_attribs);
    if (surf == EGL_NO_SURFACE) {
        fprintf(stderr, "CreatePbuffer failed err=0x%x\n", eglGetError());
        return 5;
    }
    EGLint ctx_attribs[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attribs);
    if (ctx == EGL_NO_CONTEXT) {
        fprintf(stderr, "CreateContext failed err=0x%x\n", eglGetError());
        return 6;
    }
    if (!eglMakeCurrent(dpy, surf, surf, ctx)) {
        fprintf(stderr, "MakeCurrent failed err=0x%x\n", eglGetError());
        return 7;
    }
    fprintf(stderr, "CONTEXT IS CURRENT\n");
    const char *gl_v = (const char*)glGetString(GL_VERSION);
    const char *gl_r = (const char*)glGetString(GL_RENDERER);
    const char *gl_x = (const char*)glGetString(GL_VENDOR);
    fprintf(stderr, "GL_VERSION  = %s\n", gl_v ? gl_v : "(null)");
    fprintf(stderr, "GL_RENDERER = %s\n", gl_r ? gl_r : "(null)");
    fprintf(stderr, "GL_VENDOR   = %s\n", gl_x ? gl_x : "(null)");

    eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroyContext(dpy, ctx);
    eglDestroySurface(dpy, surf);
    eglTerminate(dpy);
    XCloseDisplay(xdpy);
    return gl_v ? 0 : 8;
}
