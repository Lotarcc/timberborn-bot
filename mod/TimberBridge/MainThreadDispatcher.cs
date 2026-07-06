using System;
using System.Collections.Concurrent;
using System.Threading.Tasks;
using Timberborn.SingletonSystem;
using UnityEngine;

namespace TimberBridge {

  // Marshals work onto the Unity main thread. Game state may only be touched on
  // the main thread, but the HTTP listener runs on a background thread — so the
  // listener enqueues a read lambda here and awaits the result. Bound as a
  // singleton; the engine calls UpdateSingleton() every frame (main thread).
  public class MainThreadDispatcher : IUpdatableSingleton {

    private readonly ConcurrentQueue<Action> _queue = new ConcurrentQueue<Action>();

    public void UpdateSingleton() {
      while (_queue.TryDequeue(out Action action)) {
        try {
          action();
        } catch (Exception e) {
          Debug.LogError("[TimberBridge] main-thread action error: " + e);
        }
      }
    }

    // Called from the listener thread. Returns a Task the caller waits on; the
    // lambda runs on the main thread in the next UpdateSingleton().
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

}
