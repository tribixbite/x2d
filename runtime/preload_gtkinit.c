/*
 * preload_gtkinit.c
 *
 * LD_PRELOAD shim that runs gtk_init_check() in a constructor BEFORE
 * BambuStudio's main() executes any wxWidgets / GTK code.
 *
 * Why: BambuStudio (bionic build, GUI=ON) calls Label::initSysFont()
 * from CLI::run() before GUI_Run() is reached. initSysFont() invokes
 * wxFont::AddPrivateFont() which on linux/GTK3 internally creates a
 * GtkCssProvider. Without an open default GdkDisplay this fails with:
 *   GLib-GObject-CRITICAL: invalid (NULL) pointer instance
 *   Gtk-ERROR: Can't create a GtkStyleContext without a display connection
 * and the binary aborts before any window is shown.
 *
 * By opening the default display in a high-priority constructor we make
 * GTK ready by the time wx static-init or BambuStudio init runs.
 *
 * Build:
 *   gcc -fPIC -shared preload_gtkinit.c \
 *       $(pkg-config --cflags --libs gtk+-3.0) \
 *       -o libpreloadgtk.so
 *
 * Use:
 *   LD_PRELOAD=/path/to/libpreloadgtk.so DISPLAY=:1 ./bambu-studio
 */
#define _GNU_SOURCE
#include <gtk/gtk.h>
#include <dlfcn.h>
#include <locale.h>
#include <stdio.h>
#include <string.h>

__attribute__((constructor(101)))
static void preinit_gtk(void) {
    int argc = 0;
    char **argv = NULL;
    fprintf(stderr, "[preload] gtk_init_check before any wx code\n");
    if (!gtk_init_check(&argc, &argv)) {
        fprintf(stderr, "[preload] gtk_init_check FAILED (DISPLAY set?)\n");
    } else {
        fprintf(stderr, "[preload] gtk_init_check OK, default display=%p\n",
                (void *)gdk_display_get_default());
    }
}

/*
 * setlocale() interception.
 *
 * Termux's bionic libc has very narrow setlocale() acceptance: it
 * recognises only "" / "C" / "POSIX" / "C.UTF-8" — and uniquely also
 * "en_US.UTF-8" which it aliases to C.UTF-8. wxLocale::IsAvailable()
 * passes the wxLanguageInfo CanonicalName, e.g. plain "en_US" without an
 * encoding suffix; bionic returns NULL for that, so wxLocale considers
 * en_US unavailable, BambuStudio pops "Switching Bambu Studio to language
 * en_US failed", clicks call std::exit(EXIT_FAILURE), and the GUI never
 * starts.
 *
 * The shim transparently appends ".UTF-8" when the requested locale has
 * no encoding suffix and the bare-name lookup fails. From wx's POV,
 * IsAvailable() returns true and load_language() proceeds to wxLocale
 * construction, which itself does the same fallback internally.
 *
 * Side effects: only triggers when bionic rejects the original; never
 * upgrades a successful name. Safe for libraries that pass canonical
 * locale names like "C", "C.UTF-8", "en_US.UTF-8" — they're returned
 * unchanged.
 */
static char *(*real_setlocale)(int, const char *) = NULL;
static locale_t (*real_newlocale)(int, const char *, locale_t) = NULL;

static int locale_needs_utf8_suffix(const char *locale) {
    if (locale == NULL || *locale == '\0') return 0;
    if (strchr(locale, '.') != NULL) return 0;
    /* "C" / "POSIX" — accept as-is, never rewrite */
    if (strcmp(locale, "C") == 0 || strcmp(locale, "POSIX") == 0) return 0;
    return 1;
}

char *setlocale(int category, const char *locale) {
    if (!real_setlocale)
        real_setlocale = dlsym(RTLD_NEXT, "setlocale");
    char *r = real_setlocale(category, locale); fprintf(stderr, "[preload] setlocale(%d,\"%s\") -> %s\n", category, locale ? locale : "(null)", r ? r : "(null)");
    if (r != NULL) return r;
    if (locale_needs_utf8_suffix(locale)) {
        char buf[96];
        snprintf(buf, sizeof(buf), "%s.UTF-8", locale);
        char *r2 = real_setlocale(category, buf);
        if (r2) {
            fprintf(stderr, "[preload] setlocale(%d,%s) -> remapped to %s -> %s\n",
                    category, locale, buf, r2);
            return r2;
        }
    }
    return NULL;
}

