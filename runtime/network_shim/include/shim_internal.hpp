// shim_internal.hpp — internal types for libbambu_networking.so (Termux
// aarch64 stub that proxies to x2d_bridge over a Unix-domain socket).
//
// See PROTOCOL.md for the wire format. Only consumed by the .cpp files
// inside runtime/network_shim/.
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "../../../bs-bionic/src/nlohmann/json.hpp"

// Subset of BambuStudio's typedefs we need — copied verbatim from
// `src/slic3r/Utils/bambu_networking.hpp`. We can't `#include` that
// header directly because it pulls in the entire BambuStudio include
// tree (libslic3r/ProjectTask, boost log macros, etc.). Keep these in
// sync if upstream changes them; mismatches surface at link-load time
// when BambuStudio passes a struct of a different size to one of our
// entry points.
namespace BBL {

#define BAMBU_NETWORK_SUCCESS                           0
#define BAMBU_NETWORK_ERR_INVALID_HANDLE               -1
#define BAMBU_NETWORK_ERR_CONNECT_FAILED               -2
#define BAMBU_NETWORK_ERR_DISCONNECT_FAILED            -3
#define BAMBU_NETWORK_ERR_SEND_MSG_FAILED              -4
#define BAMBU_NETWORK_ERR_INVALID_RESULT              -19
#define BAMBU_NETWORK_ERR_FTP_UPLOAD_FAILED           -20
#define BAMBU_NETWORK_ERR_TIMEOUT                     -17
#define BAMBU_NETWORK_ERR_FILE_NOT_EXIST              -14

using OnUserLoginFn        = std::function<void(int, bool)>;
using OnPrinterConnectedFn = std::function<void(std::string)>;
using OnLocalConnectedFn   = std::function<void(int, std::string, std::string)>;
using OnServerConnectedFn  = std::function<void(int, int)>;
using OnMessageFn          = std::function<void(std::string, std::string)>;
using OnHttpErrorFn        = std::function<void(unsigned, std::string)>;
using GetCountryCodeFn     = std::function<std::string()>;
using GetSubscribeFailureFn= std::function<void(std::string)>;
using OnUpdateStatusFn     = std::function<void(int, int, std::string)>;
using WasCancelledFn       = std::function<bool()>;
using OnWaitFn             = std::function<bool(int, std::string)>;
using OnMsgArrivedFn       = std::function<void(std::string)>;
using QueueOnMainFn        = std::function<void(std::function<void()>)>;
using ProgressFn           = std::function<void(int)>;
using CheckFn              = std::function<bool(std::map<std::string, std::string>)>;
using OnServerErrFn        = std::function<void(std::string, int)>;
using OnGetSubTaskFn       = std::function<void(int, std::string)>;

// PrintParams MUST match the host's struct layout exactly. We never
// access individual fields by name from the shim — we only forward the
// whole thing as JSON to the bridge — but we need the *size* to be right
// because BambuStudio passes by value.
struct PrintParams {
    std::string     dev_id;
    std::string     task_name;
    std::string     project_name;
    std::string     preset_name;
    std::string     filename;
    std::string     config_filename;
    int             plate_index;
    std::string     ftp_folder;
    std::string     ftp_file;
    std::string     ftp_file_md5;
    std::string     nozzle_mapping;
    std::string     ams_mapping;
    std::string     ams_mapping2;
    std::string     ams_mapping_info;
    std::string     nozzles_info;
    std::string     connection_type;
    std::string     comments;
    int             origin_profile_id = 0;
    int             stl_design_id = 0;
    std::string     origin_model_id;
    std::string     print_type;
    std::string     dst_file;
    std::string     dev_name;
    std::string     dev_ip;
    bool            use_ssl_for_ftp;
    bool            use_ssl_for_mqtt;
    std::string     username;
    std::string     password;
    bool            task_bed_leveling;
    bool            task_flow_cali;
    bool            task_vibration_cali;
    bool            task_layer_inspect;
    bool            task_record_timelapse;
    bool            task_timelapse_use_internal;
    bool            task_use_ams;
    std::string     task_bed_type;
    std::string     extra_options;
    int             auto_bed_leveling{0};
    int             auto_flow_cali{0};
    int             auto_offset_cali{0};
    int             extruder_cali_manual_mode{-1};
    bool            task_ext_change_assist;
    bool            try_emmc_print;
};

struct PublishParams {
    std::string     project_name;
    std::string     project_3mf_file;
    std::string     preset_name;
    std::string     project_model_id;
    std::string     design_id;
    std::string     config_filename;
};

struct TaskQueryParams {
    std::string dev_id;
    int status = 0;
    int offset = 0;
    int limit = 20;
};

struct detectResult {
    std::string    result_msg;
    std::string    command;
    std::string    dev_id;
    std::string    model_id;
    std::string    dev_name;
    std::string    version;
    std::string    bind_state;
    std::string    connect_type;
};

struct BBLModelTask {};   // opaque; we never read fields

} // namespace BBL


