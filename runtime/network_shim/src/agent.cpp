// agent.cpp — bridge between BambuStudio NetworkAgent C ABI and the
// JSON-line bridge daemon. Binds incoming bridge events to the host's
// std::function callbacks (registered via bambu_network_set_on_*_fn),
// marshalling each call onto the host's main thread via the registered
// QueueOnMainFn.

#include "shim_internal.hpp"

namespace x2d_shim {

// Helper: snapshot a std::function under cb_mu; call it directly or via
// the host's queue_on_main if one is registered.
template <class Cb, class Apply>
static void marshal_call(Agent* a, Cb cb, Apply&& apply) {
    if (!cb) return;
    BBL::QueueOnMainFn marshal;
    {
        std::lock_guard<std::mutex> g(a->cb_mu);
        marshal = a->queue_on_main;
    }
    auto invoke = [cb, apply = std::forward<Apply>(apply)]() {
        try { apply(cb); }
        catch (const std::exception& e) {
            log_warn(std::string("host callback raised: ") + e.what());
        }
    };
    if (marshal) {
        try { marshal(invoke); }
        catch (const std::exception& e) {
            log_warn(std::string("queue_on_main raised: ") + e.what() +
                     " — calling inline");
            invoke();
        }
    } else {
        invoke();
    }
}

void register_bridge_event_handlers(Agent* a) {
    if (!a || !a->bridge) return;

    a->bridge->on_event("ssdp_msg", [a](const json& evt) {
        std::string js = evt.value("data", json::object()).value("json", "");
        if (js.empty()) return;
        BBL::OnMsgArrivedFn cb;
        {
            std::lock_guard<std::mutex> g(a->cb_mu);
            cb = a->on_ssdp_msg;
        }
        marshal_call(a, cb, [js](const auto& fn) { fn(js); });
    });

    a->bridge->on_event("local_message", [a](const json& evt) {
        auto data = evt.value("data", json::object());
        std::string dev_id = data.value("dev_id", "");
        std::string msg    = data.value("msg",    "");
        BBL::OnMessageFn cb;
        {
            std::lock_guard<std::mutex> g(a->cb_mu);
            cb = a->on_local_message;
        }
        marshal_call(a, cb, [dev_id, msg](const auto& fn) {
            fn(dev_id, msg);
        });
    });

    a->bridge->on_event("local_connect", [a](const json& evt) {
        auto data = evt.value("data", json::object());
        int status = data.value("status", 0);
        std::string dev_id = data.value("dev_id", "");
        std::string msg    = data.value("msg",    "");
        BBL::OnLocalConnectedFn cb;
        BBL::OnPrinterConnectedFn cb2;
        {
            std::lock_guard<std::mutex> g(a->cb_mu);
            cb  = a->on_local_connect;
            cb2 = a->on_printer_connected;
        }
        marshal_call(a, cb, [status, dev_id, msg](const auto& fn) {
            fn(status, dev_id, msg);
        });
        if (status == 0) {
            // Mirror to the printer-connected callback too — the GUI
            // uses both depending on which panel is visible.
            std::string topic = "device/" + dev_id + "/report";
            marshal_call(a, cb2, [topic](const auto& fn) { fn(topic); });
        }
    });

    a->bridge->on_event("printer_connected", [a](const json& evt) {
        std::string topic = evt.value("data", json::object())
                              .value("topic", "");
        BBL::OnPrinterConnectedFn cb;
        {
            std::lock_guard<std::mutex> g(a->cb_mu);
            cb = a->on_printer_connected;
        }
        marshal_call(a, cb, [topic](const auto& fn) { fn(topic); });
    });

    a->bridge->on_event("subscribe_failed", [a](const json& evt) {
        std::string topic = evt.value("data", json::object())
                              .value("topic", "");
        BBL::GetSubscribeFailureFn cb;
        {
            std::lock_guard<std::mutex> g(a->cb_mu);
            cb = a->on_subscribe_failure;
        }
        marshal_call(a, cb, [topic](const auto& fn) { fn(topic); });
    });

    a->bridge->on_event("http_error", [a](const json& evt) {
        auto data = evt.value("data", json::object());
        unsigned code = data.value("http_code", 0u);
        std::string body = data.value("body", "");
        BBL::OnHttpErrorFn cb;
        {
            std::lock_guard<std::mutex> g(a->cb_mu);
            cb = a->on_http_error;
        }
        marshal_call(a, cb, [code, body](const auto& fn) { fn(code, body); });
    });

    a->bridge->on_event("print_status", [a](const json& evt) {
        auto data = evt.value("data", json::object());
        int status = data.value("status", 0);
        int code   = data.value("code",   0);
        std::string msg = data.value("msg", "");
        BBL::OnUpdateStatusFn cb;
        {
            std::lock_guard<std::mutex> g(a->print_mu);
            cb = a->active_print_status;
        }
        marshal_call(a, cb, [status, code, msg](const auto& fn) {
            fn(status, code, msg);
        });
    });
}

// Translates a bridge response into a NetworkAgent error code. ok:true
// returns BAMBU_NETWORK_SUCCESS (0). ok:false maps the bridge's `code`
// field through unchanged (it's already in BAMBU_NETWORK_ERR_* space).
int bridge_rc(const json& reply, int default_err = BAMBU_NETWORK_ERR_INVALID_RESULT) {
    if (reply.value("ok", false)) return BAMBU_NETWORK_SUCCESS;
    auto err = reply.value("error", json::object());
    int code = err.value("code", default_err);
    log_warn("bridge op failed: " + err.value("message", std::string{}) +
             " (code " + std::to_string(code) + ")");
    return code;
}

// Serialise a PrintParams struct into the JSON shape the bridge expects.
// Field names match the keys in PROTOCOL.md → start_local_print.
json print_params_to_json(const BBL::PrintParams& p) {
    return json{
        {"dev_id",                p.dev_id},
        {"task_name",             p.task_name},
        {"project_name",          p.project_name},
        {"preset_name",           p.preset_name},
        {"filename",              p.filename},
        {"config_filename",       p.config_filename},
        {"plate_index",           p.plate_index},
        {"ftp_folder",            p.ftp_folder},
        {"ftp_file",              p.ftp_file},
        {"ftp_file_md5",          p.ftp_file_md5},
        {"nozzle_mapping",        p.nozzle_mapping},
        {"ams_mapping",           p.ams_mapping},
        {"ams_mapping2",          p.ams_mapping2},
        {"ams_mapping_info",      p.ams_mapping_info},
        {"nozzles_info",          p.nozzles_info},
        {"connection_type",       p.connection_type},
        {"comments",              p.comments},
        {"origin_profile_id",     p.origin_profile_id},
        {"stl_design_id",         p.stl_design_id},
        {"origin_model_id",       p.origin_model_id},
        {"print_type",            p.print_type},
        {"dst_file",              p.dst_file},
        {"dev_name",              p.dev_name},
        {"dev_ip",                p.dev_ip},
        {"use_ssl_for_ftp",       p.use_ssl_for_ftp},
        {"use_ssl_for_mqtt",      p.use_ssl_for_mqtt},
        {"username",              p.username},
        {"password",              p.password},
        {"task_bed_leveling",     p.task_bed_leveling},
        {"task_flow_cali",        p.task_flow_cali},
        {"task_vibration_cali",   p.task_vibration_cali},
        {"task_layer_inspect",    p.task_layer_inspect},
        {"task_record_timelapse", p.task_record_timelapse},
        {"task_use_ams",          p.task_use_ams},
        {"task_bed_type",         p.task_bed_type},
        {"extra_options",         p.extra_options},
    };
}

} // namespace x2d_shim