locale_t newlocale(int category_mask, const char *locale, locale_t base) {
    if (!real_newlocale)
        real_newlocale = dlsym(RTLD_NEXT, "newlocale");
    locale_t r = real_newlocale(category_mask, locale, base);
    if (r != (locale_t)0) return r;
    if (locale_needs_utf8_suffix(locale)) {
        char buf[96];
        snprintf(buf, sizeof(buf), "%s.UTF-8", locale);
        locale_t r2 = real_newlocale(category_mask, buf, base);
        if (r2 != (locale_t)0) {
            fprintf(stderr, "[preload] newlocale(%d,%s) -> remapped to %s\n",
                    category_mask, locale, buf);
            return r2;
        }
    }
    return (locale_t)0;
}

/*
 * wxLocale::IsAvailable(int) override. The setlocale shim above DOES make
 * bionic accept canonical names like "en_US" via UTF-8 fallback, but wx
 * 3.3 on this build uses wxUILocale (ICU-backed) for IsAvailable, not the
 * old setlocale path — the ICU locale-data package shipped with Termux's
 * libicu does not include `en_US` as a recognized locale at runtime, so
 * IsAvailable returns false and BambuStudio's load_language() pops the
 * "Switching Bambu Studio to language en_US failed" modal then exits with
 * EXIT_FAILURE.
 *
 * Forcing IsAvailable to always return true short-circuits the whole
 * locale-validation mess. wx will then proceed to wxLocale::Init() and
 * use whatever wxSetlocale returns (which in our shim succeeds via the
 * UTF-8 fallback). UI strings come from wxTranslations::AddCatalog,
 * which is independent of locale availability.
 */
__attribute__((visibility("default")))
_Bool _ZN8wxLocale11IsAvailableEi(int lang) {
    (void)lang;
    return 1;
}
__attribute__((visibility("default")))
_Bool _ZN10wxUILocale11IsAvailableEi(int lang) {
    (void)lang;
    return 1;
}

/*
 * wxOnAssert override — turn every wx assertion failure into a no-op.
 *
 * BambuStudio's GUI startup hits a stream of debug asserts in wx 3.3:
 *   wincmn.cpp:2429   Adding a window already in a sizer
 *   bookctrl.cpp:420  invalid page index in DoRemovePage
 *   textcmn.cpp:936   Use SetValue() / ChangeValue() instead
 *   wincmn.cpp:.....  size constraints, sizer flags, etc.
 *
 * These are wx-3.3 stricter-validation regressions against BambuStudio's
 * 2.6.0 GUI code (which was written for wx 3.1/3.2). wx's default assert
 * handler escalates to wxAbort() after a few — taking the GUI down before
 * the main frame finishes building. None of them are functionally fatal:
 * BambuStudio runs fine on Windows/macOS/Linux against released wx 3.2.
 *
 * Overriding wxOnAssert (all 4 wxString-flavoured overloads + the wide-
 * char one) silences the entire chain. Asserts still fire but are
 * dropped on the floor; the GUI keeps initialising and renders.
 *
 * The mangled names below are the ARM64 Itanium C++ ABI symbols visible
 * in libwx_baseu-3.3.so (verified via nm -D). Identifying them by
 * mangled-name override is the only way to suppress wx's behaviour
 * without rebuilding wx.
 */
__attribute__((visibility("default")))
void _Z10wxOnAssertPKciS0_S0_(const char *f, int l, const char *fn, const char *c) {
    (void)f; (void)l; (void)fn; (void)c;
}
__attribute__((visibility("default")))
void _Z10wxOnAssertPKciS0_S0_PKw(const char *f, int l, const char *fn, const char *c, const wchar_t *m) {
    (void)f; (void)l; (void)fn; (void)c; (void)m;
}
__attribute__((visibility("default")))
void _Z10wxOnAssertPKciS0_S0_S0_(const char *f, int l, const char *fn, const char *c, const char *m) {
    (void)f; (void)l; (void)fn; (void)c; (void)m;
}

/*
 * GUI_App::config_wizard_startup() — force-return false so the WebGuideDialog
 * (Setup Wizard) never opens.
 *
 * BambuStudio decides to pop the first-run wizard if either:
 *   - no AppConfig file existed pre-launch, OR
 *   - PresetBundle::PrinterPresetCollection::only_default_printers() returns true
 *
 * The second condition is what we hit even with a hand-crafted AppConfig
 * pre-seeded with `models` + `presets.printer` entries, because the linkage
 * between AppConfig's "models" section and the loaded printer presets is
 * runtime-dependent. Rather than reverse-engineer the exact linkage we just
 * make the startup path say "no wizard needed."
 *
 * Side effect: the user has to pick a printer manually inside the main UI
 * (Filament Settings → Printer) before slicing inside the GUI. The CLI
 * pipeline (resolve_profile.py + bambu-studio --slice) is unaffected since
 * it never reads AppConfig.
 */
__attribute__((visibility("default")))
_Bool _ZN6Slic3r3GUI7GUI_App21config_wizard_startupEv(void *self) {
    (void)self;
    return 0;
}

