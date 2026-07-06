using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
using Timberborn.SingletonSystem;
using UnityEngine;

namespace TimberBridge {

  // Phase 0 spike: a localhost server that answers /ping.
  // Uses a raw TcpListener (not HttpListener) so it needs no admin rights or
  // HTTP.sys URL-ACL reservation — the game process runs unelevated.
  // /ping needs no game state, so it is served from values cached on the main
  // thread in Load(). Phase 1 adds main-thread marshalling for /state.
  public class BridgeHttpServer : ILoadableSingleton, IUnloadableSingleton {

    private const int Port = 7744;
    private const string BridgeVersion = "0.1.0";

    private TcpListener _listener;
    private Thread _thread;
    private volatile bool _running;
    private string _gameVersion = "unknown";
    private GameObject _pumpGo;

    private readonly MainThreadDispatcher _dispatcher;
    // Drained at end-of-frame (LateUpdate) for screenshot capture only.
    private readonly MainThreadDispatcher _lateDispatcher = new MainThreadDispatcher();
    private readonly StateReader _stateReader;
    private readonly Actuator _actuator;
    private readonly BlueprintsReader _blueprints;
    private readonly MapReader _map;
    private readonly ResourcesReader _resources;
    private readonly ScreenshotReader _screenshot;

    public BridgeHttpServer(MainThreadDispatcher dispatcher, StateReader stateReader,
                            Actuator actuator, BlueprintsReader blueprints, MapReader map,
                            ResourcesReader resources, ScreenshotReader screenshot) {
      _dispatcher = dispatcher;
      _stateReader = stateReader;
      _actuator = actuator;
      _blueprints = blueprints;
      _map = map;
      _resources = resources;
      _screenshot = screenshot;
    }

    public void Load() {
      _gameVersion = ReadGameVersion();
      // Keep Unity ticking when the game window loses focus — otherwise the
      // main-thread queue never drains and every endpoint returns
      // main_thread_timeout while the operator watches YouTube.
      Application.runInBackground = true;
      // Drive the main-thread queue from a Unity component (reliable per-frame Update).
      _pumpGo = new GameObject("TimberBridgePump");
      var pump = _pumpGo.AddComponent<BridgePump>();
      pump.Dispatcher = _dispatcher;
      pump.LateDispatcher = _lateDispatcher;
      try {
        _listener = new TcpListener(IPAddress.Loopback, Port);
        _listener.Start();
        _running = true;
        _thread = new Thread(AcceptLoop) { IsBackground = true, Name = "TimberBridge" };
        _thread.Start();
        Debug.Log("[TimberBridge] listening on http://127.0.0.1:" + Port + "/ (game " + _gameVersion + ")");
      } catch (Exception e) {
        Debug.LogError("[TimberBridge] failed to start listener: " + e);
      }
    }

    public void Unload() {
      _running = false;
      try { _listener?.Stop(); } catch { /* already torn down */ }
      if (_pumpGo != null) {
        UnityEngine.Object.Destroy(_pumpGo);
        _pumpGo = null;
      }
      Debug.Log("[TimberBridge] stopped");
    }

    private void AcceptLoop() {
      while (_running) {
        TcpClient client;
        try {
          client = _listener.AcceptTcpClient();
        } catch {
          break; // listener stopped during Unload()
        }
        try {
          using (client) {
            HandleClient(client);
          }
        } catch (Exception e) {
          Debug.LogError("[TimberBridge] handler error: " + e);
        }
      }
    }

