using System;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using Timberborn.SingletonSystem;
using UnityEngine;

namespace TimberBridge {

  // Phase 0 spike: a localhost HTTP server that answers /ping.
  // Runs the listener on a background thread; /ping needs no game state, so it
  // is served directly from cached values captured on the main thread in Load().
  // Phase 1 adds main-thread request marshalling (IUpdatableSingleton) for /state.
  public class BridgeHttpServer : ILoadableSingleton, IUnloadableSingleton {

    private const int Port = 7744;
    private const string BridgeVersion = "0.1.0";

    private HttpListener _listener;
    private Thread _thread;
    private volatile bool _running;
    private string _gameVersion = "unknown";

    public void Load() {
      _gameVersion = ReadGameVersion();
      try {
        _listener = new HttpListener();
        _listener.Prefixes.Add("http://127.0.0.1:" + Port + "/");
        _listener.Start();
        _running = true;
        _thread = new Thread(ListenLoop) { IsBackground = true, Name = "TimberBridge" };
        _thread.Start();
        Debug.Log("[TimberBridge] listening on http://127.0.0.1:" + Port + "/ (game " + _gameVersion + ")");
      } catch (Exception e) {
        Debug.LogError("[TimberBridge] failed to start listener: " + e);
      }
    }

    public void Unload() {
      _running = false;
      try {
        _listener?.Stop();
        _listener?.Close();
      } catch { /* already torn down */ }
      Debug.Log("[TimberBridge] stopped");
    }

    private void ListenLoop() {
      while (_running) {
        HttpListenerContext ctx;
        try {
          ctx = _listener.GetContext();
        } catch {
          break; // listener stopped during Unload()
        }
        try {
          Handle(ctx);
        } catch (Exception e) {
          Debug.LogError("[TimberBridge] handler error: " + e);
        }
      }
    }

    private void Handle(HttpListenerContext ctx) {
      string path = ctx.Request.Url.AbsolutePath;
      int status;
      string json;
      if (path == "/ping") {
        status = 200;
        json = "{\"ok\":true,\"bridge_version\":\"" + BridgeVersion
             + "\",\"game_version\":\"" + Escape(_gameVersion)
             + "\",\"in_game\":true}";
      } else {
        status = 404;
        json = "{\"ok\":false,\"error\":\"not_found\",\"path\":\"" + Escape(path) + "\"}";
      }
      byte[] bytes = Encoding.UTF8.GetBytes(json);
      ctx.Response.StatusCode = status;
      ctx.Response.ContentType = "application/json";
      ctx.Response.ContentLength64 = bytes.Length;
      ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
      ctx.Response.OutputStream.Close();
    }

    // Prefer the exact version from VersionNumbers.json (e.g. "1.0.13.1");
    // fall back to Unity's Application.version. File IO here is fine: Load()
    // runs on the main thread during scene init.
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
