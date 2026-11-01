import CryptoKit
import Darwin
import Foundation
import Virtualization

private func isAppleHTTPSURL(_ url: URL) -> Bool {
    guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
          components.scheme?.lowercased() == "https",
          components.user == nil,
          components.password == nil,
          components.port == nil || components.port == 443,
          let host = components.host?.lowercased()
    else { return false }

    return host == "apple.com"
        || host.hasSuffix(".apple.com")
        || host.hasSuffix(".cdn-apple.com")
}

private final class AppleRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        guard let url = request.url, isAppleHTTPSURL(url) else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }
}

private func sha256(of url: URL) throws -> String {
    let data = try Data(contentsOf: url, options: [.mappedIfSafe])
    return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

private func sha256(of data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

@main
private struct RestoreMetadataProbe {
    static func main() async {
        do {
            let image = try await VZMacOSRestoreImage.latestSupported
            guard image.isSupported, isAppleHTTPSURL(image.url) else {
                throw NSError(domain: "FGMetalRestoreProbe", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "latest restore URL is not supported Apple HTTPS"])
            }
            guard let requirements = image.mostFeaturefulSupportedConfiguration else {
                throw NSError(domain: "FGMetalRestoreProbe", code: 3,
                              userInfo: [NSLocalizedDescriptionKey: "restore image has no supported VM configuration"])
            }
            let hardwareModel = requirements.hardwareModel
            guard hardwareModel.isSupported,
                  requirements.minimumSupportedCPUCount > 0,
                  requirements.minimumSupportedMemorySize > 0 else {
                throw NSError(domain: "FGMetalRestoreProbe", code: 4,
                              userInfo: [NSLocalizedDescriptionKey: "restore hardware requirements are incomplete or unsupported"])
            }

            var request = URLRequest(url: image.url)
            request.httpMethod = "HEAD"
            request.timeoutInterval = 30
            request.cachePolicy = .reloadIgnoringLocalCacheData
            request.httpShouldHandleCookies = false
            request.setValue("FG-Metal-VM-Restore-Metadata-Probe/1", forHTTPHeaderField: "User-Agent")
            let delegate = AppleRedirectDelegate()
            let configuration = URLSessionConfiguration.ephemeral
            configuration.urlCache = nil
            configuration.httpCookieStorage = nil
            configuration.httpShouldSetCookies = false
            let session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
            defer { session.invalidateAndCancel() }
            let (_, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse,
                  (200...299).contains(http.statusCode),
                  let finalURL = http.url,
                  isAppleHTTPSURL(finalURL),
                  let contentLengthText = http.value(forHTTPHeaderField: "Content-Length"),
                  !contentLengthText.isEmpty,
                  contentLengthText.utf8.allSatisfy({ $0 >= 48 && $0 <= 57 }),
                  let contentLength = Int64(contentLengthText),
                  contentLength > 0
            else {
                throw NSError(domain: "FGMetalRestoreProbe", code: 2,
                              userInfo: [NSLocalizedDescriptionKey: "Apple HEAD response lacks an exact positive Content-Length"])
            }

            let version = image.operatingSystemVersion
            let versionString = "\(version.majorVersion).\(version.minorVersion).\(version.patchVersion)"
            let executableURL = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
            func header(_ name: String) -> Any {
                if let value = http.value(forHTTPHeaderField: name) { return value }
                return NSNull()
            }
            let headers: [String: Any] = [
                "etag": header("ETag"),
                "last_modified": header("Last-Modified"),
                "digest": header("Digest"),
                "content_md5": header("Content-MD5"),
                "x_apple_content_sha256": header("X-Apple-Content-SHA256"),
                "x_apple_signature": header("X-Apple-Signature")
            ]
            let result: [String: Any] = [
                "schema_version": 1,
                "probe": "VZMacOSRestoreImage.latestSupported+HTTPS_HEAD",
                "probe_executable_path": executableURL.path,
                "probe_executable_sha256": try sha256(of: executableURL),
                "source_url": image.url.absoluteString,
                "final_url": finalURL.absoluteString,
                "operating_system_version": versionString,
                "build_version": image.buildVersion,
                "is_supported": image.isSupported,
                "hardware_model_data_sha256": sha256(of: hardwareModel.dataRepresentation),
                "hardware_model_supported": hardwareModel.isSupported,
                "minimum_supported_cpu_count": requirements.minimumSupportedCPUCount,
                "minimum_supported_memory_size_bytes": requirements.minimumSupportedMemorySize,
                "http_status": http.statusCode,
                "content_length_bytes": contentLength,
                "headers": headers,
                "captured_at_unix": Date().timeIntervalSince1970
            ]
            let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
            print(String(decoding: data, as: UTF8.self))
        } catch {
            fputs("Restore metadata probe failed: \(error)\n", stderr)
            exit(1)
        }
    }
}
