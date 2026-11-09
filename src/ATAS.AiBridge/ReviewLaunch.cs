#if MAC_INDICATOR
namespace ATAS.AiBridge;

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

// Credential-bearing values exist only in redirected process memory and the
// one-use native message. Do not make this a record (generated ToString logs fields).
internal sealed class ReviewLaunchResult
{
    internal string Url { get; }
    internal string Token { get; }
    private ReviewLaunchResult(string url, string token) { Url = url; Token = token; }

    internal static bool IsRootUrl(string value) =>
        Uri.TryCreate(value, UriKind.Absolute, out var uri) &&
        uri.Scheme == "http" && uri.Host == "127.0.0.1" && uri.Port >= 8770 && uri.Port <= 8780 &&
        value == "http://127.0.0.1:" + uri.Port.ToString(CultureInfo.InvariantCulture) + "/";

    internal static ReviewLaunchResult Parse(string output)
    {
        if (output == null || output.Length > ReviewLaunch.MaxOutput)
            throw new InvalidOperationException("Invalid native launcher response.");
        try
        {
            using var json = JsonDocument.Parse(output);
            var root = json.RootElement;
            if (root.ValueKind != JsonValueKind.Object || root.EnumerateObject().Count() != 2 ||
                !root.TryGetProperty("url", out var urlValue) || urlValue.ValueKind != JsonValueKind.String ||
                !root.TryGetProperty("token", out var tokenValue) || tokenValue.ValueKind != JsonValueKind.String)
                throw new FormatException();
            var url = urlValue.GetString();
            var token = tokenValue.GetString();
            if (!IsRootUrl(url) || token == null || token.Length < 20 || token.Length > 256 ||
                token.Any(c => !((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                                 (c >= '0' && c <= '9') || c == '_' || c == '-')))
                throw new FormatException();
            return new ReviewLaunchResult(url, token);
        }
        catch (Exception ex) when (ex is JsonException || ex is FormatException)
        {
            // Never attach malformed stdout (which may contain credentials).
            throw new InvalidOperationException("Invalid native launcher response.");
        }
    }
}

internal static class ReviewLaunch
{
    internal const int MaxOutput = 2048;

    internal static void RequireBridgePort(int bridgePort)
    {
        if (bridgePort <= 0 || bridgePort > 65535)
            throw new InvalidOperationException("A live originating bridge port is required. Apply enabled indicator settings and check Bridge status.");
    }

    internal static Task<ReviewLaunchResult> InstalledAsync(int bridgePort, CancellationToken cancellation)
    {
        RequireBridgePort(bridgePort);
        cancellation.ThrowIfCancellationRequested();
        var path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                                "ATAS AI Review", "installation.json");
        if (!File.Exists(path))
            throw new InvalidOperationException("Workbench not installed; run install-workbench.sh --install.");
        try
        {
            using var metadata = JsonDocument.Parse(File.ReadAllText(path));
            var python = metadata.RootElement.GetProperty("python").GetString();
            var launcher = metadata.RootElement.GetProperty("launcher").GetString();
            if (string.IsNullOrWhiteSpace(python) || string.IsNullOrWhiteSpace(launcher) ||
                !Path.IsPathFullyQualified(python) || !Path.IsPathFullyQualified(launcher) ||
                !File.Exists(python) || !File.Exists(launcher))
                throw new FormatException();
            return RunAsync(python, launcher, bridgePort, cancellation, TimeSpan.FromSeconds(10));
        }
        catch (Exception ex) when (ex is JsonException || ex is FormatException ||
                                   ex is KeyNotFoundException || ex is InvalidOperationException)
        {
            throw new InvalidOperationException("Invalid workbench installation; run install-workbench.sh --install.");
        }
    }

