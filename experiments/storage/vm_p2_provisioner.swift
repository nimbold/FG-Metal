import CryptoKit
import Darwin
import Foundation
import Virtualization

private let systemLogicalCapacity: Int64 = 64 * 1024 * 1024 * 1024
private let expectedRestoreSHA256 = "2f016638293c3e641b8b25391a76fbc16563b3711915a5551cf8aa0f5598a5c1"

private final class EventOutput {
    private let lock = NSLock()

    func emit(_ event: [String: Any]) {
        lock.lock()
        defer { lock.unlock() }
        do {
            var record = event
            record["captured_at_unix"] = event["captured_at_unix"] ?? Date().timeIntervalSince1970
            record["monotonic_time_seconds"] = ProcessInfo.processInfo.systemUptime
            let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data([0x0a]))
        } catch {
            fputs("event serialization failed: \(error)\n", stderr)
        }
    }
}

private struct FileIdentity: Codable, Equatable {
    let device: UInt64
    let inode: UInt64
    let logicalBytes: Int64
    let allocatedBytes: Int64
    let linkCount: UInt64
    let mode: UInt16
    let modifiedSeconds: Int64
    let modifiedNanoseconds: Int64
    let changedSeconds: Int64
    let changedNanoseconds: Int64

    init(_ value: stat) {
        device = UInt64(value.st_dev)
        inode = UInt64(value.st_ino)
        logicalBytes = Int64(value.st_size)
        allocatedBytes = Int64(value.st_blocks) * 512
        linkCount = UInt64(value.st_nlink)
        mode = UInt16(value.st_mode)
        modifiedSeconds = Int64(value.st_mtimespec.tv_sec)
        modifiedNanoseconds = Int64(value.st_mtimespec.tv_nsec)
        changedSeconds = Int64(value.st_ctimespec.tv_sec)
        changedNanoseconds = Int64(value.st_ctimespec.tv_nsec)
    }

    var json: [String: Any] {
        let mtimeNS = modifiedSeconds * 1_000_000_000 + modifiedNanoseconds
        let ctimeNS = changedSeconds * 1_000_000_000 + changedNanoseconds
        return [
            "device": device,
            "inode": inode,
            "logical_bytes": logicalBytes,
            "allocated_bytes": allocatedBytes,
            "link_count": linkCount,
            "mode": mode,
            "mtime_ns": mtimeNS,
            "ctime_ns": ctimeNS,
            "mtime_seconds": modifiedSeconds,
            "mtime_nanoseconds": modifiedNanoseconds,
            "ctime_seconds": changedSeconds,
            "ctime_nanoseconds": changedNanoseconds,
        ]
    }

    var p1JSON: [String: Any] {
        ["device": device, "inode": inode, "logical_bytes": logicalBytes,
         "allocated_bytes": allocatedBytes,
         "mtime_ns": modifiedSeconds * 1_000_000_000 + modifiedNanoseconds,
         "ctime_ns": changedSeconds * 1_000_000_000 + changedNanoseconds]
    }

    var p1Identity: P1RestoreFileIdentity {
        P1RestoreFileIdentity(device: device, inode: inode, logicalBytes: logicalBytes,
                              allocatedBytes: allocatedBytes,
                              mtimeNS: modifiedSeconds * 1_000_000_000 + modifiedNanoseconds,
                              ctimeNS: changedSeconds * 1_000_000_000 + changedNanoseconds)
    }
}

private struct P1RestoreFileIdentity: Codable, Equatable {
    let device: UInt64
    let inode: UInt64
    let logicalBytes: Int64
    let allocatedBytes: Int64
    let mtimeNS: Int64
    let ctimeNS: Int64

    enum CodingKeys: String, CodingKey {
        case device, inode
        case logicalBytes = "logical_bytes"
        case allocatedBytes = "allocated_bytes"
        case mtimeNS = "mtime_ns"
        case ctimeNS = "ctime_ns"
    }

    var json: [String: Any] {
        ["device": device, "inode": inode, "logical_bytes": logicalBytes,
         "allocated_bytes": allocatedBytes, "mtime_ns": mtimeNS, "ctime_ns": ctimeNS]
    }
}

private let p2AdmissionVerifierSource = #"""
import hashlib, hmac, json, os, pathlib, re, stat, sys

MAX_RECORD_BYTES = 64 * 1024 * 1024
RESTORE_PATH = "/Volumes/FGMetal-VM/restore.ipsw"
RESTORE_SHA256 = "2f016638293c3e641b8b25391a76fbc16563b3711915a5551cf8aa0f5598a5c1"
RESTORE_SIZE = 26637307067
P1_UPSTREAM_HARDWARE_MODEL_SHA256 = "18d57e0cfa40f7af63bb38c801a03bde06d00b281554b926128828a7ca0d9a2e"
P2_LOCAL_HARDWARE_MODEL_SHA256 = "976e66740c64041cf9f127588f93eabf7efb9e7d3cbaee9582bcded88ba5e8d3"
HARDWARE_MODEL_SHA256 = P2_LOCAL_HARDWARE_MODEL_SHA256
CONTROLLER_PATHS = ["experiments/storage/vm_capacity_domain.py", "experiments/storage/storage_policy.py"]

def fail(message):
    raise RuntimeError(message)

def identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink)

def read_stable(path, maximum=MAX_RECORD_BYTES):
    path = pathlib.Path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            fail("P2 authorization input is not a single-link regular file: " + str(path))
        data = bytearray()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            data.extend(block)
            if len(data) > maximum:
                fail("P2 authorization input exceeded its bounded size: " + str(path))
        after = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        if identity(before) != identity(after) or identity(after) != identity(current) or len(data) != after.st_size:
            fail("P2 authorization input changed while being read: " + str(path))
        return bytes(data), after
    finally:
        os.close(fd)

def record(path, signature_field, key):
    raw, info = read_stable(path)
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get(signature_field), str):
        fail("P2 authorization record omitted its signature field: " + str(path))
    payload = dict(value)
    actual = payload.pop(signature_field)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(actual, expected):
        fail("P2 authorization record has an invalid signature: " + str(path))
    return value, raw, info

def digest(path, maximum=1024 * 1024 * 1024):
    raw, _ = read_stable(path, maximum)
    return hashlib.sha256(raw).hexdigest()

def resolved(value, root):
    path = pathlib.Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=True)

