#define FG_TRACE_LOADER_OFFLINE_TEST
#include "trace_loader.mm"

#include <sys/wait.h>

#include <fstream>
#include <iostream>
#include <vector>

namespace {

constexpr char kToken[] = "0123456789abcdef0123456789abcdef";

struct DirectoryIdentity {
    uint64_t device;
    uint64_t inode;
};

DirectoryIdentity identity_for(const std::string& path) {
    struct stat info{};
    if (stat(path.c_str(), &info) != 0 || !S_ISDIR(info.st_mode)) {
        std::perror("stat test trace directory");
        std::exit(2);
    }
    return {static_cast<uint64_t>(info.st_dev), static_cast<uint64_t>(info.st_ino)};
}

void configure(const std::string& directory, DirectoryIdentity identity,
               pid_t target_pid, const char* token = kToken) {
    const std::string target = std::to_string(target_pid);
    const std::string device = std::to_string(identity.device);
    const std::string inode = std::to_string(identity.inode);
    if (setenv("FG_B_TRACE_DIRECTORY", directory.c_str(), 1) != 0
            || setenv("FG_B_TRACE_DIRECTORY_DEVICE", device.c_str(), 1) != 0
            || setenv("FG_B_TRACE_DIRECTORY_INODE", inode.c_str(), 1) != 0
            || setenv("FG_B_TARGET_UNIX_PID", target.c_str(), 1) != 0
            || setenv("FG_B_EXPERIMENT_TOKEN", token, 1) != 0) {
        std::perror("setenv test trace identity");
        std::exit(2);
    }
}

std::string trace_path(const std::string& directory, pid_t pid) {
    return directory + "/host-trace." + std::to_string(pid) + ".jsonl";
}

bool read_file(const std::string& path, std::string& contents) {
    std::ifstream input(path, std::ios::binary);
    if (!input) return false;
    contents.assign(std::istreambuf_iterator<char>(input),
                    std::istreambuf_iterator<char>());
    return true;
}

bool all_rows_have_identity(const std::string& contents, pid_t host_pid,
                            pid_t target_pid, const char* role) {
    if (contents.empty() || contents.back() != '\n') return false;
    const std::string host = "\"host_pid\":" + std::to_string(host_pid);
    const std::string target = "\"target_unix_pid\":" + std::to_string(target_pid);
    const std::string token = std::string("\"experiment_token\":\"") + kToken + "\"";
    const std::string role_field = std::string("\"trace_role\":\"") + role + "\"";
    size_t start = 0;
    size_t rows = 0;
    while (start < contents.size()) {
        const size_t end = contents.find('\n', start);
        if (end == std::string::npos) return false;
        const std::string row = contents.substr(start, end - start);
        if (!row.empty()) {
            if (row.find(host) == std::string::npos ||
                row.find(target) == std::string::npos ||
                row.find(token) == std::string::npos ||
                row.find(role_field) == std::string::npos) return false;
            ++rows;
        }
        start = end + 1;
    }
    return rows >= 3 && contents.find("\"event\":\"clock_metadata\"") != std::string::npos
        && contents.find("\"event\":\"offline_probe\"") != std::string::npos
        && contents.find("\"event\":\"trace_summary\"") != std::string::npos;
}

bool maximum_startup_record_fits() {
    const std::string path = "/" + std::string(PATH_MAX - 2, 'p');
    const std::string digest(64, 'a');
    const std::string fields =
        ",\"startup_id\":\"pid-2147483647\",\"startup_valid\":true"
        ",\"process_architecture\":\"x86_64\",\"process_translated\":1"
        ",\"process_start_monotonic_ns\":18446744073709551615"
        ",\"process_start_clock_domain\":\"mach_absolute_time_ns\""
        ",\"host_loader_role\":\"win32u.so::_vulkan_init_once direct dlopen libMoltenVK.dylib; no Khronos libvulkan\""
        + std::string(",\"winevulkan_so_path\":") + json_quote(path)
        + ",\"winevulkan_so_sha256\":" + json_quote(digest)
        + ",\"wine_host_loader_path\":" + json_quote(path)
        + ",\"wine_host_loader_sha256\":" + json_quote(digest)
        + ",\"trace_proxy_path\":" + json_quote(path)
        + ",\"trace_proxy_sha256\":" + json_quote(digest)
        + ",\"moltenvk_path\":" + json_quote(path)
        + ",\"moltenvk_sha256\":" + json_quote(digest)
        + ",\"moltenvk_version\":\"MoltenVK v1.4.2\""
        + ",\"moltenvk_version_source\":\"vkGetVersionStringsMVK from loaded delegate\""
        + ",\"vulkan_runtime_version\":\"1.4.357\",\"vulkan_api_version\":4206592"
        + ",\"loaded_moltenvk_count\":1,\"loaded_proxy_count\":1"
        + ",\"loaded_winevulkan_count\":1,\"loaded_host_loader_count\":1"
        + ",\"loaded_libvulkan_dylib_count\":0";
    Line line{};
    const int required_chars = format_trace_line(line, "INSTRUMENTATION",
        "host_runtime_startup", fields.c_str(), UINT64_MAX, UINT64_MAX,
        UINT64_MAX, std::numeric_limits<pid_t>::max(),
        std::numeric_limits<pid_t>::max(), kToken, "target");
    std::cout << "maximum-path host_runtime_startup snprintf_chars="
              << required_chars << " buffer_bytes=" << sizeof(line.bytes) << '\n';
    return required_chars > 0 &&
        static_cast<size_t>(required_chars) < sizeof(line.bytes);
}

enum class ChildMode {
    target_success,
    target_collision,
    helper_collision,
    helper_wrong_directory_identity,
    helper_malformed_token,
    wrong_directory_identity,
    symlink_directory,
    malformed_token,
};

pid_t run_child(const std::string& directory, DirectoryIdentity identity,
                ChildMode mode, const std::string& symlink_directory = {}) {
    const pid_t child = fork();
    if (child < 0) {
        std::perror("fork offline trace test");
        std::exit(2);
    }
    if (child == 0) {
        const pid_t pid = getpid();
        const bool helper = mode == ChildMode::helper_collision ||
            mode == ChildMode::helper_wrong_directory_identity ||
            mode == ChildMode::helper_malformed_token;
        const pid_t target = helper ? pid + 1 : pid;
        if (mode == ChildMode::wrong_directory_identity ||
            mode == ChildMode::helper_wrong_directory_identity) ++identity.inode;
        const std::string selected_directory = mode == ChildMode::symlink_directory
            ? symlink_directory : directory;
        configure(selected_directory, identity, target,
                  mode == ChildMode::malformed_token ||
                          mode == ChildMode::helper_malformed_token
                      ? "bad" : kToken);
        if (mode == ChildMode::target_collision || mode == ChildMode::helper_collision) {
            const std::string existing = trace_path(directory, pid);
            const int stale = open(existing.c_str(), O_CREAT | O_EXCL | O_WRONLY, 0600);
            if (stale < 0) _exit(30);
            close(stale);
        }
        start_trace();
        const bool must_abort = trace_initialization_must_abort();
        if (helper) {
            if (g_target_process || g_trace_role != std::string("helper") ||
                g_enabled.load(std::memory_order_acquire) || must_abort) _exit(31);
            _exit(0);
        }
        if (mode != ChildMode::target_success) {
            if (!g_target_process || !must_abort || g_enabled.load(std::memory_order_acquire))
                _exit(32);
            _exit(86);
        }
        if (!g_target_process || !g_enabled.load(std::memory_order_acquire) || must_abort)
            _exit(33);
        emit("TEST", "offline_probe", ",\"probe_id\":\"target\"");
        stop_trace();
        _exit(0);
    }
    return child;
}

int wait_for(pid_t child) {
    int status = 0;
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status)) return 127;
    return WEXITSTATUS(status);
}