    private void HandleClient(TcpClient client) {
      client.NoDelay = true;
      client.ReceiveTimeout = 2000;
      client.SendTimeout = 2000;
      NetworkStream stream = client.GetStream();

      // Drain the whole request head (request line + headers up to the blank line)
      // so closing doesn't RST the client — some HTTP clients report that as an error.
      string requestLine = null;
      string headerLine;
      int contentLength = 0;
      int guard = 0;
      while (guard++ < 200 && (headerLine = ReadLine(stream)) != null) {
        if (requestLine == null) {
          requestLine = headerLine;
        } else if (headerLine.Length == 0) {
          break; // blank line ends the headers
        } else {
          int ci = headerLine.IndexOf(':');
          if (ci > 0 && headerLine.Substring(0, ci).Trim()
                .Equals("Content-Length", StringComparison.OrdinalIgnoreCase)) {
            int.TryParse(headerLine.Substring(ci + 1).Trim(), out contentLength);
          }
        }
      }
      string reqBody = ReadBody(stream, contentLength);
      string path = ParsePath(requestLine);

      // Screenshot is a binary (image/png) response, handled before the JSON paths.
      if (path == "/screenshot") {
        HandleScreenshot(stream, client, requestLine);
        return;
      }

      int status;
      string statusText;
      string json;
      if (path == "/ping") {
        status = 200;
        statusText = "OK";
        json = "{\"ok\":true,\"bridge_version\":\"" + BridgeVersion
             + "\",\"game_version\":\"" + Escape(_gameVersion)
             + "\",\"in_game\":true}";
      } else if (path == "/state") {
        try {
          // Read game state on the main thread; block this listener thread until it completes.
          Task<string> read = _dispatcher.EnqueueRead(() => _stateReader.ReadStateJson());
          if (read.Wait(3000)) {
            status = 200;
            statusText = "OK";
            json = read.Result;
          } else {
            status = 503;
            statusText = "Service Unavailable";
            json = "{\"ok\":false,\"error\":\"main_thread_timeout\"}";
          }
        } catch (Exception e) {
          status = 500;
          statusText = "Internal Server Error";
          json = "{\"ok\":false,\"error\":\"read_failed\"}";
          Debug.LogError("[TimberBridge] /state error: " + e);
        }
      } else if (path == "/map") {
        try {
          Task<string> read = _dispatcher.EnqueueRead(() => _map.ReadJson());
          if (read.Wait(4000)) { status = 200; statusText = "OK"; json = read.Result; }
          else { status = 503; statusText = "Service Unavailable"; json = "{\"ok\":false,\"error\":\"main_thread_timeout\"}"; }
        } catch (Exception e) {
          status = 500; statusText = "Internal Server Error";
          json = "{\"ok\":false,\"error\":\"read_failed\"}";
          Debug.LogError("[TimberBridge] /map error: " + e);
        }
      } else if (path == "/resources") {
        try {
          Task<string> read = _dispatcher.EnqueueRead(() => _resources.ReadJson());
          if (read.Wait(4000)) { status = 200; statusText = "OK"; json = read.Result; }
          else { status = 503; statusText = "Service Unavailable"; json = "{\"ok\":false,\"error\":\"main_thread_timeout\"}"; }
        } catch (Exception e) {
          status = 500; statusText = "Internal Server Error";
          json = "{\"ok\":false,\"error\":\"read_failed\"}";
          Debug.LogError("[TimberBridge] /resources error: " + e);
        }
      } else if (path == "/blueprints") {
        try {
          Task<string> read = _dispatcher.EnqueueRead(() => _blueprints.ReadJson());
          if (read.Wait(3000)) { status = 200; statusText = "OK"; json = read.Result; }
          else { status = 503; statusText = "Service Unavailable"; json = "{\"ok\":false,\"error\":\"main_thread_timeout\"}"; }
        } catch (Exception e) {
          status = 500; statusText = "Internal Server Error";
          json = "{\"ok\":false,\"error\":\"read_failed\"}";
          Debug.LogError("[TimberBridge] /blueprints error: " + e);
        }
      } else if (path == "/act") {
        try {
          JObject req = string.IsNullOrEmpty(reqBody) ? new JObject() : JObject.Parse(reqBody);
          string command = (string)req["command"] ?? "";
          JObject actArgs = req["args"] as JObject;
          Task<string> read = _dispatcher.EnqueueRead(() => _actuator.Act(command, actArgs));
          if (read.Wait(3000)) {
            status = 200;
            statusText = "OK";
            json = read.Result;
          } else {
            status = 503;
            statusText = "Service Unavailable";
            json = "{\"ok\":false,\"error\":\"main_thread_timeout\"}";
          }
        } catch (Exception e) {
          status = 400;
          statusText = "Bad Request";
          json = "{\"ok\":false,\"error\":\"bad_request\"}";
          Debug.LogError("[TimberBridge] /act error: " + e);
        }
      } else {
        status = 404;
        statusText = "Not Found";
        json = "{\"ok\":false,\"error\":\"not_found\",\"path\":\"" + Escape(path) + "\"}";
      }

      byte[] body = Encoding.UTF8.GetBytes(json);
      string headers = "HTTP/1.1 " + status + " " + statusText + "\r\n"
                     + "Content-Type: application/json\r\n"
                     + "Content-Length: " + body.Length + "\r\n"
                     + "Connection: close\r\n\r\n";
      byte[] head = Encoding.ASCII.GetBytes(headers);
      stream.Write(head, 0, head.Length);
      stream.Write(body, 0, body.Length);
      stream.Flush();
      try { client.Client.Shutdown(SocketShutdown.Both); } catch { /* client already gone */ }
    }