def main():
    root = pathlib.Path(sys.argv[4]).resolve(strict=True)
    home = pathlib.Path.home()
    key_path = home / "Library/Caches/FGMetal/.storage-manifest-signing-key"
    key_fd = os.open(key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        key_info = os.fstat(key_fd)
        if (not stat.S_ISREG(key_info.st_mode) or key_info.st_nlink != 1
                or key_info.st_uid != os.getuid() or stat.S_IMODE(key_info.st_mode) != 0o600):
            fail("P2 signing key has an unsafe fixed-path file identity")
        key = os.read(key_fd, 33)
        if len(key) != 32 or os.read(key_fd, 1):
            fail("P2 signing key must contain exactly 32 bytes")
    finally:
        os.close(key_fd)

    admission_path = pathlib.Path(sys.argv[1]).resolve(strict=True)
    manifest_path = pathlib.Path(sys.argv[2]).resolve(strict=True)
    review_path = pathlib.Path(sys.argv[3]).resolve(strict=True)
    admission, admission_raw, _ = record(admission_path, "vm_admission_hmac_sha256", key)
    run_id = admission.get("run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", run_id) is None:
        fail("signed P2 admission has a malformed run ID")
    if admission_path != (root / ("R6-VM-P2-ADMISSION-" + run_id + ".json")).resolve(strict=True):
        fail("helper admission path is not the canonical run-specific repository receipt")
    if (admission.get("decision") != "PASS" or admission.get("authorized_phase") != "P2"
            or admission.get("authority_scope") != "PROVISION_MACOS_GUEST_BASE_ONLY"
            or admission.get("restore_path") != RESTORE_PATH
            or admission.get("restore_sha256") != RESTORE_SHA256
            or admission.get("restore_logical_bytes") != RESTORE_SIZE
            or admission.get("hardware_model_sha256") != P2_LOCAL_HARDWARE_MODEL_SHA256
            or admission.get("minimum_supported_cpu_count") != 2
            or admission.get("minimum_supported_memory_size_bytes") != 4 * 1024 * 1024 * 1024
            or admission.get("p1_upstream_hardware_model_sha256") != P1_UPSTREAM_HARDWARE_MODEL_SHA256
            or admission.get("vm_volume_uuid") != "DC80C359-922C-41DE-B106-5E0C00FEDCE1"
            or admission.get("system_raw_path") != f"/Volumes/FGMetal-VM/templates/macos-base-{run_id}/system.raw"
            or admission.get("auxiliary_storage_path") != f"/Volumes/FGMetal-VM/templates/macos-base-{run_id}/auxiliary.bin"):
        fail("signed P2 admission does not authorize the exact bounded stock-base installation")
    control_token_sha256 = admission.get("helper_control_token_sha256")
    if not isinstance(control_token_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", control_token_sha256) is None:
        fail("signed P2 admission omitted its one-process live-monitor authorization commitment")

    expected_manifest_path = resolved(admission.get("provisioner_source_manifest_path", ""), root)
    expected_review_path = resolved(admission.get("provisioner_review_path", ""), root)
    if manifest_path != expected_manifest_path or review_path != expected_review_path:
        fail("helper manifest or review path differs from the signed P2 admission")
    manifest, manifest_raw, _ = record(manifest_path, "vm_p2_provenance_hmac_sha256", key)
    review, review_raw, _ = record(
        review_path, "vm_p2_provisioner_review_hmac_sha256", key)
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    review_sha = hashlib.sha256(review_raw).hexdigest()
    sources = manifest.get("source_manifest")
    if not isinstance(sources, dict):
        fail("signed P2 provisioner manifest omitted source identities")
    binary = sources.get("binary", {})
    source = sources.get("source", {})
    adapter = sources.get("orchestration_source", {})
    dependencies = sources.get("controller_dependencies", [])
    selected_dependencies = [{"path": item.get("path"), "sha256": item.get("sha256")}
                             for item in dependencies if isinstance(item, dict)]
    if (len(dependencies) != len(CONTROLLER_PATHS)
            or selected_dependencies != [
                {"path": path, "sha256": item.get("sha256")}
                for path, item in zip(CONTROLLER_PATHS, dependencies) if isinstance(item, dict)]):
        fail("signed P2 manifest did not bind the exact imported capacity controller dependencies")
    for role in ("source", "orchestration_source", "entitlements"):
        item = sources.get(role, {})
        if (not isinstance(item, dict) or pathlib.Path(item.get("path", "")).is_absolute()
                or digest(root / item.get("path", "")) != item.get("sha256")):
            fail("reviewed P2 " + role + " bytes no longer match the signed manifest")
    review_manifest_path = resolved(review.get("manifest_path", ""), root)
    if review_manifest_path != manifest_path:
        fail("independent P2 helper review references a different manifest path")
    if (review.get("decision") not in {"PASS — P2 PROVISIONER APPROVED", "PASS - P2 PROVISIONER APPROVED"}
            or review.get("reviewed_manifest_sha256") != manifest_sha
            or review.get("reviewed_source_sha256") != source.get("sha256")
            or review.get("reviewed_adapter_sha256") != adapter.get("sha256")
            or review.get("reviewed_binary_sha256") != binary.get("sha256")
            or review.get("reviewed_code_directory_hash") != binary.get("code_directory_hash")
            or review.get("reviewed_controller_dependencies") != selected_dependencies):
        fail("independent helper review does not approve the exact signed manifest and controller dependencies")
    if (manifest_sha != admission.get("provisioner_source_manifest_sha256")
            or review_sha != admission.get("provisioner_review_sha256")
            or source.get("sha256") != admission.get("provisioner_source_sha256")
            or adapter.get("sha256") != admission.get("provisioner_orchestration_source_sha256")
            or binary.get("sha256") != admission.get("provisioner_binary_sha256")
            or binary.get("code_directory_hash") != admission.get("provisioner_code_directory_hash")):
        fail("signed P2 admission, review, and provisioner identities do not match")
    if pathlib.Path(binary.get("path", "")).is_absolute() or digest(root / binary.get("path", "")) != binary.get("sha256"):
        fail("reviewed P2 helper binary bytes no longer match the signed manifest")
    for item, relative_path in zip(dependencies, CONTROLLER_PATHS):
        if (not isinstance(item, dict) or item.get("path") != relative_path
                or digest(root / relative_path) != item.get("sha256")):
            fail("an imported P2 controller dependency changed after review: " + relative_path)
    active_path = home / "Library/Caches/FGMetal/vm-capacity-domain/active-session.json"
    active, active_raw, _ = record(active_path, "vm_session_hmac_sha256", key)
    if (active.get("session_id") != admission.get("session_id")
            or active.get("volume_uuid") != admission.get("vm_volume_uuid")
            or active.get("state") != "ACTIVE" or active.get("phase_id") != "P2_AUTHORIZED"):
        fail("fixed-path signed active marker is not the same authorized P2 session")
    print(json.dumps({
        "admission": admission,
        "admission_sha256": hashlib.sha256(admission_raw).hexdigest(),
        "active_marker_sha256": hashlib.sha256(active_raw).hexdigest(),
        "provisioner_binary_sha256": binary.get("sha256"),
        "provisioner_binary_path": binary.get("path"),
        "code_directory_hash": binary.get("code_directory_hash"),
        "helper_control_token_sha256": control_token_sha256,
    }, sort_keys=True, separators=(",", ":")))

try:
    main()
except BaseException as error:
    print("P2 admission verification failed: " + str(error), file=sys.stderr)
    raise SystemExit(1)
"""#

private final class P2LaunchContext {
    let runID: String
    let sessionID: String
    let admissionSHA256: String
    let activeMarkerSHA256: String
    let provisionerSHA256: String
    let helperControlTokenSHA256: String
    private let controlLock = NSLock()
    private var controlToken: Data?

    init(authorization: [String: Any]) throws {
        guard let runID = authorization["run_id"] as? String,
              let sessionID = authorization["session_id"] as? String,
              let admissionSHA256 = authorization["admission_sha256"] as? String,
              let activeMarkerSHA256 = authorization["active_marker_sha256"] as? String,
              let provisionerSHA256 = authorization["provisioner_binary_sha256"] as? String,
              let helperControlTokenSHA256 = authorization["helper_control_token_sha256"] as? String else {
            throw NSError(domain: "FGMetalP2", code: 64,
                          userInfo: [NSLocalizedDescriptionKey: "verified P2 launch context omitted a required identity"])
        }
        let hashes = [admissionSHA256, activeMarkerSHA256, provisionerSHA256,
                      helperControlTokenSHA256]
        guard runID.range(of: "^[A-Za-z0-9._-]{1,128}$", options: .regularExpression) != nil,
              sessionID.range(of: "^[A-Za-z0-9._-]{1,128}$", options: .regularExpression) != nil,
              hashes.allSatisfy({ $0.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil }) else {
            throw NSError(domain: "FGMetalP2", code: 61,
                          userInfo: [NSLocalizedDescriptionKey: "P2 launch authorization identity is malformed"])
        }
        self.runID = runID
        self.sessionID = sessionID
        self.admissionSHA256 = admissionSHA256
        self.activeMarkerSHA256 = activeMarkerSHA256
        self.provisionerSHA256 = provisionerSHA256
        self.helperControlTokenSHA256 = helperControlTokenSHA256
    }

    var authorizationPayload: String {
        ["P2AUTH1", runID, sessionID, admissionSHA256, activeMarkerSHA256,
         provisionerSHA256, "PROVISION_MACOS_GUEST_BASE_ONLY", "P2"].joined(separator: "|")
    }

    func acceptAuthorization(_ payloadBase64: String, tokenBase64: String,
                             signature: String) -> Bool {
        guard let data = Data(base64Encoded: payloadBase64),
              String(data: data, encoding: .utf8) == authorizationPayload,
              let token = Data(base64Encoded: tokenBase64), token.count == 32,
              Self.sha256Hex(token) == helperControlTokenSHA256,
              Self.verifyHMAC(data, key: token, signature: signature) else { return false }
        controlLock.lock()
        defer { controlLock.unlock() }
        guard controlToken == nil else { return false }
        controlToken = token
        return true
    }

    func verifyMonitorMessage(_ payloadBase64: String, signature: String,
                              minimumSamples: Int, previousSequence: UInt64) -> UInt64? {
        guard let data = Data(base64Encoded: payloadBase64),
              let text = String(data: data, encoding: .utf8) else { return nil }
        let fields = text.split(separator: "|", omittingEmptySubsequences: false).map(String.init)
        guard fields.count == 8,
              fields[0] == "P2MON1",
              fields[1] == runID,
              fields[2] == sessionID,
              fields[3] == admissionSHA256,
              fields[4] == activeMarkerSHA256,
              fields[5] == provisionerSHA256,
              let samples = Int(fields[6]), samples >= minimumSamples,
              let sequence = UInt64(fields[7]), sequence >= UInt64(samples),
              sequence > previousSequence,
              verifyControlHMAC(data, signature: signature) else { return nil }
        return sequence
    }

    private func verifyControlHMAC(_ payload: Data, signature: String) -> Bool {
        controlLock.lock()
        defer { controlLock.unlock() }
        guard let controlToken else { return false }
        return Self.verifyHMAC(payload, key: controlToken, signature: signature)
    }

    private static func verifyHMAC(_ payload: Data, key: Data, signature: String) -> Bool {
        let authenticationCode = HMAC<SHA256>.authenticationCode(for: payload,
                                                                  using: SymmetricKey(data: key))
        let expected = Data(authenticationCode).map { String(format: "%02x", $0) }.joined()
        let lhs = Array(expected.utf8)
        let rhs = Array(signature.utf8)
        guard lhs.count == rhs.count else { return false }
        var difference: UInt8 = 0
        for index in lhs.indices { difference |= lhs[index] ^ rhs[index] }
        return difference == 0
    }

    private static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}

private func p2RepositoryRoot() -> URL {
    URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
}

private func verifyP2LaunchFiles(admissionPath: String, manifestPath: String,
                                 reviewPath: String) throws -> [String: Any] {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    process.arguments = ["-I", "-c", p2AdmissionVerifierSource,
                        admissionPath, manifestPath, reviewPath, p2RepositoryRoot().path]
    process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                           "LANG": "en_US.UTF-8",
                           "HOME": NSHomeDirectory()]
    let stdout = Pipe()
    let stderr = Pipe()
    process.standardOutput = stdout
    process.standardError = stderr
    do {
        try process.run()
    } catch {
        throw NSError(domain: "FGMetalP2", code: 65,
                      userInfo: [NSLocalizedDescriptionKey: "fixed Python verifier could not start: \(error)"])
    }
    let output = stdout.fileHandleForReading.readDataToEndOfFile()
    let errorOutput = stderr.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard output.count <= 64 * 1024 * 1024,
          process.terminationReason == .exit, process.terminationStatus == 0,
          let result = try? JSONSerialization.jsonObject(with: output) as? [String: Any],
          let admission = result["admission"] as? [String: Any],
          let runID = admission["run_id"] as? String,
          let sessionID = admission["session_id"] as? String else {
        let details = String(data: errorOutput, encoding: .utf8) ?? "verifier returned malformed output"
        throw NSError(domain: "FGMetalP2", code: 66,
                      userInfo: [NSLocalizedDescriptionKey: "fixed verifier rejected the signed P2 launch inputs: \(details)"])
    }
    var authorization = result
    authorization["run_id"] = runID
    authorization["session_id"] = sessionID
    authorization["admission"] = admission
    return authorization
}

private func verifyRestoreIdentity(_ url: URL,
                                   expected: P1RestoreFileIdentity) throws -> stat {
    var descriptorStat = stat()
    let descriptor = open(url.path, O_RDONLY | O_NOFOLLOW)
    guard descriptor >= 0 else {
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                      userInfo: [NSLocalizedDescriptionKey: "accepted IPSW could not be reopened without following links"])
    }
    let descriptorOK = fstat(descriptor, &descriptorStat) == 0
    close(descriptor)
    var pathStat = stat()
    guard descriptorOK, lstat(url.path, &pathStat) == 0,
          (descriptorStat.st_mode & S_IFMT) == S_IFREG,
          (pathStat.st_mode & S_IFMT) == S_IFREG,
          descriptorStat.st_nlink == 1, pathStat.st_nlink == 1,
          FileIdentity(descriptorStat) == FileIdentity(pathStat),
          FileIdentity(pathStat).p1Identity == expected else {
        throw NSError(domain: "FGMetalP2", code: 18,
                      userInfo: [NSLocalizedDescriptionKey: "accepted IPSW file identity changed from signed P2 admission"])
    }
    return pathStat
}

private struct DirectoryIdentity: Equatable {
    let device: UInt64
    let inode: UInt64

    init(_ value: stat) {
        device = UInt64(value.st_dev)
        inode = UInt64(value.st_ino)
    }
}

private final class ProvisioningSession: @unchecked Sendable {
    private let lock = NSLock()
    private let output: EventOutput
    private let launchContext: P2LaunchContext
    private var vm: VZVirtualMachine?
    private var installer: VZMacOSInstaller?
    private var attachment: VZDiskImageStorageDeviceAttachment?
    private var configuration: VZVirtualMachineConfiguration?
    private var progress: Progress?
    private var installStarted = false
    private var installStarting = false
    private var cancelWhenStarted = false
    private var installTerminal = false
    private var stopRequested = false
    private var abortBeforeInstall = false
    private var startRequested = false
    private var launchAuthorizationAccepted = false
    private var monitorGateAccepted = false
    private var monitorSequence: UInt64 = 0
    private var lastMonitorHeartbeatMonotonic: Double?
    private var heartbeatExpiryReported = false
    private var releasedResult: [String: Any]?
    private var vmStopTask: Task<Void, Never>?

    init(output: EventOutput, launchContext: P2LaunchContext) {
        self.output = output
        self.launchContext = launchContext
    }

    func retain(vm: VZVirtualMachine,
                installer: VZMacOSInstaller,
                attachment: VZDiskImageStorageDeviceAttachment,
                configuration: VZVirtualMachineConfiguration) {
        lock.lock()
        self.vm = vm
        self.installer = installer
        self.attachment = attachment
        self.configuration = configuration
        progress = installer.progress
        lock.unlock()
    }

    func requestInstallStart() throws -> VZMacOSInstaller {
        lock.lock()
        defer { lock.unlock() }
        if abortBeforeInstall || stopRequested {
            throw NSError(domain: "FGMetalP2", code: 20,
                          userInfo: [NSLocalizedDescriptionKey: "provisioning was stopped before installer start"])
        }
        guard let installer else {
            throw NSError(domain: "FGMetalP2", code: 21,
                          userInfo: [NSLocalizedDescriptionKey: "installer ownership was lost"])
        }
        installStarting = true
        return installer
    }

    func installInvocationStarted() {
        lock.lock()
        installStarting = false
        installStarted = !installTerminal
        let shouldCancel = cancelWhenStarted && !installTerminal
        let selectedProgress = progress
        lock.unlock()
        if shouldCancel { selectedProgress?.cancel() }
        output.emit(["event": "installer_invocation_started",
                     "pending_cancel_applied": shouldCancel,
                     "captured_at_unix": Date().timeIntervalSince1970])
    }

    func operationFinished(_ success: Bool) {
        lock.lock()
        installTerminal = true
        lock.unlock()
        output.emit(["event": "installer_terminal", "installer_completed": success,
                     "captured_at_unix": Date().timeIntervalSince1970])
    }

    func monitorState() -> (finished: Bool, stopRequested: Bool) {
        lock.lock()
        defer { lock.unlock() }
        return (installTerminal, stopRequested)
    }

    func progressSnapshot() -> Progress? {
        lock.lock()
        defer { lock.unlock() }
        return progress
    }

    func requestCancel() {
        lock.lock()
        let shouldCancel = installStarted && !installTerminal
        if installStarting && !installTerminal {
            cancelWhenStarted = true
        } else if !installStarted {
            abortBeforeInstall = true
        }
        let selectedProgress = progress
        lock.unlock()
        if shouldCancel { selectedProgress?.cancel() }
        output.emit(["event": "cancel_command_received", "installer_cancel_called": shouldCancel,
                     "captured_at_unix": Date().timeIntervalSince1970])
    }

    func requestStop() {
        lock.lock()
        stopRequested = true
        if installStarting && !installTerminal { cancelWhenStarted = true }
        let terminal = installTerminal
        let selectedVM = vm
        if let selectedVM, vmStopTask == nil {
            // A stop request must reach Virtualization.framework while the
            // installer completion continuation is still pending. Waiting for
            // that continuation before calling stop can leave a failed P2
            // writer alive after its capacity monitor has stopped.
            vmStopTask = Task { @MainActor [weak self, selectedVM] in
                let stateBefore = String(describing: selectedVM.state)
                var stopRequestIssued = false
                var stopFailure: String?
                if selectedVM.state != .stopped {
                    stopRequestIssued = true
                    do { try await selectedVM.stop() }
                    catch { stopFailure = String(describing: error) }
                }
                let stateAfter = String(describing: selectedVM.state)
                self?.recordVMStopTaskResult(stateBefore: stateBefore,
                                              stopRequestIssued: stopRequestIssued,
                                              stateAfter: stateAfter,
                                              failure: stopFailure)
            }
        }
        let stopTaskScheduled = vmStopTask != nil
        lock.unlock()
        output.emit(["event": "stop_command_received", "installer_terminal": terminal,
                     "vm_stop_task_scheduled": stopTaskScheduled,
                     "captured_at_unix": Date().timeIntervalSince1970])
    }

    private func recordVMStopTaskResult(stateBefore: String,
                                        stopRequestIssued: Bool,
                                        stateAfter: String,
                                        failure: String?) {
        let result: [String: Any] = [
            "event": "vm_stop_task_result",
            "state_before_stop": stateBefore,
            "stop_request_issued": stopRequestIssued,
            "vm_state_after_stop": stateAfter,
            "error": failure as Any? ?? NSNull(),
            "captured_at_unix": Date().timeIntervalSince1970,
        ]
        output.emit(result)
    }

    func requestProvisionStart() {
        lock.lock()
        let heartbeatFresh = lastMonitorHeartbeatMonotonic.map {
            ProcessInfo.processInfo.systemUptime - $0 <= 0.25
        } ?? false
        if launchAuthorizationAccepted && monitorGateAccepted && heartbeatFresh
                && !abortBeforeInstall && !stopRequested {
            startRequested = true
        }
        let accepted = startRequested
        let authorizationAccepted = launchAuthorizationAccepted
        let monitorAccepted = monitorGateAccepted
        let acceptedMonitorSequence = monitorSequence
        lock.unlock()
        output.emit(["event": "start_command_received", "accepted": accepted,
                     "p2_authorization_accepted": authorizationAccepted,
                     "monitor_gate_accepted": monitorAccepted,
                     "monitor_sequence": acceptedMonitorSequence,
                     "captured_at_unix": Date().timeIntervalSince1970])
    }

    func requestLaunchAuthorization(_ payloadBase64: String, tokenBase64: String,
                                    signature: String) {
        let tokenValid = launchContext.acceptAuthorization(
            payloadBase64, tokenBase64: tokenBase64, signature: signature)
        lock.lock()
        let accepted = tokenValid && !abortBeforeInstall && !stopRequested
        if accepted { launchAuthorizationAccepted = true }
        lock.unlock()
        output.emit(["event": accepted ? "p2_authorization_accepted" : "p2_authorization_rejected",
                     "accepted": accepted,
                     "captured_at_unix": Date().timeIntervalSince1970])
    }

    func requestMonitorHeartbeat(_ payloadBase64: String, signature: String, isGate: Bool) {
        lock.lock()
        let canAccept = launchAuthorizationAccepted && (!isGate || !monitorGateAccepted)
            && (isGate || monitorGateAccepted)
        let sequence = canAccept
            ? launchContext.verifyMonitorMessage(payloadBase64, signature: signature,
                                                  minimumSamples: 3,
                                                  previousSequence: monitorSequence)
            : nil
        if let sequence {
            monitorSequence = sequence
            monitorGateAccepted = true
            lastMonitorHeartbeatMonotonic = ProcessInfo.processInfo.systemUptime
        }
        let accepted = sequence != nil
        lock.unlock()
        let event = isGate
            ? (accepted ? "monitor_gate_accepted" : "monitor_gate_rejected")
            : (accepted ? "monitor_heartbeat_accepted" : "monitor_heartbeat_rejected")
        let eventSequence: Any
        if let sequence { eventSequence = NSNumber(value: sequence) }
        else { eventSequence = NSNull() }
        output.emit(["event": event, "accepted": accepted,
                     "monitor_sequence": eventSequence,
                     "captured_at_unix": Date().timeIntervalSince1970])
    }

    @discardableResult
    func enforceMonitorHeartbeat() -> Bool {
        lock.lock()
        let now = ProcessInfo.processInfo.systemUptime
        let expired = monitorGateAccepted
            && !stopRequested
            && (lastMonitorHeartbeatMonotonic.map { now - $0 > 0.25 } ?? true)
        if expired {
            abortBeforeInstall = true
            stopRequested = true
            if installStarting && !installTerminal { cancelWhenStarted = true }
        }
        let shouldReport = expired && !heartbeatExpiryReported
        if shouldReport { heartbeatExpiryReported = true }
        let shouldCancel = expired && installStarted && !installTerminal
        let selectedProgress = progress
        lock.unlock()
        if shouldCancel { selectedProgress?.cancel() }
        if shouldReport {
            output.emit(["event": "monitor_heartbeat_expired", "captured_at_unix": Date().timeIntervalSince1970])
        }
        return expired
    }

    func awaitProvisionStart() async throws {
        while true {
            _ = enforceMonitorHeartbeat()
            let (shouldStart, shouldAbort) = provisionStartState()
            if shouldAbort {
                throw NSError(domain: "FGMetalP2", code: 19,
                              userInfo: [NSLocalizedDescriptionKey: "live monitor stopped provisioning before asset creation"])
            }
            if shouldStart { return }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
    }

    private func provisionStartState() -> (Bool, Bool) {
        lock.lock()
        defer { lock.unlock() }
        let heartbeatFresh = lastMonitorHeartbeatMonotonic.map {
            ProcessInfo.processInfo.systemUptime - $0 <= 0.25
        } ?? false
        return (startRequested && launchAuthorizationAccepted && monitorGateAccepted && heartbeatFresh,
                abortBeforeInstall || stopRequested)
    }

    func abortRequested() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return abortBeforeInstall || stopRequested
    }

    private func retainedVM() -> VZVirtualMachine? {
        lock.lock()
        defer { lock.unlock() }
        return vm
    }

    private func retainedVMStopTask() -> Task<Void, Never>? {
        lock.lock()
        defer { lock.unlock() }
        return vmStopTask
    }

    private func releasedResultSnapshot() -> [String: Any]? {
        lock.lock()
        defer { lock.unlock() }
        return releasedResult
    }

    private func rememberReleasedResult(_ result: [String: Any]) {
        lock.lock()
        releasedResult = result
        lock.unlock()
    }

    private func releaseRetainedObjects() {
        lock.lock()
        installer = nil
        progress = nil
        vm = nil
        attachment = nil
        configuration = nil
        lock.unlock()
    }

    @MainActor
    func stopAndRelease() async throws -> [String: Any] {
        if let releasedResult = releasedResultSnapshot() { return releasedResult }
        let selectedVM = retainedVM()

        var finalState = "NO_VM_CREATED"
        var stateBeforeShutdown = "NO_VM_CREATED"
        var stopRequestIssued = false
        var stoppedObservedAtUnix: Double?
        var stoppedObservedMonotonic: Double?
        if let selectedVM {
            stateBeforeShutdown = String(describing: selectedVM.state)
            if selectedVM.state != .stopped {
                requestStop()
                stopRequestIssued = true
            }
            let stopTask = retainedVMStopTask()
            if let stopTask { await stopTask.value }
            let deadline = Date().addingTimeInterval(30)
            while selectedVM.state != .stopped && Date() < deadline {
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            guard selectedVM.state == .stopped else {
                throw NSError(domain: "FGMetalP2", code: 22,
                              userInfo: [NSLocalizedDescriptionKey: "Virtualization.framework did not reach actual stopped state"])
            }
            finalState = "STOPPED"
            stoppedObservedAtUnix = Date().timeIntervalSince1970
            stoppedObservedMonotonic = ProcessInfo.processInfo.systemUptime
        }

        let result: [String: Any] = ["vm_state": finalState,
                                     "observer_source": "VZVirtualMachine.state polled by ProvisioningSession.stopAndRelease",
                                     "state_before_shutdown": stateBeforeShutdown,
                                     "stop_request_issued": stopRequestIssued,
                                     "state_after_stop": selectedVM.map { String(describing: $0.state) } ?? "NO_VM_CREATED",
                                     "stopped_observed_at_unix": stoppedObservedAtUnix as Any? ?? NSNull(),
                                     "stopped_observed_monotonic_seconds": stoppedObservedMonotonic as Any? ?? NSNull(),
                                     "all_writable_handles_released": true,
                                     "installer_object_released": true,
                                     "disk_attachment_released": true]
        releaseRetainedObjects()
        rememberReleasedResult(result)
        return result
    }

    func emitHandlesReleased() {
        lock.lock()
        let result = releasedResult
        lock.unlock()
        guard let result else { return }
        output.emit(["event": "handles_released",
                     "vm_state": result["vm_state"] ?? "UNKNOWN",
                     "observer_source": result["observer_source"] ?? "UNKNOWN",
                     "state_before_shutdown": result["state_before_shutdown"] ?? "UNKNOWN",
                     "stop_request_issued": result["stop_request_issued"] ?? false,
                     "state_after_stop": result["state_after_stop"] ?? "UNKNOWN",
                     "stopped_observed_at_unix": result["stopped_observed_at_unix"] ?? NSNull(),
                     "stopped_observed_monotonic_seconds": result["stopped_observed_monotonic_seconds"] ?? NSNull(),
                     "all_writable_handles_released": result["all_writable_handles_released"] ?? false,
                     "installer_object_released": result["installer_object_released"] ?? false,
                     "disk_attachment_released": result["disk_attachment_released"] ?? false,
                     "captured_at_unix": Date().timeIntervalSince1970])
    }
}

private func sha256(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

private func sha256NoFollow(_ url: URL) throws -> String {
    let descriptor = open(url.path, O_RDONLY | O_NOFOLLOW)
    guard descriptor >= 0 else {
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                      userInfo: [NSLocalizedDescriptionKey: "reviewed helper could not be reopened without following links"])
    }
    defer { close(descriptor) }
    var before = stat()
    guard fstat(descriptor, &before) == 0,
          (before.st_mode & S_IFMT) == S_IFREG,
          before.st_nlink == 1 else {
        throw NSError(domain: "FGMetalP2", code: 68,
                      userInfo: [NSLocalizedDescriptionKey: "reviewed helper is not a single-link regular file"])
    }
    var hasher = SHA256()
    var bytes = [UInt8](repeating: 0, count: 1024 * 1024)
    while true {
        let amount = bytes.withUnsafeMutableBytes { buffer in
            read(descriptor, buffer.baseAddress!, buffer.count)
        }
        if amount < 0 {
            if errno == EINTR { continue }
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                          userInfo: [NSLocalizedDescriptionKey: "reviewed helper hash read failed"])
        }
        if amount == 0 { break }
        hasher.update(data: Data(bytes.prefix(amount)))
    }
    var after = stat()
    var current = stat()
    guard fstat(descriptor, &after) == 0, lstat(url.path, &current) == 0,
          FileIdentity(before) == FileIdentity(after),
          FileIdentity(after) == FileIdentity(current) else {
        throw NSError(domain: "FGMetalP2", code: 69,
                      userInfo: [NSLocalizedDescriptionKey: "reviewed helper changed during its native identity check"])
    }
    return hasher.finalize().map { String(format: "%02x", $0) }.joined()
}

private func fullFileIdentity(_ value: stat) -> [String: Any] {
    let birthNS = Int64(value.st_birthtimespec.tv_sec) * 1_000_000_000
        + Int64(value.st_birthtimespec.tv_nsec)
    let mtimeNS = Int64(value.st_mtimespec.tv_sec) * 1_000_000_000
        + Int64(value.st_mtimespec.tv_nsec)
    let ctimeNS = Int64(value.st_ctimespec.tv_sec) * 1_000_000_000
        + Int64(value.st_ctimespec.tv_nsec)
    return [
        "device": UInt64(value.st_dev), "inode": UInt64(value.st_ino),
        "logical_bytes": Int64(value.st_size),
        "allocated_bytes": Int64(value.st_blocks) * 512,
        "link_count": UInt64(value.st_nlink),
        "owner_uid": UInt32(value.st_uid), "owner_gid": UInt32(value.st_gid),
        "mode": UInt16(value.st_mode), "birthtime_ns": birthNS,
        "mtime_ns": mtimeNS, "ctime_ns": ctimeNS,
    ]
}

private func readRestore(_ url: URL, output: EventOutput,
                         shouldAbort: () -> Bool) async throws -> (VZMacOSRestoreImage, Data, String, [String: Any], P1RestoreFileIdentity) {
    guard url.isFileURL, url.standardizedFileURL.path == url.path else {
        throw NSError(domain: "FGMetalP2", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "restore path must be a standardized local file URL"])
    }
    let descriptor = open(url.path, O_RDONLY | O_NOFOLLOW)
    guard descriptor >= 0 else {
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                      userInfo: [NSLocalizedDescriptionKey: "could not open the accepted local IPSW without following links"])
    }
    defer { close(descriptor) }
    var before = stat()
    guard fstat(descriptor, &before) == 0, (before.st_mode & S_IFMT) == S_IFREG,
          before.st_nlink == 1, before.st_size == 26_637_307_067 else {
        throw NSError(domain: "FGMetalP2", code: 2,
                      userInfo: [NSLocalizedDescriptionKey: "local IPSW type, link count, or exact size changed"])
    }
    output.emit(["event": "restore_hash_started", "file_identity": FileIdentity(before).json,
                 "captured_at_unix": Date().timeIntervalSince1970])
    var hasher = SHA256()
    var buffer = [UInt8](repeating: 0, count: 8 * 1024 * 1024)
    while true {
        if shouldAbort() {
            throw NSError(domain: "FGMetalP2", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "monitor stop was requested during local IPSW revalidation"])
        }
        let count = read(descriptor, &buffer, buffer.count)
        if count < 0 {
            if errno == EINTR { continue }
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                          userInfo: [NSLocalizedDescriptionKey: "local IPSW hash read failed"])
        }
        if count == 0 { break }
        hasher.update(data: Data(buffer[0..<count]))
    }
    var after = stat()
    guard fstat(descriptor, &after) == 0 else {
        throw NSError(domain: "FGMetalP2", code: 4,
                      userInfo: [NSLocalizedDescriptionKey: "local IPSW descriptor could not be restated"])
    }
    let identityBefore = FileIdentity(before)
    let identityAfter = FileIdentity(after)
    var pathAfter = stat()
    guard lstat(url.path, &pathAfter) == 0,
          (pathAfter.st_mode & S_IFMT) == S_IFREG,
          pathAfter.st_nlink == 1,
          FileIdentity(pathAfter) == identityAfter else {
        throw NSError(domain: "FGMetalP2", code: 4,
                      userInfo: [NSLocalizedDescriptionKey: "local IPSW path no longer names the verified open file"])
    }
    let digest = hasher.finalize().map { String(format: "%02x", $0) }.joined()
    guard identityBefore == identityAfter,
          digest == expectedRestoreSHA256 else {
        throw NSError(domain: "FGMetalP2", code: 5,
                      userInfo: [NSLocalizedDescriptionKey: "local IPSW identity or SHA-256 differs from accepted P1"])
    }
    output.emit(["event": "restore_hash_verified", "sha256": digest,
                 "file_identity": identityAfter.json,
                 "captured_at_unix": Date().timeIntervalSince1970])
    let image = try await VZMacOSRestoreImage.image(from: url)
    var descriptorAfterParse = stat()
    guard fstat(descriptor, &descriptorAfterParse) == 0 else {
        throw NSError(domain: "FGMetalP2", code: 4,
                      userInfo: [NSLocalizedDescriptionKey: "local IPSW descriptor could not be restated after restore metadata parsing"])
    }
    let identityAfterParse = FileIdentity(descriptorAfterParse)
    var pathAfterParse = stat()
    guard lstat(url.path, &pathAfterParse) == 0,
          (pathAfterParse.st_mode & S_IFMT) == S_IFREG,
          pathAfterParse.st_nlink == 1,
          identityAfterParse == identityAfter,
          FileIdentity(pathAfterParse) == identityAfterParse else {
        throw NSError(domain: "FGMetalP2", code: 4,
                      userInfo: [NSLocalizedDescriptionKey: "local IPSW file identity changed during restore metadata parsing"])
    }
    guard image.isSupported, let requirements = image.mostFeaturefulSupportedConfiguration,
          requirements.hardwareModel.isSupported,
          image.buildVersion == "26A434" else {
        throw NSError(domain: "FGMetalP2", code: 6,
                      userInfo: [NSLocalizedDescriptionKey: "accepted local restore image metadata is unsupported or differs from P1"])
    }
    let modelData = requirements.hardwareModel.dataRepresentation
    let modelHash = sha256(modelData)
    let version = image.operatingSystemVersion
    let versionString = "\(version.majorVersion).\(version.minorVersion).\(version.patchVersion)"
    output.emit(["event": "local_restore_compatibility_observed",
                 "restore_sha256": digest,
                 "restore_file_identity_after_hash": identityAfter.json,
                 "restore_file_identity_after_parse": identityAfterParse.json,
                 "restore_parse_source": "VZMacOSRestoreImage.image(from: verified local IPSW URL)",
                 "operating_system_version": versionString,
                 "build_version": image.buildVersion,
                 "image_supported": image.isSupported,
                 "hardware_model_supported": requirements.hardwareModel.isSupported,
                 "hardware_model_sha256": modelHash,
                 "minimum_supported_cpu_count": requirements.minimumSupportedCPUCount,
                 "minimum_supported_memory_size_bytes": requirements.minimumSupportedMemorySize,
                 "captured_at_unix": Date().timeIntervalSince1970])
    let expectedModelHash = "976e66740c64041cf9f127588f93eabf7efb9e7d3cbaee9582bcded88ba5e8d3"
    guard modelHash == expectedModelHash else {
        throw NSError(domain: "FGMetalP2", code: 7,
                      userInfo: [NSLocalizedDescriptionKey:
                                    "local restore hardware model differs from reconciled P2 local compatibility identity (expected SHA-256 \(expectedModelHash), actual SHA-256 \(modelHash))"])
    }
    guard version.majorVersion == 27 && version.minorVersion == 0 && version.patchVersion == 1,
          requirements.minimumSupportedCPUCount == 2,
          requirements.minimumSupportedMemorySize == 4 * 1024 * 1024 * 1024 else {
        throw NSError(domain: "FGMetalP2", code: 8,
                      userInfo: [NSLocalizedDescriptionKey: "accepted restore requirements exceed the P1 host-compatible minimum"])
    }
    let metadata: [String: Any] = [
        "restore_sha256": digest,
        "restore_file_identity": identityAfter.json,
        "hardware_model_data_base64": modelData.base64EncodedString(),
        "hardware_model_sha256": modelHash,
        "build_version": image.buildVersion,
        "operating_system_version": "\(version.majorVersion).\(version.minorVersion).\(version.patchVersion)",
        "minimum_supported_cpu_count": requirements.minimumSupportedCPUCount,
        "minimum_supported_memory_size_bytes": requirements.minimumSupportedMemorySize,
    ]
    return (image, modelData, modelHash, metadata, identityAfter.p1Identity)
}

private func openProvisioningDirectory(systemURL: URL, auxiliaryURL: URL) throws -> (Int32, DirectoryIdentity) {
    let root = "/Volumes/FGMetal-VM"
    let baseURL = systemURL.deletingLastPathComponent().standardizedFileURL
    let baseName = baseURL.lastPathComponent
    guard systemURL.standardizedFileURL.path == "\(root)/templates/\(baseName)/system.raw",
          auxiliaryURL.standardizedFileURL.path == "\(root)/templates/\(baseName)/auxiliary.bin",
          baseName.hasPrefix("macos-base-"), baseName.count > "macos-base-".count,
          baseName.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.contains($0) || $0 == "-" }) else {
        throw NSError(domain: "FGMetalP2", code: 40,
                      userInfo: [NSLocalizedDescriptionKey: "admitted P2 paths are outside the fixed VM-volume template layout"])
    }
    let rootFD = open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    guard rootFD >= 0 else {
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                      userInfo: [NSLocalizedDescriptionKey: "VM volume root could not be opened without following links"])
    }
    var rootStat = stat()
    guard fstat(rootFD, &rootStat) == 0, (rootStat.st_mode & S_IFMT) == S_IFDIR else {
        close(rootFD)
        throw NSError(domain: "FGMetalP2", code: 41,
                      userInfo: [NSLocalizedDescriptionKey: "VM volume root is not a directory"])
    }
    let templatesFD = openat(rootFD, "templates", O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    close(rootFD)
    guard templatesFD >= 0 else {
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                      userInfo: [NSLocalizedDescriptionKey: "template directory could not be opened without following links"])
    }
    var templatesStat = stat()
    guard fstat(templatesFD, &templatesStat) == 0,
          (templatesStat.st_mode & S_IFMT) == S_IFDIR,
          templatesStat.st_dev == rootStat.st_dev else {
        close(templatesFD)
        throw NSError(domain: "FGMetalP2", code: 42,
                      userInfo: [NSLocalizedDescriptionKey: "template directory is not on the admitted VM volume"])
    }
    let baseFD = openat(templatesFD, baseName, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    close(templatesFD)
    guard baseFD >= 0 else {
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                      userInfo: [NSLocalizedDescriptionKey: "run-specific base directory could not be opened without following links"])
    }
    var baseStat = stat()
    guard fstat(baseFD, &baseStat) == 0,
          (baseStat.st_mode & S_IFMT) == S_IFDIR,
          baseStat.st_dev == rootStat.st_dev else {
        close(baseFD)
        throw NSError(domain: "FGMetalP2", code: 43,
                      userInfo: [NSLocalizedDescriptionKey: "run-specific base directory is not on the admitted VM volume"])
    }
    return (baseFD, DirectoryIdentity(baseStat))
}

private func validateProvisioningDirectory(_ url: URL, descriptor: Int32,
                                           expected: DirectoryIdentity) throws {
    var descriptorStat = stat()
    var pathStat = stat()
    guard fstat(descriptor, &descriptorStat) == 0,
          lstat(url.path, &pathStat) == 0,
          (pathStat.st_mode & S_IFMT) == S_IFDIR,
          DirectoryIdentity(descriptorStat) == expected,
          DirectoryIdentity(pathStat) == expected else {
        throw NSError(domain: "FGMetalP2", code: 44,
                      userInfo: [NSLocalizedDescriptionKey: "run-specific base directory identity changed during provisioning"])
    }
}

private func writeExclusive(_ data: Data, name: String, directoryURL: URL,
                            directoryFD: Int32, directoryIdentity: DirectoryIdentity,
                            mode: mode_t = 0o600) throws {
    try validateProvisioningDirectory(directoryURL, descriptor: directoryFD, expected: directoryIdentity)
    let descriptor = openat(directoryFD, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, mode)
    guard descriptor >= 0 else {
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                      userInfo: [NSLocalizedDescriptionKey: "exclusive no-follow artifact creation failed for \(directoryURL.appendingPathComponent(name).path)"])
    }
    defer { close(descriptor) }
    var offset = 0
    try data.withUnsafeBytes { rawBuffer in
        guard let base = rawBuffer.baseAddress else { return }
        while offset < rawBuffer.count {
            let amount = Darwin.write(descriptor, base.advanced(by: offset), rawBuffer.count - offset)
            if amount < 0 {
                if errno == EINTR { continue }
                throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                              userInfo: [NSLocalizedDescriptionKey: "artifact write failed"])
            }
            offset += amount
        }
    }
    guard fsync(descriptor) == 0 else {
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                      userInfo: [NSLocalizedDescriptionKey: "artifact fsync failed"])
    }
    try validateProvisioningDirectory(directoryURL, descriptor: directoryFD, expected: directoryIdentity)
    let fileURL = directoryURL.appendingPathComponent(name)
    var fileStat = stat()
    guard lstat(fileURL.path, &fileStat) == 0,
          (fileStat.st_mode & S_IFMT) == S_IFREG,
          fileStat.st_nlink == 1,
          UInt64(fileStat.st_dev) == directoryIdentity.device else {
        throw NSError(domain: "FGMetalP2", code: 45,
                      userInfo: [NSLocalizedDescriptionKey: "created P2 identity file is outside the admitted VM volume"])
    }
}

private func makeSparseRaw(at url: URL, directoryURL: URL,
                           directoryFD: Int32, directoryIdentity: DirectoryIdentity) throws {
    try validateProvisioningDirectory(directoryURL, descriptor: directoryFD, expected: directoryIdentity)
    let descriptor = openat(directoryFD, "system.raw", O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
    guard descriptor >= 0 else {
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                      userInfo: [NSLocalizedDescriptionKey: "exclusive RAW system image creation failed"])
    }
    defer { close(descriptor) }
    guard ftruncate(descriptor, off_t(systemLogicalCapacity)) == 0, fsync(descriptor) == 0 else {
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                      userInfo: [NSLocalizedDescriptionKey: "sparse RAW system image sizing failed"])
    }
    try validateProvisioningDirectory(directoryURL, descriptor: directoryFD, expected: directoryIdentity)
    var fileStat = stat()
    guard lstat(url.path, &fileStat) == 0,
          (fileStat.st_mode & S_IFMT) == S_IFREG,
          fileStat.st_nlink == 1,
          UInt64(fileStat.st_dev) == directoryIdentity.device else {
        throw NSError(domain: "FGMetalP2", code: 46,
                      userInfo: [NSLocalizedDescriptionKey: "created sparse RAW is outside the admitted VM volume"])
    }
}

private func installProgressReporter(_ session: ProvisioningSession,
                                     output: EventOutput) -> DispatchSourceTimer {
    let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))
    timer.schedule(deadline: .now() + 1, repeating: 5)
    timer.setEventHandler {
        guard let progress = session.progressSnapshot() else { return }
        let fraction: Any = progress.fractionCompleted.isFinite
            ? progress.fractionCompleted as Any : NSNull()
        output.emit(["event": "installer_progress",
                     "fraction_completed": fraction,
                     "completed_unit_count": progress.completedUnitCount,
                     "total_unit_count": progress.totalUnitCount,
                     "cancelled": progress.isCancelled,
                     "captured_at_unix": Date().timeIntervalSince1970])
    }
    timer.resume()
    return timer
}

private func startControlReader(session: ProvisioningSession,
                                output: EventOutput) {
    DispatchQueue.global(qos: .userInitiated).async {
        while let line = readLine() {
            let fields = line.trimmingCharacters(in: .whitespacesAndNewlines)
                .split(whereSeparator: \.isWhitespace).map(String.init)
            guard let command = fields.first else { continue }
            switch command {
            case "START" where fields.count == 1:
                session.requestProvisionStart()
            case "AUTH" where fields.count == 4:
                session.requestLaunchAuthorization(fields[1], tokenBase64: fields[2],
                                                   signature: fields[3])
            case "MONITOR_GATE" where fields.count == 3:
                session.requestMonitorHeartbeat(fields[1], signature: fields[2], isGate: true)
            case "HEARTBEAT" where fields.count == 3:
                session.requestMonitorHeartbeat(fields[1], signature: fields[2], isGate: false)
            case "CANCEL" where fields.count == 1:
                session.requestCancel()
            case "STOP" where fields.count == 1:
                session.requestStop()
            default:
                output.emit(["event": "unknown_control_command", "captured_at_unix": Date().timeIntervalSince1970])
            }
        }
        // Losing the adapter/control pipe is a fail-closed stop request. This
        // also prevents an unauthenticated helper from waiting forever before
        // its monitor gate has been accepted.
        session.requestCancel()
        session.requestStop()
        output.emit(["event": "control_pipe_eof", "captured_at_unix": Date().timeIntervalSince1970])
    }
}

private func plan(restorePath: String, output: EventOutput) async throws {
    let url = URL(fileURLWithPath: restorePath).standardizedFileURL
    let (image, modelData, modelHash, metadata, restoreFileIdentity) = try await readRestore(
        url, output: output, shouldAbort: { false })
    let machineIdentifierCreationStartedAtUnix = Date().timeIntervalSince1970
    let machineIdentifierCreationStartedMonotonic = ProcessInfo.processInfo.systemUptime
    let machineIdentifier = VZMacMachineIdentifier()
    let machineIdentifierCreatedAtUnix = Date().timeIntervalSince1970
    let machineIdentifierCreatedMonotonic = ProcessInfo.processInfo.systemUptime
    output.emit([
        "event": "plan",
        "is_supported": image.isSupported,
        "hardware_model_data_base64": modelData.base64EncodedString(),
        "hardware_model_sha256": modelHash,
        "machine_identifier_data_base64": machineIdentifier.dataRepresentation.base64EncodedString(),
        "machine_identifier_sha256": sha256(machineIdentifier.dataRepresentation),
        "machine_identifier_creation": [
            "method": "VZMacMachineIdentifier() exactly once during the signed P2 identity-plan command",
            "creation_started_at_unix": machineIdentifierCreationStartedAtUnix,
            "created_at_unix": machineIdentifierCreatedAtUnix,
            "creation_started_monotonic_seconds": machineIdentifierCreationStartedMonotonic,
            "created_monotonic_seconds": machineIdentifierCreatedMonotonic,
        ],
        "restore_file_identity": restoreFileIdentity.json,
        "metadata": metadata,
        "plan_only": true,
        "captured_at_unix": Date().timeIntervalSince1970,
    ])
}

@MainActor
private func provision(restorePath: String,
                       systemPath: String,
                       auxiliaryPath: String,
                       machineIdentifierBase64: String,
                       hardwareModelBase64: String,
                       expectedMachineIdentifierSHA256: String,
                       machineIdentifierCreatedAtUnix: Double,
                       expectedRestoreIdentityBase64: String,
                       launchContext: P2LaunchContext,
                       output: EventOutput) async throws {
    let session = ProvisioningSession(output: output, launchContext: launchContext)
    startControlReader(session: session, output: output)
    do {
        try await provisionBody(restorePath: restorePath, systemPath: systemPath,
                                auxiliaryPath: auxiliaryPath,
                                machineIdentifierBase64: machineIdentifierBase64,
                                hardwareModelBase64: hardwareModelBase64,
                                expectedMachineIdentifierSHA256: expectedMachineIdentifierSHA256,
                                machineIdentifierCreatedAtUnix: machineIdentifierCreatedAtUnix,
                                expectedRestoreIdentityBase64: expectedRestoreIdentityBase64,
                                session: session, output: output)
        session.emitHandlesReleased()
    } catch {
        session.requestCancel()
        session.requestStop()
        do {
            _ = try await session.stopAndRelease()
            session.emitHandlesReleased()
        }
        catch { output.emit(["event": "release_failed", "error": String(describing: error),
                             "captured_at_unix": Date().timeIntervalSince1970]) }
        throw error
    }
}

@MainActor
private func provisionBody(restorePath: String,
                           systemPath: String,
                           auxiliaryPath: String,
                           machineIdentifierBase64: String,
                           hardwareModelBase64: String,
                           expectedMachineIdentifierSHA256: String,
                           machineIdentifierCreatedAtUnix: Double,
                           expectedRestoreIdentityBase64: String,
                           session: ProvisioningSession,
                           output: EventOutput) async throws {
    try await session.awaitProvisionStart()
    output.emit(["event": "provision_start_authorized", "captured_at_unix": Date().timeIntervalSince1970])
    let monitorWatchdog = Task.detached(priority: .userInitiated) {
        while !Task.isCancelled {
            if session.enforceMonitorHeartbeat() { return }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
    }
    defer { monitorWatchdog.cancel() }
    let restoreURL = URL(fileURLWithPath: restorePath).standardizedFileURL
    let (image, hardwareModelData, hardwareModelHash, restoreMetadata, restoreFileIdentity) = try await readRestore(
        restoreURL, output: output, shouldAbort: { session.abortRequested() })
    guard let requirements = image.mostFeaturefulSupportedConfiguration,
          requirements.hardwareModel.dataRepresentation.base64EncodedString() == hardwareModelBase64 else {
        throw NSError(domain: "FGMetalP2", code: 10,
                      userInfo: [NSLocalizedDescriptionKey: "admitted hardware model bytes differ from the accepted local IPSW"])
    }
    guard let machineData = Data(base64Encoded: machineIdentifierBase64),
          sha256(machineData) == expectedMachineIdentifierSHA256,
          let machineIdentifier = VZMacMachineIdentifier(dataRepresentation: machineData) else {
        throw NSError(domain: "FGMetalP2", code: 11,
                      userInfo: [NSLocalizedDescriptionKey: "admitted machine identifier is malformed or does not match its hash"])
    }
    guard let expectedIdentityData = Data(base64Encoded: expectedRestoreIdentityBase64),
          let expectedRestoreIdentity = try? JSONDecoder().decode(P1RestoreFileIdentity.self,
                                                                  from: expectedIdentityData),
          expectedRestoreIdentity == restoreFileIdentity else {
        throw NSError(domain: "FGMetalP2", code: 17,
                      userInfo: [NSLocalizedDescriptionKey: "restore file identity differs from the signed P2 admission"])
    }
    guard !session.abortRequested() else {
        throw NSError(domain: "FGMetalP2", code: 12,
                      userInfo: [NSLocalizedDescriptionKey: "monitor stop was requested before provisioning assets were created"])
    }

    let systemURL = URL(fileURLWithPath: systemPath).standardizedFileURL
    let auxiliaryURL = URL(fileURLWithPath: auxiliaryPath).standardizedFileURL
    let baseURL = systemURL.deletingLastPathComponent()
    let (baseDirectoryFD, baseDirectoryIdentity) = try openProvisioningDirectory(
        systemURL: systemURL, auxiliaryURL: auxiliaryURL)
    defer { close(baseDirectoryFD) }
    try writeExclusive(machineData, name: "machine-identifier.bin", directoryURL: baseURL,
                       directoryFD: baseDirectoryFD, directoryIdentity: baseDirectoryIdentity)
    try writeExclusive(hardwareModelData, name: "hardware-model.bin", directoryURL: baseURL,
                       directoryFD: baseDirectoryFD, directoryIdentity: baseDirectoryIdentity)
    output.emit(["event": "platform_identity_persisted",
                 "machine_identifier_sha256": sha256(machineData),
                 "machine_identifier_data_base64": machineData.base64EncodedString(),
                 "machine_identifier_creation_method": "VZMacMachineIdentifier() exactly once during the signed P2 identity-plan command",
                 "machine_identifier_created_at_unix": machineIdentifierCreatedAtUnix,
                 "machine_identifier_reused_unchanged_through_install_and_shutdown": true,
                 "hardware_model_sha256": hardwareModelHash,
                 "hardware_model_data_base64": hardwareModelData.base64EncodedString(),
                 "hardware_model_source": "VZMacOSRestoreImage.mostFeaturefulSupportedConfiguration for the P1-accepted local IPSW",
                 "restore_image_supported": image.isSupported,
                 "hardware_model_compatibility_outcome": "exact dataRepresentation matched the signed P2 plan",
                 "captured_at_unix": Date().timeIntervalSince1970])

    try makeSparseRaw(at: systemURL, directoryURL: baseURL,
                      directoryFD: baseDirectoryFD, directoryIdentity: baseDirectoryIdentity)
    output.emit(["event": "system_raw_created", "path": systemURL.path,
                 "api": "openat(O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW) plus ftruncate and fsync",
                 "file_format": "sparse RAW regular file",
                 "exclusive_creation": true,
                 "logical_capacity_bytes": systemLogicalCapacity,
                 "file_identity": try fileIdentity(at: systemURL,
                                                    expectedVolumeDevice: baseDirectoryIdentity.device),
                 "captured_at_unix": Date().timeIntervalSince1970])
    guard !session.abortRequested() else {
        throw NSError(domain: "FGMetalP2", code: 13,
                      userInfo: [NSLocalizedDescriptionKey: "monitor stop was requested before auxiliary storage creation"])
    }

    try validateProvisioningDirectory(baseURL, descriptor: baseDirectoryFD,
                                      expected: baseDirectoryIdentity)
    let auxiliaryStorage = try VZMacAuxiliaryStorage(
        creatingStorageAt: auxiliaryURL, hardwareModel: requirements.hardwareModel, options: [])
    try validateProvisioningDirectory(baseURL, descriptor: baseDirectoryFD,
                                      expected: baseDirectoryIdentity)
    output.emit(["event": "auxiliary_storage_created", "api": "VZMacAuxiliaryStorage(creatingStorageAt:hardwareModel:options:)",
                 "path": auxiliaryURL.path,
                 "hardware_model_sha256": hardwareModelHash,
                 "options": [],
                 "file_identity": try fileIdentity(at: auxiliaryURL,
                                                    expectedVolumeDevice: baseDirectoryIdentity.device),
                 "captured_at_unix": Date().timeIntervalSince1970])

    try validateProvisioningDirectory(baseURL, descriptor: baseDirectoryFD,
                                      expected: baseDirectoryIdentity)
    let attachment = try VZDiskImageStorageDeviceAttachment(url: systemURL, readOnly: false)
    _ = try fileIdentity(at: systemURL, expectedVolumeDevice: baseDirectoryIdentity.device)
    let storageDevice = VZVirtioBlockDeviceConfiguration(attachment: attachment)
    let platform = VZMacPlatformConfiguration()
    platform.hardwareModel = requirements.hardwareModel
    platform.machineIdentifier = machineIdentifier
    platform.auxiliaryStorage = auxiliaryStorage

    let configuration = VZVirtualMachineConfiguration()
    configuration.platform = platform
    configuration.bootLoader = VZMacOSBootLoader()
    configuration.cpuCount = max(2, requirements.minimumSupportedCPUCount)
    configuration.memorySize = max(UInt64(4 * 1024 * 1024 * 1024), requirements.minimumSupportedMemorySize)
    configuration.storageDevices = [storageDevice]
    configuration.networkDevices = []
    configuration.directorySharingDevices = []
    configuration.serialPorts = []
    configuration.consoleDevices = []
    configuration.socketDevices = []
    configuration.usbControllers = []
    configuration.graphicsDevices = []
    try configuration.validate()

    let configurationRecord: [String: Any] = [
        "bootLoaderType": "VZMacOSBootLoader",
        "cpuCount": configuration.cpuCount,
        "memoryBytes": configuration.memorySize,
        "networkDevices": [],
        "directorySharingDevices": [],
        "serialDevices": [],
        "serialPorts": [],
        "usbDevices": [],
        "usbControllers": [],
        "socketDevices": [],
        "consoleDevices": [],
        "graphicsDevices": [],
        "storageDevices": [systemURL.path],
        "storageDeviceFormat": "sparse RAW regular file",
        "storageDeviceAttachmentType": "VZDiskImageStorageDeviceAttachment",
        "storageDeviceReadOnly": false,
        "writableAttachmentOwner": "only the P2 provisioning VZVirtualMachine during installer execution",
        "restorePath": restoreURL.path,
        "hardwareModelDataBase64": hardwareModelData.base64EncodedString(),
        "hardwareModelSHA256": hardwareModelHash,
        "machineIdentifierDataBase64": machineData.base64EncodedString(),
        "machineIdentifierSHA256": sha256(machineData),
        "auxiliaryStoragePath": auxiliaryURL.path,
        "auxiliaryStorageHardwareModelSHA256": hardwareModelHash,
        "platformType": "VZMacPlatformConfiguration",
        "validationResult": "VZVirtualMachineConfiguration.validate() returned successfully",
        "validationOccurredBeforeVMCreation": true,
    ]
    let configurationReadback: [String: Int] = [
        "storage_devices": configuration.storageDevices.count,
        "network_devices": configuration.networkDevices.count,
        "directory_sharing_devices": configuration.directorySharingDevices.count,
        "serial_ports": configuration.serialPorts.count,
        "console_devices": configuration.consoleDevices.count,
        "socket_devices": configuration.socketDevices.count,
        "usb_controllers": configuration.usbControllers.count,
        "graphics_devices": configuration.graphicsDevices.count,
    ]
    guard configurationReadback["storage_devices"] == 1,
          configurationReadback.filter({ $0.key != "storage_devices" }).values.allSatisfy({ $0 == 0 }) else {
        throw NSError(domain: "FGMetalP2", code: 23,
                      userInfo: [NSLocalizedDescriptionKey: "validated configuration readback does not match the admitted device policy"])
    }
    output.emit(["event": "configuration_validated",
                 "validation_result": "success",
                 "configuration_record": configurationRecord,
                 "cpu_count": configuration.cpuCount,
                 "memory_bytes": configuration.memorySize,
                 "storage_device_count": configuration.storageDevices.count,
                 "network_devices": configuration.networkDevices.count,
                 "directory_sharing_devices": configuration.directorySharingDevices.count,
                 "serial_ports": configuration.serialPorts.count,
                 "console_devices": configuration.consoleDevices.count,
                 "socket_devices": configuration.socketDevices.count,
                 "usb_controllers": configuration.usbControllers.count,
                 "graphics_devices": configuration.graphicsDevices.count,
                 "runtime_readback_counts": configurationReadback,
                 "hardware_model_sha256": hardwareModelHash,
                 "machine_identifier_sha256": sha256(machineData),
                 "restore_subject": ["macos_version": restoreMetadata["operating_system_version"] ?? "unknown",
                                     "macos_build": restoreMetadata["build_version"] ?? "unknown"],
                 "captured_at_unix": Date().timeIntervalSince1970])

    _ = try verifyRestoreIdentity(restoreURL, expected: expectedRestoreIdentity)
    try validateProvisioningDirectory(baseURL, descriptor: baseDirectoryFD,
                                      expected: baseDirectoryIdentity)
    let virtualMachine = VZVirtualMachine(configuration: configuration)
    let installerRestorePathStat = try verifyRestoreIdentity(restoreURL, expected: expectedRestoreIdentity)
    let installerIdentityCheckedAtUnix = Date().timeIntervalSince1970
    let installerIdentityCheckedAtMonotonic = ProcessInfo.processInfo.systemUptime
    output.emit(["event": "installer_restore_identity_verified",
                 "path": restoreURL.path,
                 "sha256": restoreMetadata["restore_sha256"] ?? "unknown",
                 "restore_subject": ["macos_version": restoreMetadata["operating_system_version"] ?? "unknown",
                                     "macos_build": restoreMetadata["build_version"] ?? "unknown"],
                 "hardware_model_sha256": hardwareModelHash,
                 "file_identity": fullFileIdentity(installerRestorePathStat),
                 "identity_checked_at_unix": installerIdentityCheckedAtUnix,
                 "identity_checked_monotonic_seconds": installerIdentityCheckedAtMonotonic,
                 "verification_method": "O_NOFOLLOW descriptor fstat plus path lstat matched signed P2 restore identity immediately before VZMacOSInstaller construction",
                 "captured_at_unix": Date().timeIntervalSince1970])
    let installer = VZMacOSInstaller(virtualMachine: virtualMachine, restoringFromImageAt: restoreURL)
    let installerRestoreAfterConstruction = try verifyRestoreIdentity(restoreURL, expected: expectedRestoreIdentity)
    output.emit(["event": "installer_restore_identity_after_construction",
                 "path": restoreURL.path,
                 "sha256": restoreMetadata["restore_sha256"] ?? "unknown",
                 "file_identity": fullFileIdentity(installerRestoreAfterConstruction),
                 "identity_matches_preconstruction": FileIdentity(installerRestoreAfterConstruction) == FileIdentity(installerRestorePathStat),
                 "captured_at_unix": Date().timeIntervalSince1970])
    try validateProvisioningDirectory(baseURL, descriptor: baseDirectoryFD,
                                      expected: baseDirectoryIdentity)
    _ = try fileIdentity(at: systemURL, expectedVolumeDevice: baseDirectoryIdentity.device)
    _ = try fileIdentity(at: auxiliaryURL, expectedVolumeDevice: baseDirectoryIdentity.device)
    session.retain(vm: virtualMachine, installer: installer,
                   attachment: attachment, configuration: configuration)
    let runtimeReadback: [String: Int] = [
        "storage_devices": configuration.storageDevices.count,
        "network_devices": virtualMachine.networkDevices.count,
        "directory_sharing_devices": virtualMachine.directorySharingDevices.count,
        "console_devices": virtualMachine.consoleDevices.count,
        "socket_devices": virtualMachine.socketDevices.count,
        "usb_controllers": virtualMachine.usbControllers.count,
        "graphics_devices": virtualMachine.graphicsDevices.count,
    ]
    guard runtimeReadback["storage_devices"] == 1,
          runtimeReadback.filter({ $0.key != "storage_devices" }).values.allSatisfy({ $0 == 0 }) else {
        throw NSError(domain: "FGMetalP2", code: 23,
                      userInfo: [NSLocalizedDescriptionKey: "runtime configuration unexpectedly contains network, sharing, console, socket, USB, or graphics devices"])
    }
    output.emit(["event": "configuration_runtime_readback",
                 "runtime_readback_counts": runtimeReadback,
                 "storage_path": systemURL.path,
                 "network_devices": [],
                 "directory_sharing_devices": [],
                 "serial_devices": [],
                 "usb_devices": [],
                 "socket_devices": [],
                 "vm_created": true,
                 "vm_started": false,
                 "captured_at_unix": Date().timeIntervalSince1970])
    guard !session.abortRequested() else {
        throw NSError(domain: "FGMetalP2", code: 14,
                      userInfo: [NSLocalizedDescriptionKey: "cancelled before installer start"])
    }

    let progressTimer = installProgressReporter(session, output: output)
    defer { progressTimer.cancel() }
    let selectedInstaller = try session.requestInstallStart()
    let installerStartRestoreIdentity = try verifyRestoreIdentity(restoreURL, expected: expectedRestoreIdentity)
    let installerStartCheckedAtUnix = Date().timeIntervalSince1970
    let installerStartCheckedAtMonotonic = ProcessInfo.processInfo.systemUptime
    output.emit(["event": "installer_restore_identity_verified_at_start",
                 "path": restoreURL.path,
                 "sha256": restoreMetadata["restore_sha256"] ?? "unknown",
                 "file_identity": fullFileIdentity(installerStartRestoreIdentity),
                 "identity_checked_at_unix": installerStartCheckedAtUnix,
                 "identity_checked_monotonic_seconds": installerStartCheckedAtMonotonic,
                 "verification_method": "O_NOFOLLOW descriptor fstat plus path lstat matched signed P2 admission immediately before VZMacOSInstaller.install",
                 "captured_at_unix": Date().timeIntervalSince1970])
    output.emit(["event": "installer_started",
                 "source": "accepted local IPSW",
                 "api": "VZMacOSInstaller.install(completionHandler:)",
                 "restore_sha256": restoreMetadata["restore_sha256"] ?? "unknown",
                 "restore_file_identity": fullFileIdentity(installerStartRestoreIdentity),
                 "restore_identity_checked_at_unix": installerStartCheckedAtUnix,
                 "restore_identity_checked_monotonic_seconds": installerStartCheckedAtMonotonic,
                 "restore_subject": ["macos_version": restoreMetadata["operating_system_version"] ?? "unknown",
                                     "macos_build": restoreMetadata["build_version"] ?? "unknown"],
                 "captured_at_unix": Date().timeIntervalSince1970])
    let installResult = await withCheckedContinuation {
        (continuation: CheckedContinuation<Result<Void, any Error>, Never>) in
        selectedInstaller.install { result in
            let succeeded: Bool
            switch result {
            case .success: succeeded = true
            case .failure: succeeded = false
            }
            session.operationFinished(succeeded)
            continuation.resume(returning: result)
        }
        session.installInvocationStarted()
    }
    let installSuccess: Bool
    let installFailure: String?
    switch installResult {
    case .success:
        installSuccess = true
        installFailure = nil
    case .failure(let error):
        installSuccess = false
        installFailure = String(describing: error)
    }
    output.emit(["event": "installer_result", "installer_completed": installSuccess,
                 "failure": installFailure as Any? ?? NSNull(),
                 "restore_sha256": restoreMetadata["restore_sha256"] ?? "unknown",
                 "captured_at_unix": Date().timeIntervalSince1970])
    progressTimer.cancel()

    while !session.monitorState().stopRequested {
        try await Task.sleep(nanoseconds: 100_000_000)
    }
    let release = try await session.stopAndRelease()
    output.emit(["event": "helper_result", "installer_completed": installSuccess,
                 "failure": installFailure as Any? ?? NSNull(),
                 "release": release,
                 "restore_sha256": restoreMetadata["restore_sha256"] ?? "unknown",
                 "hardware_model_sha256": hardwareModelHash,
                 "machine_identifier_sha256": sha256(machineData),
                 "captured_at_unix": Date().timeIntervalSince1970])
    if !installSuccess {
        throw NSError(domain: "FGMetalP2", code: 15,
                      userInfo: [NSLocalizedDescriptionKey: installFailure ?? "installer failed"])
    }
}

private func fileIdentity(at url: URL, expectedVolumeDevice: UInt64? = nil) throws -> [String: Any] {
    var value = stat()
    guard lstat(url.path, &value) == 0, (value.st_mode & S_IFMT) == S_IFREG,
          value.st_nlink == 1,
          expectedVolumeDevice.map({ UInt64(value.st_dev) == $0 }) ?? true else {
        throw NSError(domain: "FGMetalP2", code: 16,
                      userInfo: [NSLocalizedDescriptionKey: "created artifact is not a single-link regular file"])
    }
    return FileIdentity(value).json
}

@main
private struct P2Provisioner {
    @MainActor
    static func main() async {
        let output = EventOutput()
        let args = Array(CommandLine.arguments.dropFirst())
        do {
            guard let mode = args.first else {
                throw NSError(domain: "FGMetalP2", code: 30,
                              userInfo: [NSLocalizedDescriptionKey: "expected plan or provision command"])
            }
            if mode == "plan", args.count == 2 {
                try await plan(restorePath: args[1], output: output)
                return
            }
            if mode == "provision", args.count == 4 {
                let authorization = try verifyP2LaunchFiles(
                    admissionPath: args[1], manifestPath: args[2], reviewPath: args[3])
                guard let admission = authorization["admission"] as? [String: Any],
                      let expectedBinaryPath = authorization["provisioner_binary_path"] as? String,
                      let expectedBinarySHA256 = authorization["provisioner_binary_sha256"] as? String,
                      let restorePath = admission["restore_path"] as? String,
                      let systemPath = admission["system_raw_path"] as? String,
                      let auxiliaryPath = admission["auxiliary_storage_path"] as? String,
                      let machineIdentifierBase64 = admission["machine_identifier_data_base64"] as? String,
                      let hardwareModelBase64 = admission["hardware_model_data_base64"] as? String,
                      let machineIdentifierSHA256 = admission["machine_identifier_sha256"] as? String,
                      let machineCreation = admission["machine_identifier_creation"] as? [String: Any],
                      let machineIdentifierCreatedAtUnix = machineCreation["created_at_unix"] as? Double,
                      machineIdentifierCreatedAtUnix.isFinite, machineIdentifierCreatedAtUnix > 0,
                      let restoreIdentity = admission["restore_file_identity"] as? [String: Any] else {
                    throw NSError(domain: "FGMetalP2", code: 67,
                                  userInfo: [NSLocalizedDescriptionKey: "verified P2 admission omitted native provisioning inputs"])
                }
                let invokedBinary = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
                let reviewedBinary = p2RepositoryRoot().appendingPathComponent(expectedBinaryPath).standardizedFileURL
                guard invokedBinary == reviewedBinary,
                      try sha256NoFollow(invokedBinary) == expectedBinarySHA256 else {
                    throw NSError(domain: "FGMetalP2", code: 70,
                                  userInfo: [NSLocalizedDescriptionKey: "invoked helper path or bytes differ from the signed reviewed binary identity"])
                }
                let restoreIdentityData = try JSONSerialization.data(withJSONObject: restoreIdentity,
                                                                     options: [.sortedKeys])
                let launchContext = try P2LaunchContext(authorization: authorization)
                output.emit(["event": "p2_signed_admission_verified",
                             "run_id": authorization["run_id"] as? String ?? "unknown",
                             "session_id": authorization["session_id"] as? String ?? "unknown",
                             "admission_sha256": authorization["admission_sha256"] as? String ?? "unknown",
                             "active_marker_sha256": authorization["active_marker_sha256"] as? String ?? "unknown",
                             "provisioner_binary_sha256": authorization["provisioner_binary_sha256"] as? String ?? "unknown",
                             "captured_at_unix": Date().timeIntervalSince1970])
                try await provision(restorePath: restorePath, systemPath: systemPath,
                                    auxiliaryPath: auxiliaryPath, machineIdentifierBase64: machineIdentifierBase64,
                                    hardwareModelBase64: hardwareModelBase64,
                                    expectedMachineIdentifierSHA256: machineIdentifierSHA256,
                                    machineIdentifierCreatedAtUnix: machineIdentifierCreatedAtUnix,
                                    expectedRestoreIdentityBase64: restoreIdentityData.base64EncodedString(),
                                    launchContext: launchContext, output: output)
                return
            }
            throw NSError(domain: "FGMetalP2", code: 31,
                          userInfo: [NSLocalizedDescriptionKey: "invalid command arguments"])
        } catch {
            output.emit(["event": "fatal", "error": String(describing: error),
                         "captured_at_unix": Date().timeIntervalSince1970])
            fputs("P2 provisioner failed: \(error)\n", stderr)
            exit(1)
        }
    }
}