bool run_parallel_child(const std::string& directory, DirectoryIdentity identity,
                        pid_t* child_out) {
    const pid_t child = fork();
    if (child < 0) return false;
    if (child == 0) {
        configure(directory, identity, getpid());
        start_trace();
        if (!g_target_process || !g_enabled.load(std::memory_order_acquire)) _exit(40);
        emit("TEST", "offline_probe", ",\"probe_id\":\"parallel\"");
        usleep(100000);
        stop_trace();
        _exit(0);
    }
    *child_out = child;
    return true;
}

void require(bool condition, const char* message) {
    if (!condition) {
        std::cerr << "FAIL: " << message << '\n';
        std::exit(1);
    }
}

}  // namespace

int main() {
    require(maximum_startup_record_fits(),
            "maximum PATH_MAX startup row fits with ownership fields");
    char root_template[] = "/tmp/fgmetal-trace-ownership.XXXXXX";
    char* root = mkdtemp(root_template);
    require(root != nullptr, "create temporary trace directory");
    char canonical_root[PATH_MAX]{};
    require(realpath(root, canonical_root) != nullptr,
            "resolve canonical temporary trace directory");
    const std::string directory(canonical_root);
    const DirectoryIdentity identity = identity_for(directory);

    pid_t child = run_child(directory, identity, ChildMode::target_success);
    require(wait_for(child) == 0, "target opens and drains its per-PID trace");
    std::string contents;
    const std::string target_trace = trace_path(directory, child);
    require(read_file(target_trace, contents), "target trace file exists");
    require(all_rows_have_identity(contents, child, child, "target"),
            "target clock, event, and shutdown rows carry matching ownership");
    struct stat trace_info{};
    require(stat(target_trace.c_str(), &trace_info) == 0 &&
            (trace_info.st_mode & 0777) == 0600, "target trace mode is 0600");
    unlink(target_trace.c_str());

    child = run_child(directory, identity, ChildMode::target_collision);
    require(wait_for(child) == 86, "target trace collision fails closed with status 86");
    unlink(trace_path(directory, child).c_str());

    child = run_child(directory, identity, ChildMode::helper_collision);
    require(wait_for(child) == 0, "helper trace collision does not abort that helper path");
    unlink(trace_path(directory, child).c_str());

    child = run_child(directory, identity, ChildMode::helper_wrong_directory_identity);
    require(wait_for(child) == 0, "helper directory identity failure does not abort the helper path");

    child = run_child(directory, identity, ChildMode::helper_malformed_token);
    require(wait_for(child) == 0, "helper token failure does not abort the helper path");

    child = run_child(directory, identity, ChildMode::wrong_directory_identity);
    require(wait_for(child) == 86, "directory inode mismatch fails closed for target");

    const std::string symlink_directory = directory + "-link";
    require(symlink(directory.c_str(), symlink_directory.c_str()) == 0,
            "create symlink directory fixture");
    child = run_child(directory, identity, ChildMode::symlink_directory,
                      symlink_directory);
    require(wait_for(child) == 86, "symlink trace directory is rejected");
    unlink(symlink_directory.c_str());

    child = run_child(directory, identity, ChildMode::malformed_token);
    require(wait_for(child) == 86, "malformed experiment token fails closed for target");

    pid_t first = -1;
    pid_t second = -1;
    require(run_parallel_child(directory, identity, &first) &&
            run_parallel_child(directory, identity, &second),
            "start concurrent per-process trace writers");
    require(wait_for(first) == 0 && wait_for(second) == 0,
            "concurrent trace writers finish cleanly");
    require(first != second, "concurrent writers have distinct host PIDs");
    std::string first_contents;
    std::string second_contents;
    require(read_file(trace_path(directory, first), first_contents) &&
            read_file(trace_path(directory, second), second_contents),
            "concurrent writers create distinct per-PID files");
    require(all_rows_have_identity(first_contents, first, first, "target") &&
            all_rows_have_identity(second_contents, second, second, "target"),
            "concurrent files preserve independent target ownership and summaries");

    unlink(trace_path(directory, first).c_str());
    unlink(trace_path(directory, second).c_str());
    require(rmdir(directory.c_str()) == 0, "remove temporary trace directory");
    std::cout << "native trace ownership tests passed\n";
    return 0;
}