namespace x2d_shim {

using json = nlohmann::json;

// One pending request awaiting a `rsp` from the bridge.
struct PendingResponse {
    std::mutex mu;
    std::condition_variable cv;
    bool ready = false;
    json response;        // either {ok:true,result:...} or {ok:false,error:...}
};

class BridgeClient;

// Per-process Agent state. BambuStudio stores the void* we return from
// bambu_network_create_agent and passes it back on every call.
struct Agent {
    Agent();
    ~Agent();

    // Callbacks registered by the host (set_on_*_fn). All assignments must
    // be guarded by cb_mu because the worker thread can read them
    // concurrently while delivering events.
    std::mutex cb_mu;
    BBL::OnMsgArrivedFn         on_ssdp_msg;
    BBL::OnUserLoginFn          on_user_login;
    BBL::OnPrinterConnectedFn   on_printer_connected;
    BBL::OnServerConnectedFn    on_server_connected;
    BBL::OnHttpErrorFn          on_http_error;
    BBL::GetCountryCodeFn       get_country_code;
    BBL::GetSubscribeFailureFn  on_subscribe_failure;
    BBL::OnMessageFn            on_message;
    BBL::OnMessageFn            on_user_message;
    BBL::OnLocalConnectedFn     on_local_connect;
    BBL::OnMessageFn            on_local_message;
    BBL::QueueOnMainFn          queue_on_main;
    BBL::OnServerErrFn          on_server_err;

    std::string log_dir;
    std::string config_dir;
    std::string country_code = "Others";
    std::string cert_folder;
    std::string cert_filename;

    std::unique_ptr<BridgeClient> bridge;

    // Active print job's update callback. Stored at start_local_print
    // time so print_status events can find it. Only one print in flight.
    BBL::OnUpdateStatusFn        active_print_status;
    BBL::WasCancelledFn          active_print_cancel;
    std::mutex                   print_mu;
};


class BridgeClient {
public:
    BridgeClient();
    ~BridgeClient();

    // Connects to bridge. Spawns x2d_bridge.py serve as a subprocess if
    // the socket path doesn't exist yet. Returns false on failure;
    // callers should treat that as "shim cannot proceed". Idempotent.
    bool connect();

    // Closes the socket and joins the worker thread. Idempotent.
    void disconnect();

    // Synchronous request → response. Returns the bridge's reply JSON
    // (the full {ok, result|error} object). Throws std::runtime_error
    // on socket failure. Times out per-request via timeout_ms.
    json request(const std::string& op, json args, int timeout_ms = 8000);

    // Owner registers a handler for events of a given name. The handler
    // runs on the WORKER thread; the caller is responsible for
    // marshalling to GTK via Agent::queue_on_main.
    using EventHandler = std::function<void(const json&)>;
    void on_event(const std::string& name, EventHandler handler);

    bool is_connected() const { return connected_.load(); }

private:
    void worker_loop();
    bool send_line(const std::string& line);
    void process_message(const json& msg);
    void wake_pending(uint64_t id, json response);
    bool ensure_socket();
    bool spawn_bridge_subprocess();

    std::string                                   sock_path_;
    int                                           fd_ = -1;
    std::atomic<bool>                             connected_{false};
    std::atomic<bool>                             stop_{false};
    std::thread                                   worker_;
    std::mutex                                    write_mu_;

    std::mutex                                    pending_mu_;
    std::map<uint64_t, std::shared_ptr<PendingResponse>> pending_;
    std::atomic<uint64_t>                         next_id_{1};

    std::mutex                                    handlers_mu_;
    std::map<std::string, EventHandler>           handlers_;
};

// Helpers
std::string default_socket_path();
void log_info(const std::string& msg);
void log_warn(const std::string& msg);
void log_err(const std::string& msg);

// Shared global Agent registry — bambu_network_create_agent allocates
// an Agent, we hand back a void* pointer; bambu_network_destroy_agent
// frees it. The shim doesn't keep its own list (BambuStudio currently
// only constructs one), but we sanity-check the pointer before deref.
extern std::mutex g_agent_mu;
extern std::vector<Agent*> g_agents;

bool is_valid_agent(void* p);

} // namespace x2d_shim
