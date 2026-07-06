using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
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

    public void Load() {
      _gameVersion = ReadGameVersion();
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
      int guard = 0;
      while (guard++ < 200 && (headerLine = ReadLine(stream)) != null) {
        if (requestLine == null) requestLine = headerLine;
        if (headerLine.Length == 0) break; // blank line ends the headers
      }
      string path = ParsePath(requestLine);

      int status;
      string statusText;
      string json;
      if (path == "/ping") {
        status = 200;
        statusText = "OK";
        json = "{\"ok\":true,\"bridge_version\":\"" + BridgeVersion
             + "\",\"game_version\":\"" + Escape(_gameVersion)
             + "\",\"in_game\":true}";
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
