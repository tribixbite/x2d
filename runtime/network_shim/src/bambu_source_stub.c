/* bambu_source_stub.c — minimal libBambuSource.so stub.
 *
 * BambuStudio's GUI_App::on_init_network gates create_network_agent on
 * `get_bambu_source_entry() != null`. That call dlopen's
 * `<plugin_folder>/libBambuSource.so`; if the file doesn't exist the
 * dlopen returns null, the gate fails, and `m_agent` is never created
 * — so none of our libbambu_networking.so entry points get reached.
 *
 * The host checks the handle is non-null but does NOT dlsym any
 * specific Bambu_* symbol up front. PrinterFileSystem.cpp later dlsyms
 * Bambu_Create / Bambu_Open / etc. for the "browse files on printer SD"
 * feature, but those are tolerated as nullptrs (the file browser just
 * shows empty) — they're not on the connect/AMS/print critical path.
 *
 * So this .so just has to exist and dlopen cleanly. One trivial export
 * is enough to make linkers happy.
 */

#include <stddef.h>

__attribute__((visibility("default")))
int bambu_source_abi_version(void) { return 1; }