    internal static async Task<ReviewLaunchResult> RunAsync(string python, string launcher, int bridgePort,
                                                          CancellationToken cancellation, TimeSpan timeout)
    {
        RequireBridgePort(bridgePort);
        cancellation.ThrowIfCancellationRequested();
        var start = new ProcessStartInfo(python)
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true
        };
        start.ArgumentList.Add(launcher);
        start.ArgumentList.Add("--native-json");
        start.ArgumentList.Add("--bridge-port");
        start.ArgumentList.Add(bridgePort.ToString(CultureInfo.InvariantCulture));
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        deadline.CancelAfter(timeout);
        using var process = Process.Start(start) ?? throw new InvalidOperationException("Workbench launcher could not start.");
        try
        {
            var stdout = ReadBoundedAsync(process.StandardOutput, deadline.Token);
            var stderr = ReadBoundedAsync(process.StandardError, deadline.Token);
            await Task.WhenAll(stdout, stderr, process.WaitForExitAsync(deadline.Token)).ConfigureAwait(false);
            if (process.ExitCode != 0)
                throw new InvalidOperationException("Workbench launcher failed; check installation and local ports 8770–8780.");
            return ReviewLaunchResult.Parse(await stdout.ConfigureAwait(false));
        }
        catch (OperationCanceledException) when (!cancellation.IsCancellationRequested)
        {
            throw new InvalidOperationException("Workbench launcher timed out; retry from indicator settings.");
        }
        finally
        {
            if (!process.HasExited)
            {
                // Kill only our launcher, never the detached companion or ATAS.
                process.Kill();
                await process.WaitForExitAsync().ConfigureAwait(false);
            }
        }
    }

    private static async Task<string> ReadBoundedAsync(StreamReader reader, CancellationToken cancellation)
    {
        var output = new StringBuilder();
        var buffer = new char[256];
        int count;
        while ((count = await reader.ReadAsync(buffer.AsMemory(), cancellation).ConfigureAwait(false)) > 0)
        {
            if (output.Length + count > MaxOutput)
                throw new InvalidOperationException("Native launcher response exceeded its limit.");
            output.Append(buffer, 0, count);
        }
        return output.ToString();
    }
}

internal sealed class ReviewBootstrapDelivery
{
    private ReviewLaunchResult _pending;
    private readonly string _url;
    private string _requestId = Guid.NewGuid().ToString("N");
    private bool _sent;
    private bool _acknowledged;
    internal ReviewBootstrapDelivery(ReviewLaunchResult result) { _pending = result; _url = result.Url; }
    internal void Clear() { _pending = null; _requestId = null; }
    internal string TakeMessage(string currentUrl, bool successful, bool messagingSupported)
    {
        if (_pending == null) return null;
        if (!successful || !messagingSupported || !ReviewLaunchResult.IsRootUrl(currentUrl) || currentUrl != _pending.Url)
            throw new InvalidOperationException("Native review page URL or messaging could not be verified.");
        var token = _pending.Token;
        _pending = null; // A duplicate navigation event must not replay the token.
        _sent = true;
        return JsonSerializer.Serialize(new { type = "atas-ai-review.bootstrap", version = 1, token, requestId = _requestId });
    }

    internal bool TryAcceptAcknowledgement(string message, string source, string currentUrl, out bool ok)
    {
        ok = false;
        if (!_sent || _acknowledged || _requestId == null || source != _url || currentUrl != _url ||
            message == null || message.Length > ReviewLaunch.MaxOutput) return false;
        try
        {
            using var json = JsonDocument.Parse(message);
            var root = json.RootElement;
            if (root.ValueKind != JsonValueKind.Object || root.EnumerateObject().Count() != 4 ||
                !root.TryGetProperty("type", out var type) || type.ValueKind != JsonValueKind.String ||
                type.GetString() != "atas-ai-review.session" ||
                !root.TryGetProperty("version", out var version) || version.ValueKind != JsonValueKind.Number ||
                !version.TryGetInt32(out var v) || v != 1 ||
                !root.TryGetProperty("requestId", out var request) || request.ValueKind != JsonValueKind.String ||
                request.GetString() != _requestId || !root.TryGetProperty("ok", out var accepted) ||
                (accepted.ValueKind != JsonValueKind.True && accepted.ValueKind != JsonValueKind.False)) return false;
            ok = accepted.GetBoolean();
            _acknowledged = true;
            return true;
        }
        catch (JsonException) { return false; }
    }
}
#endif
