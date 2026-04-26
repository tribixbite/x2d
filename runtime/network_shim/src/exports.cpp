// exports.cpp — extern "C" entry points for libbambu_networking.so.
//
// BambuStudio's NetworkAgent::initialize_network_module dlsym()s every
// symbol below by name. Missing symbols cause the related callsite to
// fall back to a no-op or, in some cases, to disable the entire feature.
// We export every typedef-d entry from src/slic3r/Utils/NetworkAgent.hpp.
//
// Call sites that drive LAN-mode (connect_printer / send_message_to_printer
// / start_local_print / set_on_local_message_fn / set_on_printer_connected_fn /
// disconnect_printer) marshal to the bridge for real work. Cloud entry
// points return BAMBU_NETWORK_SUCCESS with empty data; the GUI's cloud
// panels stay quiet but its core LAN flow works.

#include "shim_internal.hpp"

#include <cstring>

using namespace x2d_shim;
using namespace BBL;

// Forward decls from agent.cpp
namespace x2d_shim {
void register_bridge_event_handlers(Agent* a);
int  bridge_rc(const json& reply, int default_err);
json print_params_to_json(const PrintParams& p);
} // namespace x2d_shim

// Convenience: cast a void* agent handle to Agent* with validation.
static Agent* as_agent(void* p) {
    if (!is_valid_agent(p)) return nullptr;
    return static_cast<Agent*>(p);
}

// Diagnostic load tracer — fires the moment dlopen() actually maps the
// .so. If you don't see this in BambuStudio's stderr after launch, the
// host never tried to load us.
__attribute__((constructor))
static void x2d_shim_loaded_marker() {
    log_info("libbambu_networking.so loaded into host process");
}

extern "C" {

// ---------------------------------------------------------------------------
// Module-level (no agent handle) — these are the first things the host
// looks up. None of them touch the bridge.
// ---------------------------------------------------------------------------

bool bambu_network_check_debug_consistent(bool /*is_debug*/) {
    return true;
}

std::string bambu_network_get_version() {
    // Matches BAMBU_NETWORK_AGENT_VERSION in bambu_networking.hpp. The
    // host compares against this string to decide whether to install /
    // upgrade the network plug-in. Returning the canonical agent
    // version short-circuits the upgrade path.
    return "02.06.00.50";
}

void* bambu_network_create_agent(std::string log_dir) {
    auto* a = new Agent();
    a->log_dir = std::move(log_dir);
    a->bridge  = std::make_unique<BridgeClient>();
    {
        std::lock_guard<std::mutex> g(g_agent_mu);
        g_agents.push_back(a);
    }
    log_info("create_agent ok (handle=" + std::to_string(reinterpret_cast<uintptr_t>(a)) + ")");
    return a;
}

int bambu_network_destroy_agent(void* agent) {
    auto* a = as_agent(agent);
    if (!a) return BAMBU_NETWORK_ERR_INVALID_HANDLE;
    {
        std::lock_guard<std::mutex> g(g_agent_mu);
        g_agents.erase(std::remove(g_agents.begin(), g_agents.end(), a),
                       g_agents.end());
    }
    delete a;
    return BAMBU_NETWORK_SUCCESS;
}

int bambu_network_init_log(void* agent) {
    return as_agent(agent) ? BAMBU_NETWORK_SUCCESS : BAMBU_NETWORK_ERR_INVALID_HANDLE;
}

int bambu_network_set_config_dir(void* agent, std::string config_dir) {
    auto* a = as_agent(agent);
    if (!a) return BAMBU_NETWORK_ERR_INVALID_HANDLE;
    a->config_dir = std::move(config_dir);
    return BAMBU_NETWORK_SUCCESS;
}

int bambu_network_set_cert_file(void* agent, std::string folder, std::string filename) {
    auto* a = as_agent(agent);
    if (!a) return BAMBU_NETWORK_ERR_INVALID_HANDLE;
    a->cert_folder   = std::move(folder);
    a->cert_filename = std::move(filename);
    return BAMBU_NETWORK_SUCCESS;
}

int bambu_network_set_country_code(void* agent, std::string country_code) {
    auto* a = as_agent(agent);
    if (!a) return BAMBU_NETWORK_ERR_INVALID_HANDLE;
    a->country_code = std::move(country_code);
    return BAMBU_NETWORK_SUCCESS;
}

int bambu_network_start(void* agent) {
    auto* a = as_agent(agent);
    if (!a) return BAMBU_NETWORK_ERR_INVALID_HANDLE;
    if (!a->bridge->connect()) {
        log_err("bambu_network_start: bridge unavailable");
        return BAMBU_NETWORK_ERR_INVALID_HANDLE;
    }
    register_bridge_event_handlers(a);
    return BAMBU_NETWORK_SUCCESS;
}

// ---------------------------------------------------------------------------
// Callback registration — every set_on_*_fn just stashes the lambda.
// ---------------------------------------------------------------------------

#define DEFINE_SET_FN(NAME, FIELD, FNTYPE)                              \
    int bambu_network_##NAME(void* agent, FNTYPE fn) {                  \
        auto* a = as_agent(agent);                                      \
        if (!a) return BAMBU_NETWORK_ERR_INVALID_HANDLE;                \
        std::lock_guard<std::mutex> g(a->cb_mu);                        \
        a->FIELD = std::move(fn);                                       \
        return BAMBU_NETWORK_SUCCESS;                                   \
    }