/*
 * gtk_window_present() override — sizes + centers transient dialogs.
 *
 * Why: termux-x11 + openbox manages ordinary windows fine, but
 * BambuStudio's wxFileDialog spawns a GtkFileChooserDialog whose
 * upstream default size (~640×480) is computed for desktop displays
 * and ends up half-off-screen on a 672-wide phone in portrait. Same
 * for many of BambuStudio's own modal dialogs — they SetSize() to a
 * value that assumes 1920+ wide. Result: file picker is unusable
 * because the OK/Cancel buttons render off the right edge.
 *
 * Hook gtk_window_present (and gtk_window_present_with_time) so on
 * the first present of any transient/dialog window we:
 *   1. Read the host's primary display geometry via gdk_monitor.
 *   2. Compute a target size = min(current, 0.95 * display).
 *   3. SetSize + center on that monitor.
 *
 * Call ordering: we forward to the real symbol AFTER the resize so
 * the WM still maps the dialog with the new geometry. We use a
 * GObject quark to ensure each dialog is sized at most once (so
 * subsequent presents — e.g. user clicked away then back — don't
 * keep re-resizing and overriding any user drag).
 */
static void (*real_gtk_window_present)(GtkWindow *) = NULL;
static void (*real_gtk_window_present_with_time)(GtkWindow *, guint32) = NULL;

static GQuark x2d_resized_quark = 0;

static void x2d_clamp_dialog(GtkWindow *win) {
    if (!win || !GTK_IS_WINDOW(win)) return;
    if (x2d_resized_quark == 0)
        x2d_resized_quark = g_quark_from_static_string("x2d-resized");
    if (g_object_get_qdata(G_OBJECT(win), x2d_resized_quark)) return;
    g_object_set_qdata(G_OBJECT(win), x2d_resized_quark, GINT_TO_POINTER(1));

    GdkDisplay *disp = gtk_widget_get_display(GTK_WIDGET(win));
    if (!disp) disp = gdk_display_get_default();
    if (!disp) return;
    GdkMonitor *mon = gdk_display_get_primary_monitor(disp);
    if (!mon) mon = gdk_display_get_monitor(disp, 0);
    if (!mon) return;

    GdkRectangle area;
    gdk_monitor_get_workarea(mon, &area);
    if (area.width <= 0 || area.height <= 0) return;

    int cur_w = 0, cur_h = 0;
    gtk_window_get_size(win, &cur_w, &cur_h);

    /* Only intervene if the window doesn't fit the workarea OR if it's
     * tiny (likely a default GtkMessageDialog that won't have title-bar
     * room for OK/Cancel). Don't fight wxFrames that already sized
     * themselves correctly via SetSize — that includes BambuStudio's
     * MainFrame, which our MainFrame.cpp.termux.patch already clamps. */
    int max_w = area.width;
    int max_h = area.height;
    int new_w = cur_w;
    int new_h = cur_h;
    int needs_resize = 0;
    if (cur_w > max_w) { new_w = max_w; needs_resize = 1; }
    if (cur_h > max_h) { new_h = max_h; needs_resize = 1; }
    if (cur_w < 320 && cur_w < max_w) { new_w = 320 < max_w ? 320 : max_w; needs_resize = 1; }
    if (cur_h < 200 && cur_h < max_h) { new_h = 200 < max_h ? 200 : max_h; needs_resize = 1; }

    if (!needs_resize) {
        /* Already a sane size. Leave geometry alone — even the position,
         * since the WM (openbox) places it. */
        return;
    }

    gtk_window_set_default_size(win, new_w, new_h);
    gtk_window_resize(win, new_w, new_h);
    int x = area.x + (area.width  - new_w) / 2;
    int y = area.y + (area.height - new_h) / 2;
    if (x < area.x) x = area.x;
    if (y < area.y) y = area.y;
    gtk_window_move(win, x, y);

    fprintf(stderr, "[preload] resized dialog %dx%d→%dx%d at (%d,%d) "
                    "(workarea %dx%d+%d+%d)\n",
            cur_w, cur_h, new_w, new_h, x, y,
            area.width, area.height, area.x, area.y);
}

void gtk_window_present(GtkWindow *win) {
    if (!real_gtk_window_present)
        real_gtk_window_present = dlsym(RTLD_NEXT, "gtk_window_present");
    x2d_clamp_dialog(win);
    if (real_gtk_window_present) real_gtk_window_present(win);
}

void gtk_window_present_with_time(GtkWindow *win, guint32 timestamp) {
    if (!real_gtk_window_present_with_time)
        real_gtk_window_present_with_time =
            dlsym(RTLD_NEXT, "gtk_window_present_with_time");
    x2d_clamp_dialog(win);
    if (real_gtk_window_present_with_time)
        real_gtk_window_present_with_time(win, timestamp);
}
