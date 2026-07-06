using System;
using System.Collections.Concurrent;
using System.Threading.Tasks;
using UnityEngine;

namespace TimberBridge {

  // Marshals reads onto the Unity main thread. Driven by a MonoBehaviour pump
  // (BridgePump) whose Unity Update() runs every frame — NOT by IUpdatableSingleton,
  // which depends on singleton-collection timing that proved unreliable (the
  // dispatcher is instantiated lazily and can miss the one-time collection).
  public class MainThreadDispatcher {

    private readonly ConcurrentQueue<Action> _queue = new ConcurrentQueue<Action>();

    // Called every frame on the Unity main thread by BridgePump.
    public void Drain() {
      while (_queue.TryDequeue(out Action action)) {
        try {
          action();
        } catch (Exception e) {
          Debug.LogError("[TimberBridge] main-thread action error: " + e);
        }
      }
    }

    // Called from the listener thread; returns a Task the caller waits on.
    public Task<T> EnqueueRead<T>(Func<T> read) {
      var tcs = new TaskCompletionSource<T>();
      _queue.Enqueue(() => {
        try {
          tcs.SetResult(read());
        } catch (Exception e) {
          tcs.SetException(e);
        }
      });
      return tcs.Task;
    }

  }

  // A plain Unity component. Its Update() is invoked directly by Unity every
  // frame (regardless of game pause or singleton wiring), so the dispatcher
  // queue always drains. Created on a GameObject in BridgeHttpServer.Load().
  public class BridgePump : MonoBehaviour {

    public MainThreadDispatcher Dispatcher;
    // A separate queue drained at end-of-frame. Screenshot capture
    // (ScreenCapture.CaptureScreenshotAsTexture) must run in LateUpdate, after the
    // frame is fully composited, or it grabs a partially-rendered backbuffer.
    public MainThreadDispatcher LateDispatcher;

    private void Update() {
      Dispatcher?.Drain();
    }

    private void LateUpdate() {
      LateDispatcher?.Drain();
    }

  }

}