DEFINE_SET_FN(set_on_ssdp_msg_fn,           on_ssdp_msg,           OnMsgArrivedFn)
DEFINE_SET_FN(set_on_user_login_fn,         on_user_login,         OnUserLoginFn)
DEFINE_SET_FN(set_on_printer_connected_fn,  on_printer_connected,  OnPrinterConnectedFn)
DEFINE_SET_FN(set_on_server_connected_fn,   on_server_connected,   OnServerConnectedFn)
DEFINE_SET_FN(set_on_http_error_fn,         on_http_error,         OnHttpErrorFn)
DEFINE_SET_FN(set_get_country_code_fn,      get_country_code,      GetCountryCodeFn)
DEFINE_SET_FN(set_on_subscribe_failure_fn,  on_subscribe_failure,  GetSubscribeFailureFn)
DEFINE_SET_FN(set_on_message_fn,            on_message,            OnMessageFn)
DEFINE_SET_FN(set_on_user_message_fn,       on_user_message,       OnMessageFn)
DEFINE_SET_FN(set_on_local_connect_fn,      on_local_connect,      OnLocalConnectedFn)
DEFINE_SET_FN(set_on_local_message_fn,      on_local_message,      OnMessageFn)
DEFINE_SET_FN(set_queue_on_main_fn,         queue_on_main,         QueueOnMainFn)
DEFINE_SET_FN(set_server_callback,          on_server_err,         OnServerErrFn)

#undef DEFINE_SET_FN

// ---------------------------------------------------------------------------
// Server (cloud) — return success-with-empty.
// ---------------------------------------------------------------------------

int bambu_network_connect_server(void* agent) {
    return as_agent(agent) ? BAMBU_NETWORK_SUCCESS : BAMBU_NETWORK_ERR_INVALID_HANDLE;
}

bool bambu_network_is_server_connected(void* /*agent*/) { return false; }

int bambu_network_refresh_connection(void* agent) {
    return as_agent(agent) ? BAMBU_NETWORK_SUCCESS : BAMBU_NETWORK_ERR_INVALID_HANDLE;
}

int bambu_network_start_subscribe(void* agent, std::string /*module*/) {
    return as_agent(agent) ? BAMBU_NETWORK_SUCCESS : BAMBU_NETWORK_ERR_INVALID_HANDLE;
}

int bambu_network_stop_subscribe(void* agent, std::string /*module*/) {
    return as_agent(agent) ? BAMBU_NETWORK_SUCCESS : BAMBU_NETWORK_ERR_INVALID_HANDLE;
}

int bambu_network_add_subscribe(void* agent, std::vector<std::string> /*dev_list*/) {
    return as_agent(agent) ? BAMBU_NETWORK_SUCCESS : BAMBU_NETWORK_ERR_INVALID_HANDLE;
}

int bambu_network_del_subscribe(void* agent, std::vector<std::string> /*dev_list*/) {
    return as_agent(agent) ? BAMBU_NETWORK_SUCCESS : BAMBU_NETWORK_ERR_INVALID_HANDLE;
}

