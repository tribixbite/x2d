/*
 * probe_vendor_window.c — exercises libEGL_x2dadreno's X11-window
 * redirect path: creates a real X11 window, calls
 * eglCreatePlatformWindowSurface (which our vendor maps to a pbuffer
 * + remembers the XID), draws a colored quad via GLES2, swaps
 * (vendor does glReadPixels + XPutImage), and waits for visual
 * confirmation.
 *
 * Build:
 *   clang -O2 -o probe_vendor_window probe_vendor_window.c -lEGL -lGLESv2 -lX11
 *
 * Run:
 *   DISPLAY=:1 \
 *     __EGL_VENDOR_LIBRARY_FILENAMES=$PREFIX/share/glvnd/egl_vendor.d/40_x2dadreno.json \
 *     ./probe_vendor_window
 *
 * Expected: a 400x300 X11 window appears with a smooth color gradient,
 * the title shows the GL_RENDERER. Process holds for 5 seconds then
 * exits cleanly.
 */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

static const char *VS = "attribute vec2 a_pos;\n"
                        "varying vec2 v_uv;\n"
                        "void main(){ v_uv = a_pos*0.5+0.5; gl_Position = vec4(a_pos,0,1); }\n";
static const char *FS = "precision mediump float;\n"
                        "varying vec2 v_uv;\n"
                        "uniform float u_t;\n"
                        "void main(){ gl_FragColor = vec4(v_uv.x, v_uv.y, 0.5+0.5*sin(u_t), 1); }\n";

static GLuint compile(GLenum t, const char *src) {
    GLuint s = glCreateShader(t);
    glShaderSource(s, 1, &src, NULL);
    glCompileShader(s);
    GLint ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[1024]; glGetShaderInfoLog(s, sizeof(log), NULL, log);
        fprintf(stderr, "shader compile fail: %s\n", log);
    }
    return s;
}

int main(void) {
    Display *xdpy = XOpenDisplay(NULL);
    if (!xdpy) { fprintf(stderr, "no X dpy\n"); return 1; }
    int scr = DefaultScreen(xdpy);
    Window root = RootWindow(xdpy, scr);
    Window win = XCreateSimpleWindow(xdpy, root, 0, 0, 400, 300, 0,
                                      0, 0xff202020);
    XStoreName(xdpy, win, "x2d-vendor-probe");
    XSelectInput(xdpy, win, ExposureMask | StructureNotifyMask);
    XMapWindow(xdpy, win);
    XFlush(xdpy);

    EGLDisplay dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    if (!eglInitialize(dpy, NULL, NULL)) {
        fprintf(stderr, "eglInitialize failed: 0x%x\n", eglGetError()); return 2;
    }
    fprintf(stderr, "EGL vendor: %s\n", eglQueryString(dpy, EGL_VENDOR));

    EGLint cfg_a[] = {
        EGL_SURFACE_TYPE, EGL_WINDOW_BIT | EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
        EGL_NONE,
    };
    EGLConfig cfg; EGLint nc = 0;
    if (!eglChooseConfig(dpy, cfg_a, &cfg, 1, &nc) || nc < 1) {
        fprintf(stderr, "ChooseConfig: 0x%x\n", eglGetError()); return 3;
    }
    eglBindAPI(EGL_OPENGL_ES_API);
    EGLSurface surf = eglCreatePlatformWindowSurface(dpy, cfg, &win, NULL);
    if (surf == EGL_NO_SURFACE) {
        fprintf(stderr, "CreatePlatformWindowSurface: 0x%x\n", eglGetError()); return 4;
    }
    EGLint ctx_a[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_a);
    if (ctx == EGL_NO_CONTEXT) {
        fprintf(stderr, "CreateContext: 0x%x\n", eglGetError()); return 5;
    }
    if (!eglMakeCurrent(dpy, surf, surf, ctx)) {
        fprintf(stderr, "MakeCurrent: 0x%x\n", eglGetError()); return 6;
    }
    fprintf(stderr, "GL_VERSION:  %s\n", glGetString(GL_VERSION));
    fprintf(stderr, "GL_RENDERER: %s\n", glGetString(GL_RENDERER));

    /* Bind a colorful quad shader so swap should paint a visible gradient. */
    GLuint vs = compile(GL_VERTEX_SHADER, VS);
    GLuint fs = compile(GL_FRAGMENT_SHADER, FS);
    GLuint prog = glCreateProgram();
    glAttachShader(prog, vs); glAttachShader(prog, fs);
    glBindAttribLocation(prog, 0, "a_pos");
    glLinkProgram(prog);
    glUseProgram(prog);
    GLint u_t = glGetUniformLocation(prog, "u_t");

    static const float quad[] = { -1,-1,  1,-1,  -1,1,  1,1 };
    GLuint vbo; glGenBuffers(1, &vbo);
    glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glBufferData(GL_ARRAY_BUFFER, sizeof(quad), quad, GL_STATIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, NULL);

    /* Render 30 frames so the swap path gets exercised + we can time it. */
    long t0_ns;
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    t0_ns = ts.tv_sec * 1000000000L + ts.tv_nsec;
    int frames = 0;
    for (frames = 0; frames < 30; frames++) {
        glViewport(0, 0, 400, 300);
        glClearColor(0.1f, 0.1f, 0.15f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        glUniform1f(u_t, frames * 0.1f);
        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
        eglSwapBuffers(dpy, surf);
        XFlush(xdpy);
        usleep(33000);  /* ~30 fps */
    }
    clock_gettime(CLOCK_MONOTONIC, &ts);
    long t1_ns = ts.tv_sec * 1000000000L + ts.tv_nsec;
    double avg_ms = (t1_ns - t0_ns) / 1.0e6 / frames;
    fprintf(stderr, "%d frames, avg %.2f ms (incl 33ms sleep) → effective %.2f fps render-only\n",
        frames, avg_ms, 1000.0 / (avg_ms - 33));

    sleep(8);  /* leave window visible for the user */
    eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroyContext(dpy, ctx);
    eglDestroySurface(dpy, surf);
    eglTerminate(dpy);
    XDestroyWindow(xdpy, win);
    XCloseDisplay(xdpy);
    return 0;
}