    // Capture the game view on the Unity main thread (end-of-frame) and stream it
    // back as a binary PNG. On failure, returns a small JSON error instead.
    private void HandleScreenshot(NetworkStream stream, TcpClient client, string requestLine) {
      int maxWidth = ParseIntQuery(requestLine, "w", 768);
      byte[] png = null;
      string error = null;
      try {
        Task<byte[]> shot = _lateDispatcher.EnqueueRead(() => _screenshot.CapturePng(maxWidth));
        if (shot.Wait(5000)) { png = shot.Result; }
        else { error = "main_thread_timeout"; }
      } catch (Exception e) {
        error = "capture_failed";
        Debug.LogError("[TimberBridge] /screenshot error: " + e);
      }

      if (png == null || png.Length == 0) {
        string json = "{\"ok\":false,\"error\":\"" + Escape(error ?? "empty_capture") + "\"}";
        byte[] body = Encoding.UTF8.GetBytes(json);
        string h = "HTTP/1.1 503 Service Unavailable\r\n"
                 + "Content-Type: application/json\r\n"
                 + "Content-Length: " + body.Length + "\r\n"
                 + "Connection: close\r\n\r\n";
        byte[] head = Encoding.ASCII.GetBytes(h);
        stream.Write(head, 0, head.Length);
        stream.Write(body, 0, body.Length);
        stream.Flush();
        try { client.Client.Shutdown(SocketShutdown.Both); } catch { }
        return;
      }

      string headers = "HTTP/1.1 200 OK\r\n"
                     + "Content-Type: image/png\r\n"
                     + "Content-Length: " + png.Length + "\r\n"
                     + "Connection: close\r\n\r\n";
      byte[] headerBytes = Encoding.ASCII.GetBytes(headers);
      stream.Write(headerBytes, 0, headerBytes.Length);
      stream.Write(png, 0, png.Length);
      stream.Flush();
      try { client.Client.Shutdown(SocketShutdown.Both); } catch { /* client already gone */ }
    }

    // Parse an integer query param (e.g. "?w=640") from the request line; default on absence.
    private static int ParseIntQuery(string requestLine, string key, int fallback) {
      if (string.IsNullOrEmpty(requestLine)) return fallback;
      int q = requestLine.IndexOf('?');
      if (q < 0) return fallback;
      int end = requestLine.IndexOf(' ', q);
      string query = end > q ? requestLine.Substring(q + 1, end - q - 1) : requestLine.Substring(q + 1);
      foreach (string pair in query.Split('&')) {
        int eq = pair.IndexOf('=');
        if (eq > 0 && pair.Substring(0, eq) == key) {
          if (int.TryParse(pair.Substring(eq + 1), out int v)) return v;
        }
      }
      return fallback;
    }

    // Read one CRLF-terminated line. Returns "" for a blank line (end of headers),
    // null at end of stream with nothing buffered.
    private static string ReadLine(NetworkStream stream) {
      var sb = new StringBuilder();
      int guard = 0;
      int b;
      while (guard++ < 16384 && (b = stream.ReadByte()) != -1) {
        if (b == '\n') return sb.ToString();
        if (b != '\r') sb.Append((char)b);
      }
      return sb.Length > 0 ? sb.ToString() : null;
    }

    private static string ReadBody(NetworkStream stream, int contentLength) {
      if (contentLength <= 0) {
        return "";
      }
      byte[] buf = new byte[contentLength];
      int read = 0;
      while (read < contentLength) {
        int n;
        try { n = stream.Read(buf, read, contentLength - read); } catch { break; }
        if (n <= 0) break;
        read += n;
      }
      return Encoding.UTF8.GetString(buf, 0, read);
    }

    private static string ParsePath(string requestLine) {
      if (string.IsNullOrEmpty(requestLine)) return "";
      string[] parts = requestLine.Split(' ');
      if (parts.Length < 2) return "";
      string target = parts[1];
      int q = target.IndexOf('?');
      return q >= 0 ? target.Substring(0, q) : target;
    }

    // Prefer the exact version from VersionNumbers.json ("1.0.13.1"); fall back
    // to Unity's Application.version. File IO is fine here — Load() is main-thread.
    private static string ReadGameVersion() {
      try {
        string path = Path.Combine(Application.streamingAssetsPath, "VersionNumbers.json");
        if (File.Exists(path)) {
          string v = ExtractJsonString(File.ReadAllText(path), "CurrentVersion");
          if (!string.IsNullOrEmpty(v)) {
            return v;
          }
        }
      } catch { /* fall through */ }
      return Application.version;
    }

    // Minimal, dependency-free extractor for "key":"value" out of small JSON.
    private static string ExtractJsonString(string json, string key) {
      int k = json.IndexOf("\"" + key + "\"", StringComparison.Ordinal);
      if (k < 0) return null;
      int colon = json.IndexOf(':', k);
      if (colon < 0) return null;
      int firstQuote = json.IndexOf('"', colon);
      if (firstQuote < 0) return null;
      int secondQuote = json.IndexOf('"', firstQuote + 1);
      if (secondQuote < 0) return null;
      return json.Substring(firstQuote + 1, secondQuote - firstQuote - 1);
    }

    private static string Escape(string s) {
      return s == null ? "" : s.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }

  }

}