void bambu_network_enable_multi_machine(void* /*agent*/, bool /*enable*/) {}

int bambu_network_send_message(void* /*agent*/, std::string /*dev_id*/, std::string /*json_str*/, int /*qos*/, int /*flag*/) {
    // Cloud-side message; LAN mode never reaches here.
    return BAMBU_NETWORK_SUCCESS;
}

// ---------------------------------------------------------------------------
// LAN printer — the core path the bridge actually services.
// ---------------------------------------------------------------------------

int bambu_network_connect_printer(void* agent, std::string dev_id, std::string dev_ip,
                                  std::string username, std::string password,
                                  bool use_ssl) {
    auto* a = as_agent(agent);
    if (!a) return BAMBU_NETWORK_ERR_INVALID_HANDLE;
    if (!a->bridge->is_connected()) return BAMBU_NETWORK_ERR_INVALID_HANDLE;
    json reply = a->bridge->request("connect_printer", json{
        {"dev_id",   dev_id},
        {"dev_ip",   dev_ip},
        {"username", username},
        {"password", password},
        {"use_ssl",  use_ssl},
    }, 10000);
    return bridge_rc(reply, BAMBU_NETWORK_ERR_CONNECT_FAILED);
}

int bambu_network_disconnect_printer(void* agent) {
    auto* a = as_agent(agent);
    if (!a) return BAMBU_NETWORK_ERR_INVALID_HANDLE;
    if (!a->bridge->is_connected()) return BAMBU_NETWORK_SUCCESS;
    json reply = a->bridge->request("disconnect_printer", json::object(), 4000);
    return bridge_rc(reply, BAMBU_NETWORK_ERR_DISCONNECT_FAILED);
}

int bambu_network_send_message_to_printer(void* agent, std::string dev_id,
                                          std::string json_str, int qos, int flag) {
    auto* a = as_agent(agent);
    if (!a) return BAMBU_NETWORK_ERR_INVALID_HANDLE;
    if (!a->bridge->is_connected()) return BAMBU_NETWORK_ERR_DISCONNECT_FAILED;
    json reply = a->bridge->request("send_message_to_printer", json{
        {"dev_id", dev_id}, {"json", json_str}, {"qos", qos}, {"flag", flag},
    }, 5000);
    return bridge_rc(reply, BAMBU_NETWORK_ERR_SEND_MSG_FAILED);
}

int bambu_network_update_cert(void* /*agent*/) { return BAMBU_NETWORK_SUCCESS; }

void bambu_network_install_device_cert(void* /*agent*/, std::string /*dev_id*/, bool /*lan_only*/) {}

bool bambu_network_start_discovery(void* /*agent*/, bool /*start*/, bool /*sending*/) {
    // LAN discovery is via the bridge socket; nothing for the host to do
    // here (it polls the device list separately).
    return true;
}

// ---------------------------------------------------------------------------
// User / login — LAN mode means logged-out, no presets, no tasks.
// ---------------------------------------------------------------------------

int bambu_network_change_user(void* /*agent*/, std::string /*user_info*/) { return BAMBU_NETWORK_SUCCESS; }
bool bambu_network_is_user_login(void* /*agent*/) { return false; }
int bambu_network_user_logout(void* /*agent*/, bool /*request*/) { return BAMBU_NETWORK_SUCCESS; }
std::string bambu_network_get_user_id(void* /*agent*/)       { return ""; }
std::string bambu_network_get_user_name(void* /*agent*/)     { return ""; }
std::string bambu_network_get_user_avatar(void* /*agent*/)   { return ""; }
std::string bambu_network_get_user_nickanme(void* /*agent*/) { return ""; }
std::string bambu_network_build_login_cmd(void* /*agent*/)   { return ""; }
std::string bambu_network_build_logout_cmd(void* /*agent*/)  { return ""; }
std::string bambu_network_build_login_info(void* /*agent*/)  { return ""; }

