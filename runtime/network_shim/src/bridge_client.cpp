// bridge_client.cpp — Unix-domain socket client for the x2d bridge.
//
// Owns one TCP-stream-style socket and one worker thread that drains
// incoming JSON-line messages, dispatches `rsp` replies to whoever's
// blocked in request(), and `evt` async pushes to handlers registered
// via on_event(). All sends serialise through write_mu_ so two threads
// can safely call request() concurrently.

#include "shim_internal.hpp"

#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

namespace x2d_shim {

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

std::string default_socket_path() {
    if (const char* env = std::getenv("X2D_BRIDGE_SOCK"); env && *env) {
        return env;
    }
    const char* home = std::getenv("HOME");
    if (!home) home = "/tmp";
    return std::string(home) + "/.x2d/bridge.sock";
}

static void log_to_stderr(const char* level, const std::string& msg) {
    std::fprintf(stderr, "[x2d-shim:%s] %s\n", level, msg.c_str());
    std::fflush(stderr);
}

void log_info(const std::string& msg) { log_to_stderr("info",  msg); }
void log_warn(const std::string& msg) { log_to_stderr("warn",  msg); }
void log_err (const std::string& msg) { log_to_stderr("error", msg); }

// ---------------------------------------------------------------------------
// BridgeClient
// ---------------------------------------------------------------------------

BridgeClient::BridgeClient()
    : sock_path_(default_socket_path()) {}

BridgeClient::~BridgeClient() { disconnect(); }

bool BridgeClient::ensure_socket() {
    struct stat st{};
    if (::stat(sock_path_.c_str(), &st) == 0) return true;
    return spawn_bridge_subprocess();
}

bool BridgeClient::spawn_bridge_subprocess() {
    log_info("bridge socket missing at " + sock_path_ +
             "; spawning x2d_bridge.py serve");
    // The shim doesn't know its install prefix at runtime. We try the
    // canonical script path first, then fall back to PATH lookup. The
    // child uses double-fork so we don't have to wait() for it.
    pid_t pid = ::fork();
    if (pid < 0) {
        log_err(std::string("fork() failed: ") + std::strerror(errno));
        return false;
    }
    if (pid == 0) {
        ::setsid();
        pid_t pid2 = ::fork();
        if (pid2 != 0) ::_exit(0);
        // Inside grandchild: redirect stdio to /dev/null so paho/log
        // chatter doesn't leak into the host's terminal.
        int dnull = ::open("/dev/null", O_RDWR);
        if (dnull >= 0) {
            ::dup2(dnull, 0); ::dup2(dnull, 1); ::dup2(dnull, 2);
            if (dnull > 2) ::close(dnull);
        }
        const char* candidates[] = {
            "/data/data/com.termux/files/home/git/x2d/x2d_bridge.py",
            "/usr/local/lib/x2d/x2d_bridge.py",
            "/data/data/com.termux/files/usr/lib/x2d/x2d_bridge.py",
            nullptr
        };
        // Prefer python3.12 first because that's where Termux installs the
        // optional `cryptography` + `paho-mqtt` packages that x2d_bridge
        // imports; the bare `python3` symlink might be a slimmer build
        // missing them. Fall back to python3 so a clean install with
        // `pip install -t ...python3` still works.
        for (auto** c = candidates; *c; ++c) {
            if (::access(*c, R_OK) == 0) {
                ::execlp("python3.12", "python3.12", *c, "serve",
                         "--sock", sock_path_.c_str(), (char*)nullptr);
                ::execlp("python3", "python3", *c, "serve", "--sock",
                         sock_path_.c_str(), (char*)nullptr);
            }
        }
        // Last resort: hope it's on PATH
        ::execlp("x2d_bridge", "x2d_bridge", "serve",
                 "--sock", sock_path_.c_str(), (char*)nullptr);
        ::_exit(127);
    }
    // Parent: reap the immediate child to avoid a zombie.
    int status = 0;
    ::waitpid(pid, &status, 0);
    // Spin briefly until the socket appears.
    for (int i = 0; i < 50; ++i) {  // 50 * 100ms = 5s
        struct stat st{};
        if (::stat(sock_path_.c_str(), &st) == 0) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    log_err("bridge subprocess did not create socket within 5s");
    return false;
}

bool BridgeClient::connect() {
    if (connected_.load()) return true;
    if (!ensure_socket()) return false;

    fd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd_ < 0) {
        log_err(std::string("socket(): ") + std::strerror(errno));
        return false;
    }
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    if (sock_path_.size() >= sizeof(addr.sun_path)) {
        log_err("sock_path too long: " + sock_path_);
        ::close(fd_); fd_ = -1; return false;
    }
    std::strncpy(addr.sun_path, sock_path_.c_str(), sizeof(addr.sun_path) - 1);
    if (::connect(fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        log_err(std::string("connect(): ") + std::strerror(errno));
        ::close(fd_); fd_ = -1; return false;
    }

    connected_.store(true);
    stop_.store(false);
    worker_ = std::thread([this] { this->worker_loop(); });

    // Send hello and wait for the reply on the worker thread (it'll
    // process the reply and wake us up).
    json reply;
    try {
        reply = request("hello",
                        json{{"abi", 1}, {"shim_version", 1}},
                        2000);
    } catch (const std::exception& e) {
        log_err(std::string("hello failed: ") + e.what());
        disconnect();
        return false;
    }
    if (!reply.value("ok", false)) {
        log_err("hello response not ok: " + reply.dump());
        disconnect();
        return false;
    }
    log_info("bridge handshake ok: " + reply.value("result", json{}).dump());
    return true;
}

void BridgeClient::disconnect() {
    if (!connected_.exchange(false)) return;
    stop_.store(true);
    ::shutdown(fd_, SHUT_RDWR);
    if (worker_.joinable()) worker_.join();
    if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
    // Wake every blocked requester with a synthetic disconnect error.
    std::lock_guard<std::mutex> g(pending_mu_);
    for (auto& [id, p] : pending_) {
        std::lock_guard<std::mutex> g2(p->mu);
        p->response = json{{"ok", false},
                           {"error", {{"code", BAMBU_NETWORK_ERR_DISCONNECT_FAILED},
                                      {"message", "bridge disconnected"}}}};
        p->ready = true;
        p->cv.notify_all();
    }
    pending_.clear();
}

bool BridgeClient::send_line(const std::string& line) {
    std::string buf = line + "\n";
    std::lock_guard<std::mutex> g(write_mu_);
    const char* p = buf.data();
    size_t left = buf.size();
    while (left > 0) {
        ssize_t n = ::send(fd_, p, left, MSG_NOSIGNAL);
        if (n > 0) { p += n; left -= static_cast<size_t>(n); continue; }
        if (n < 0 && errno == EINTR) continue;
        log_err(std::string("send(): ") + std::strerror(errno));
        return false;
    }
    return true;
}

void BridgeClient::wake_pending(uint64_t id, json response) {
    std::shared_ptr<PendingResponse> p;
    {
        std::lock_guard<std::mutex> g(pending_mu_);
        auto it = pending_.find(id);
        if (it == pending_.end()) return;
        p = it->second;
        pending_.erase(it);
    }
    std::lock_guard<std::mutex> g(p->mu);
    p->response = std::move(response);
    p->ready = true;
    p->cv.notify_all();
}

void BridgeClient::process_message(const json& msg) {
    auto kind = msg.value("kind", "");
    if (kind == "rsp") {
        uint64_t id = msg.value("id", 0ull);
        if (id == 0) return;
        wake_pending(id, msg);
    } else if (kind == "evt") {
        auto name = msg.value("name", "");
        EventHandler h;
        {
            std::lock_guard<std::mutex> g(handlers_mu_);
            auto it = handlers_.find(name);
            if (it != handlers_.end()) h = it->second;
        }
        if (h) {
            try { h(msg); }
            catch (const std::exception& e) {
                log_err("event handler raised: " + std::string(e.what()));
            }
        }
    }
}

void BridgeClient::worker_loop() {
    std::string buf;
    char tmp[4096];
    while (!stop_.load()) {
        ssize_t n = ::recv(fd_, tmp, sizeof(tmp), 0);
        if (n > 0) {
            buf.append(tmp, static_cast<size_t>(n));
            for (;;) {
                auto nl = buf.find('\n');
                if (nl == std::string::npos) break;
                std::string line = buf.substr(0, nl);
                buf.erase(0, nl + 1);
                if (line.empty()) continue;
                try {
                    auto msg = json::parse(line);
                    process_message(msg);
                } catch (const std::exception& e) {
                    log_warn("bad json from bridge: " + std::string(e.what()));
                }
            }
            continue;
        }
        if (n == 0) {
            log_warn("bridge socket closed by peer");
            break;
        }
        if (errno == EINTR) continue;
        log_warn(std::string("recv(): ") + std::strerror(errno));
        break;
    }
    connected_.store(false);
    // Don't disconnect() from here — it joins the same thread we're on.
    // Owner will do that on next request() / explicit disconnect().
    std::lock_guard<std::mutex> g(pending_mu_);
    for (auto& [id, p] : pending_) {
        std::lock_guard<std::mutex> g2(p->mu);
        p->response = json{{"ok", false},
                           {"error", {{"code", BAMBU_NETWORK_ERR_DISCONNECT_FAILED},
                                      {"message", "bridge dropped"}}}};
        p->ready = true;
        p->cv.notify_all();
    }
    pending_.clear();
}

json BridgeClient::request(const std::string& op, json args, int timeout_ms) {
    if (!connected_.load()) {
        return json{{"ok", false},
                    {"error", {{"code", BAMBU_NETWORK_ERR_DISCONNECT_FAILED},
                               {"message", "not connected"}}}};
    }
    uint64_t id = next_id_.fetch_add(1);
    auto p = std::make_shared<PendingResponse>();
    {
        std::lock_guard<std::mutex> g(pending_mu_);
        pending_[id] = p;
    }
    json req = {
        {"kind", "req"}, {"id", id}, {"op", op}, {"args", std::move(args)}
    };
    if (!send_line(req.dump())) {
        std::lock_guard<std::mutex> g(pending_mu_);
        pending_.erase(id);
        return json{{"ok", false},
                    {"error", {{"code", BAMBU_NETWORK_ERR_SEND_MSG_FAILED},
                               {"message", "send failed"}}}};
    }
    std::unique_lock<std::mutex> lk(p->mu);
    if (!p->cv.wait_for(lk, std::chrono::milliseconds(timeout_ms),
                         [&]{ return p->ready; })) {
        // Timed out — we leave the pending entry in place; if a late
        // response arrives the wake_pending call will harmlessly find
        // the slot empty. Actually, drop it to bound memory.
        std::lock_guard<std::mutex> g(pending_mu_);
        pending_.erase(id);
        return json{{"ok", false},
                    {"error", {{"code", BAMBU_NETWORK_ERR_TIMEOUT},
                               {"message", "bridge response timeout"}}}};
    }
    return p->response;
}

void BridgeClient::on_event(const std::string& name, EventHandler handler) {
    std::lock_guard<std::mutex> g(handlers_mu_);
    handlers_[name] = std::move(handler);
}

// ---------------------------------------------------------------------------
// Agent — global registry
// ---------------------------------------------------------------------------

std::mutex g_agent_mu;
std::vector<Agent*> g_agents;

Agent::Agent() {}
Agent::~Agent() {
    if (bridge) bridge->disconnect();
}

bool is_valid_agent(void* p) {
    if (!p) return false;
    std::lock_guard<std::mutex> g(g_agent_mu);
    for (auto* a : g_agents) if (a == p) return true;
    return false;
}

} // namespace x2d_shim
