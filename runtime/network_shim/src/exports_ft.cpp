// exports_ft.cpp — FileTransferModule (ft_*) C entry points.
//
// BambuStudio's FileTransferUtils.cpp dlsym()s these out of the SAME
// libbambu_networking.so as the bambu_network_* family. They power the
// "Send file to printer SD card via in-protocol tunnel" path (a
// Bambu-proprietary alternative to FTPS). For LAN mode on Termux we
// route FTPS uploads through the bridge instead, so the host code path
// that uses FT tunnels is never exercised — but we MUST export the
// symbols so InitFTModule's sym_lookup calls don't quietly leave
// nullptrs in the FileTransferModule struct that some callsite could
// later dereference.
//
// All entry points return FT_EUNKNOWN (-128) so any accidental caller
// fails loudly rather than blocking forever on a nullptr handle.

#include <cstddef>
#include <cstdint>

extern "C" {

// Match the enum from FileTransferUtils.hpp
enum {
    FT_OK         =    0,
    FT_EINVAL     =   -1,
    FT_ESTATE     =   -2,
    FT_EIO        =   -3,
    FT_ETIMEOUT   =   -4,
    FT_ECANCELLED =   -5,
    FT_EXCEPTION  =   -6,
    FT_EUNKNOWN   = -128,
};

struct FT_TunnelHandle;
struct FT_JobHandle;

struct ft_job_result { int ec; int resp_ec; const char* json; const void* bin; uint32_t bin_size; };
struct ft_job_msg    { int kind; const char* json; };

int  ft_abi_version(void) { return 1; }

void ft_free(void* /*p*/) {}
void ft_job_result_destroy(ft_job_result* /*r*/) {}
void ft_job_msg_destroy(ft_job_msg* /*m*/) {}

// tunnel
int ft_tunnel_create(const char* /*url*/, FT_TunnelHandle** out) {
    if (out) *out = nullptr;
    return FT_EUNKNOWN;
}
void ft_tunnel_retain (FT_TunnelHandle* /*h*/) {}
void ft_tunnel_release(FT_TunnelHandle* /*h*/) {}
int  ft_tunnel_start_connect(FT_TunnelHandle* /*h*/,
                             void(*/*cb*/)(void*, int, int, const char*),
                             void* /*user*/) { return FT_EUNKNOWN; }
int  ft_tunnel_sync_connect (FT_TunnelHandle* /*h*/) { return FT_EUNKNOWN; }
int  ft_tunnel_set_status_cb(FT_TunnelHandle* /*h*/,
                             void(*/*cb*/)(void*, int, int, int, const char*),
                             void* /*user*/) { return FT_EUNKNOWN; }
int  ft_tunnel_shutdown(FT_TunnelHandle* /*h*/) { return FT_EUNKNOWN; }

// job
int  ft_job_create(const char* /*params_json*/, FT_JobHandle** out) {
    if (out) *out = nullptr;
    return FT_EUNKNOWN;
}
void ft_job_retain (FT_JobHandle* /*h*/) {}
void ft_job_release(FT_JobHandle* /*h*/) {}
int  ft_job_set_result_cb(FT_JobHandle* /*h*/,
                          void(*/*cb*/)(void*, ft_job_result),
                          void* /*user*/) { return FT_EUNKNOWN; }
int  ft_job_get_result(FT_JobHandle* /*h*/, uint32_t /*timeout_ms*/,
                       ft_job_result* /*out*/) { return FT_EUNKNOWN; }
int  ft_tunnel_start_job(FT_TunnelHandle* /*t*/, FT_JobHandle* /*j*/) { return FT_EUNKNOWN; }
int  ft_job_cancel(FT_JobHandle* /*h*/) { return FT_EUNKNOWN; }
int  ft_job_set_msg_cb(FT_JobHandle* /*h*/,
                       void(*/*cb*/)(void*, ft_job_msg),
                       void* /*user*/) { return FT_EUNKNOWN; }
int  ft_job_try_get_msg(FT_JobHandle* /*h*/, ft_job_msg* /*out*/) { return FT_EUNKNOWN; }
int  ft_job_get_msg(FT_JobHandle* /*h*/, uint32_t /*timeout_ms*/,
                    ft_job_msg* /*out*/) { return FT_EUNKNOWN; }

} // extern "C"