int bambu_network_ping_bind(void* /*agent*/, std::string /*ping_code*/) { return BAMBU_NETWORK_SUCCESS; }
int bambu_network_bind_detect(void* /*agent*/, std::string /*dev_ip*/, std::string /*sec_link*/, detectResult& /*detect*/) { return BAMBU_NETWORK_SUCCESS; }
int bambu_network_report_consent(void* /*agent*/, std::string /*expand*/) { return BAMBU_NETWORK_SUCCESS; }

int bambu_network_bind(void* /*agent*/, std::string /*dev_ip*/, std::string /*dev_id*/,
                       std::string /*sec_link*/, std::string /*timezone*/, bool /*improved*/,
                       OnUpdateStatusFn /*update_fn*/) {
    return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_unbind(void* /*agent*/, std::string /*dev_id*/) { return BAMBU_NETWORK_SUCCESS; }
std::string bambu_network_get_bambulab_host(void* /*agent*/) { return ""; }
std::string bambu_network_get_user_selected_machine(void* /*agent*/) { return ""; }
int bambu_network_set_user_selected_machine(void* /*agent*/, std::string /*dev_id*/) { return BAMBU_NETWORK_SUCCESS; }

// ---------------------------------------------------------------------------
// Print jobs — all funnel into the bridge's start_local_print op.
// ---------------------------------------------------------------------------

static int do_start_print(void* agent, PrintParams params,
                          OnUpdateStatusFn update_fn,
                          WasCancelledFn cancel_fn,
                          const std::string& op) {
    auto* a = as_agent(agent);
    if (!a) return BAMBU_NETWORK_ERR_INVALID_HANDLE;
    if (!a->bridge->is_connected()) return BAMBU_NETWORK_ERR_DISCONNECT_FAILED;
    {
        std::lock_guard<std::mutex> g(a->print_mu);
        a->active_print_status = std::move(update_fn);
        a->active_print_cancel = std::move(cancel_fn);
    }
    json reply = a->bridge->request(op, print_params_to_json(params), 300000);
    {
        std::lock_guard<std::mutex> g(a->print_mu);
        a->active_print_status = {};
        a->active_print_cancel = {};
    }
    return bridge_rc(reply, BAMBU_NETWORK_ERR_INVALID_RESULT);
}

int bambu_network_start_print(void* agent, PrintParams params,
                              OnUpdateStatusFn update_fn,
                              WasCancelledFn cancel_fn,
                              OnWaitFn /*wait_fn*/) {
    return do_start_print(agent, std::move(params), std::move(update_fn),
                          std::move(cancel_fn), "start_print");
}

int bambu_network_start_local_print_with_record(void* agent, PrintParams params,
                                                OnUpdateStatusFn update_fn,
                                                WasCancelledFn cancel_fn,
                                                OnWaitFn /*wait_fn*/) {
    return do_start_print(agent, std::move(params), std::move(update_fn),
                          std::move(cancel_fn), "start_local_print_with_record");
}

int bambu_network_start_send_gcode_to_sdcard(void* agent, PrintParams params,
                                             OnUpdateStatusFn update_fn,
                                             WasCancelledFn cancel_fn,
                                             OnWaitFn /*wait_fn*/) {
    return do_start_print(agent, std::move(params), std::move(update_fn),
                          std::move(cancel_fn), "start_send_gcode_to_sdcard");
}

int bambu_network_start_local_print(void* agent, PrintParams params,
                                    OnUpdateStatusFn update_fn,
                                    WasCancelledFn cancel_fn) {
    return do_start_print(agent, std::move(params), std::move(update_fn),
                          std::move(cancel_fn), "start_local_print");
}

int bambu_network_start_sdcard_print(void* agent, PrintParams params,
                                     OnUpdateStatusFn update_fn,
                                     WasCancelledFn cancel_fn) {
    return do_start_print(agent, std::move(params), std::move(update_fn),
                          std::move(cancel_fn), "start_sdcard_print");
}

// ---------------------------------------------------------------------------
// Cloud catalog (presets / settings / tasks / oss / model-mall) — all
// return success with empty payloads. The GUI's cloud panels will look
// empty but won't crash.
// ---------------------------------------------------------------------------

int bambu_network_get_user_presets(void* /*agent*/, std::map<std::string, std::map<std::string, std::string>>* user_presets) {
    if (user_presets) user_presets->clear();
    return BAMBU_NETWORK_SUCCESS;
}

std::string bambu_network_request_setting_id(void* /*agent*/, std::string /*name*/, std::map<std::string, std::string>* /*values*/, unsigned int* http_code) {
    if (http_code) *http_code = 200;
    return "";
}

int bambu_network_put_setting(void* /*agent*/, std::string /*setting_id*/, std::string /*name*/, std::map<std::string, std::string>* /*values*/, unsigned int* http_code) {
    if (http_code) *http_code = 200;
    return BAMBU_NETWORK_SUCCESS;
}

int bambu_network_get_setting_list(void* /*agent*/, std::string /*ver*/, ProgressFn /*pro*/, WasCancelledFn /*cancel*/) { return BAMBU_NETWORK_SUCCESS; }
int bambu_network_get_setting_list2(void* /*agent*/, std::string /*ver*/, CheckFn /*chk*/, ProgressFn /*pro*/, WasCancelledFn /*cancel*/) { return BAMBU_NETWORK_SUCCESS; }
int bambu_network_delete_setting(void* /*agent*/, std::string /*setting_id*/) { return BAMBU_NETWORK_SUCCESS; }

std::string bambu_network_get_studio_info_url(void* /*agent*/) { return ""; }
int bambu_network_set_extra_http_header(void* /*agent*/, std::map<std::string, std::string> /*hdrs*/) { return BAMBU_NETWORK_SUCCESS; }

#define EMPTY_HTTP(NAME, ...)                                                  \
    int bambu_network_##NAME(void* /*agent*/, ##__VA_ARGS__,                   \
                              unsigned int* http_code, std::string* http_body) {\
        if (http_code) *http_code = 200;                                       \
        if (http_body) *http_body = "{}";                                      \
        return BAMBU_NETWORK_SUCCESS;                                          \
    }

