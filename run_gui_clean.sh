#!/data/data/com.termux/files/usr/bin/bash
export DISPLAY=:1 LC_ALL=C LANG=C
export WXSUPPRESS_SIZER_FLAGS_CHECK=1
# wx 3.x: silence non-fatal debug asserts so they don't pop a dialog or abort
export WXSUPPRESS_DBL_CLICK_ASSERT=1
export WXASSERT_DISABLE=1
mkdir -p ~/.config/BambuStudio
[[ -s ~/.config/BambuStudio/BambuStudio.conf ]] || echo '{ "app": { "language": "en_US", "first_run": false } }' > ~/.config/BambuStudio/BambuStudio.conf
export LD_PRELOAD=/data/data/com.termux/files/home/git/x2d/runtime/libpreloadgtk.so
unset LD_LIBRARY_PATH EPOXY_USE_ANGLE MESA_GL_VERSION_OVERRIDE \
      MESA_GLES_VERSION_OVERRIDE MESA_GLSL_VERSION_OVERRIDE LIBGL_DRI3_DISABLE

# Force Mesa llvmpipe (software GL) instead of zink (Vulkan→GL).
# zink_kopper.c:720 asserts on swapchain acquire because termux-x11 has no
# DRI3/Present, so kopper can't allocate presentable images. Triggers as soon
# as wxGLCanvas actually renders (Prepare tab, network-plugin install dialog
# with embedded WebView, etc.). llvmpipe renders to an offscreen surface and
# blits via XPutImage, which termux-x11 supports fine.
export GALLIUM_DRIVER=llvmpipe
export LIBGL_ALWAYS_SOFTWARE=1
export MESA_LOADER_DRIVER_OVERRIDE=llvmpipe
# wx 3.3 GLCanvas needs EGL; surfaceless lets Mesa allocate offscreen render
# targets without GLX (which termux-x11 doesn't expose either).
export EGL_PLATFORM=surfaceless

cd /data/data/com.termux/files/home/git/x2d/bs-bionic
exec ./build/src/bambu-studio "$@"
