namespace ATAS.AiBridge;

using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

/// <summary>
/// 极简 HTTP/1.1 服务端，基于 TcpListener。
///
/// 为什么不用 HttpListener：http.sys 要求非管理员进程先做 urlacl 端口预留
/// （netsh http add urlacl），否则绑端口直接 Network Access Denied —— 原版
/// ATAS MCP 的排障表里就有这一条。TcpListener 不经过 http.sys，免管理员、
/// 免预留，且只绑 127.0.0.1（回环），局域网不可达。
///
/// 只支持本桥需要的子集：请求行 + 头 + Content-Length 体；响应一律
/// Connection: close（每次请求一个连接，客户端是 Python urllib，够用且简单）。
/// </summary>
internal sealed class BridgeServer
{
    public delegate string RequestHandler(string method, string path, Dictionary<string, string> query, string body);

    private const int MaxRequestBytes = 1 << 20;

    private readonly RequestHandler _handler;
    private readonly Action<string> _log;
    private TcpListener _listener;
    private CancellationTokenSource _cts;

    public BridgeServer(RequestHandler handler, Action<string> log)
    {
        _handler = handler;
        _log = log;
    }

    public int Port { get; private set; }
    public bool IsListening
    {
        get
        {
            var listener = _listener;
            var cancellation = _cts;
            return listener != null && cancellation != null && !cancellation.IsCancellationRequested && listener.Server.IsBound;
        }
    }

    /// <summary>在 [firstPort, lastPort] 里找第一个可用端口并开始监听。</summary>
    public bool Start(int firstPort, int lastPort, out string error)
    {
        error = null;

        for (var port = firstPort; port <= lastPort; port++)
        {
            try
            {
                var listener = new TcpListener(IPAddress.Loopback, port);
                listener.Start();

                _listener = listener;
                Port = port;
                _cts = new CancellationTokenSource();
                _ = Task.Run(() => AcceptLoopAsync(_cts.Token));
                return true;
            }
            catch (SocketException)
            {
                // 端口占用，试下一个
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
        }

        error = $"no free port in range {firstPort}..{lastPort}";
        return false;
    }

    public void Stop()
    {
        try { _cts?.Cancel(); } catch { }
        try { _listener?.Stop(); } catch { }
        _listener = null;
        _cts = null;
    }

    private async Task AcceptLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            TcpClient client;
            try
            {
                client = await _listener.AcceptTcpClientAsync(ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException) { return; }
            catch (ObjectDisposedException) { return; }
            catch (Exception ex)
            {
                _log("accept error: " + ex.Message);
                continue;
            }

            _ = Task.Run(() => ServeAsync(client, ct), CancellationToken.None);
        }
    }

    private async Task ServeAsync(TcpClient client, CancellationToken ct)
    {
        using (client)
        {
            try
            {
                client.NoDelay = true;
                using var stream = client.GetStream();
                stream.ReadTimeout = 10000;

                var request = await ReadRequestAsync(stream, ct).ConfigureAwait(false);
                if (request == null)
                    return;

                var (headerPart, body) = request.Value;

                var lines = headerPart.Split(new[] { "\r\n" }, StringSplitOptions.None);
                if (lines.Length == 0)
                    return;

                var parts = lines[0].Split(' ');
                if (parts.Length < 2)
                    return;

                var method = parts[0].ToUpperInvariant();
                var target = parts[1];

                var query = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                var path = target;
                var qIndex = target.IndexOf('?');
                if (qIndex >= 0)
                {
                    path = target.Substring(0, qIndex);
                    foreach (var pair in target.Substring(qIndex + 1).Split('&'))
                    {
                        if (pair.Length == 0)
                            continue;
                        var eq = pair.IndexOf('=');
                        var key = eq < 0 ? pair : pair.Substring(0, eq);
                        var val = eq < 0 ? "" : pair.Substring(eq + 1);
                        var decodedKey = Uri.UnescapeDataString(key);
                        if (query.ContainsKey(decodedKey))
                        {
                            await WriteResponseAsync(stream, 400, "{\"ok\":false,\"error\":\"Duplicate query parameter.\"}", ct).ConfigureAwait(false);
                            return;
                        }
                        query[decodedKey] = Uri.UnescapeDataString(val.Replace("+", " "));
                    }
                }

                string json;
                var status = 200;
                try
                {
                    json = _handler(method, path, query, body) ?? "{}";
                }
                catch (Exception ex)
                {
                    status = 500;
                    json = "{\"ok\":false,\"error\":" + JsonString(ex.Message) + "}";
                }

                await WriteResponseAsync(stream, status, json, ct).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                _log("serve error: " + ex.Message);
            }
        }
    }

    private static async Task<(string HeaderPart, string Body)?> ReadRequestAsync(NetworkStream stream, CancellationToken ct)
    {
        var buffer = new byte[8192];
        var sb = new StringBuilder();
        var headerEnd = -1;

        while (headerEnd < 0)
        {
            var read = await stream.ReadAsync(buffer, 0, buffer.Length, ct).ConfigureAwait(false);
            if (read <= 0)
                return null;

            sb.Append(Encoding.UTF8.GetString(buffer, 0, read));

            if (sb.Length > MaxRequestBytes)
                throw new InvalidOperationException("request too large");

            headerEnd = sb.ToString().IndexOf("\r\n\r\n", StringComparison.Ordinal);
        }

        var all = sb.ToString();
        var headerPart = all.Substring(0, headerEnd);
        var body = all.Substring(headerEnd + 4);

        var contentLength = 0;
        foreach (var line in headerPart.Split(new[] { "\r\n" }, StringSplitOptions.None))
        {
            var colon = line.IndexOf(':');
            if (colon <= 0)
                continue;
            if (line.Substring(0, colon).Trim().Equals("Content-Length", StringComparison.OrdinalIgnoreCase))
                int.TryParse(line.Substring(colon + 1).Trim(), out contentLength);
        }

        while (Encoding.UTF8.GetByteCount(body) < contentLength)
        {
            var read = await stream.ReadAsync(buffer, 0, buffer.Length, ct).ConfigureAwait(false);
            if (read <= 0)
                break;
            body += Encoding.UTF8.GetString(buffer, 0, read);
        }

        return (headerPart, body);
    }

    private static async Task WriteResponseAsync(NetworkStream stream, int status, string json, CancellationToken ct)
    {
        var payload = Encoding.UTF8.GetBytes(json);
        var head = new StringBuilder()
            .Append("HTTP/1.1 ").Append(status).Append(status == 200 ? " OK" : " Error").Append("\r\n")
            .Append("Content-Type: application/json; charset=utf-8\r\n")
            .Append("Content-Length: ").Append(payload.Length).Append("\r\n")
            .Append("Cache-Control: no-store\r\n")
            .Append("Connection: close\r\n\r\n")
            .ToString();

        await stream.WriteAsync(Encoding.ASCII.GetBytes(head), 0, head.Length, ct).ConfigureAwait(false);
        await stream.WriteAsync(payload, 0, payload.Length, ct).ConfigureAwait(false);
        await stream.FlushAsync(ct).ConfigureAwait(false);
    }

    private static string JsonString(string value)
    {
        var sb = new StringBuilder("\"");
        foreach (var c in value ?? "")
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < ' ')
                        sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else
                        sb.Append(c);
                    break;
            }
        }
        return sb.Append('"').ToString();
    }
}