int bambu_network_get_my_message(void* /*a*/, int /*type*/, int /*after*/, int /*limit*/, unsigned int* http_code, std::string* http_body) {
    if (http_code) *http_code = 200; if (http_body) *http_body = "{\"messages\":[]}"; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_check_user_task_report(void* /*a*/, int* task_id, bool* printable) {
    if (task_id) *task_id = 0; if (printable) *printable = false; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_user_print_info(void* /*a*/, unsigned int* http_code, std::string* http_body) {
    if (http_code) *http_code = 200; if (http_body) *http_body = "{}"; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_user_tasks(void* /*a*/, TaskQueryParams /*p*/, std::string* http_body) {
    if (http_body) *http_body = "{\"hits\":[]}"; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_printer_firmware(void* /*a*/, std::string /*dev_id*/, unsigned* http_code, std::string* http_body) {
    if (http_code) *http_code = 200; if (http_body) *http_body = "{}"; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_task_plate_index(void* /*a*/, std::string /*task_id*/, int* plate_index) {
    if (plate_index) *plate_index = 0; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_user_info(void* /*a*/, int* identifier) {
    if (identifier) *identifier = 0; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_request_bind_ticket(void* /*a*/, std::string* ticket) {
    if (ticket) *ticket = ""; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_subtask_info(void* /*a*/, std::string /*subtask_id*/, std::string* task_json,
                                   unsigned int* http_code, std::string* http_body) {
    if (task_json) *task_json = "{}"; if (http_code) *http_code = 200;
    if (http_body) *http_body = "{}"; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_slice_info(void* /*a*/, std::string /*project_id*/, std::string /*profile_id*/,
                                 int /*plate_index*/, std::string* slice_json) {
    if (slice_json) *slice_json = "{}"; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_query_bind_status(void* /*a*/, std::vector<std::string> /*ids*/,
                                    unsigned int* http_code, std::string* http_body) {
    if (http_code) *http_code = 200; if (http_body) *http_body = "[]"; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_modify_printer_name(void* /*a*/, std::string /*dev_id*/, std::string /*name*/) {
    return BAMBU_NETWORK_SUCCESS;
}

int bambu_network_get_camera_url(void* /*a*/, std::string dev_id,
                                 std::function<void(std::string)> callback) {
    if (callback) callback("rtsps://" + dev_id + ":322/streaming/live/1");
    return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_camera_url_for_golive(void* /*a*/, std::string /*dev_id*/, std::string /*sdev_id*/,
                                            std::function<void(std::string)> callback) {
    if (callback) callback("");
    return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_design_staffpick(void* /*a*/, int /*offset*/, int /*limit*/,
                                       std::function<void(std::string)> callback) {
    if (callback) callback("{\"hits\":[]}");
    return BAMBU_NETWORK_SUCCESS;
}

int bambu_network_start_publish(void* /*a*/, PublishParams /*p*/, OnUpdateStatusFn /*upd*/,
                                WasCancelledFn /*cancel*/, std::string* out) {
    if (out) *out = "";
    return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_model_publish_url(void* /*a*/, std::string* url) {
    if (url) *url = ""; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_subtask(void* /*a*/, BBLModelTask* /*task*/, OnGetSubTaskFn /*fn*/) {
    return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_model_mall_home_url(void* /*a*/, std::string* url) {
    if (url) *url = ""; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_model_mall_detail_url(void* /*a*/, std::string* url, std::string /*id*/) {
    if (url) *url = ""; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_my_profile(void* /*a*/, std::string /*token*/, unsigned int* http_code, std::string* http_body) {
    if (http_code) *http_code = 200; if (http_body) *http_body = "{}"; return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_my_token(void* /*a*/, std::string /*ticket*/, unsigned int* http_code, std::string* http_body) {
    if (http_code) *http_code = 200; if (http_body) *http_body = "{}"; return BAMBU_NETWORK_SUCCESS;
}

// Telemetry — all no-ops.
int bambu_network_track_enable(void* /*a*/, bool /*enable*/) { return BAMBU_NETWORK_SUCCESS; }
int bambu_network_track_remove_files(void* /*a*/) { return BAMBU_NETWORK_SUCCESS; }
int bambu_network_track_event(void* /*a*/, std::string /*key*/, std::string /*content*/) { return BAMBU_NETWORK_SUCCESS; }
int bambu_network_track_header(void* /*a*/, std::string /*hdr*/) { return BAMBU_NETWORK_SUCCESS; }
int bambu_network_track_update_property(void* /*a*/, std::string /*name*/, std::string /*value*/, std::string /*type*/) { return BAMBU_NETWORK_SUCCESS; }
int bambu_network_track_get_property(void* /*a*/, std::string /*name*/, std::string& value, std::string /*type*/) {
    value = ""; return BAMBU_NETWORK_SUCCESS;
}

// Mall ratings + OSS — empty success.
int bambu_network_put_model_mall_rating(void* /*a*/, int /*rating_id*/, int /*score*/, std::string /*content*/,
                                            std::vector<std::string> /*images*/, unsigned int& http_code, std::string& http_error) {
    http_code = 200; http_error.clear(); return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_oss_config(void* /*a*/, std::string& config, std::string /*country_code*/, unsigned int& http_code, std::string& http_error) {
    config.clear(); http_code = 200; http_error.clear(); return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_put_rating_picture_oss(void* /*a*/, std::string& /*config*/, std::string& /*pic_oss_path*/,
                                         std::string /*model_id*/, int /*profile_id*/, unsigned int& http_code, std::string& http_error) {
    http_code = 200; http_error.clear(); return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_model_mall_rating(void* /*a*/, int /*job_id*/, std::string& rating_result, unsigned int& http_code, std::string& http_error) {
    rating_result.clear(); http_code = 200; http_error.clear(); return BAMBU_NETWORK_SUCCESS;
}

// Music wall + HMS snapshot — async callbacks invoked synchronously with empty data.
int bambu_network_get_mw_user_preference(void* /*a*/, std::function<void(std::string)> callback) {
    if (callback) callback("{}"); return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_mw_user_4ulist(void* /*a*/, int /*seed*/, int /*limit*/, std::function<void(std::string)> callback) {
    if (callback) callback("[]"); return BAMBU_NETWORK_SUCCESS;
}
int bambu_network_get_hms_snapshot(void* /*a*/, std::string& /*dev_id*/, std::string& /*file_name*/,
                                    std::function<void(std::string, int)> callback) {
    if (callback) callback("", 0); return BAMBU_NETWORK_SUCCESS;
}

} // extern "C"
