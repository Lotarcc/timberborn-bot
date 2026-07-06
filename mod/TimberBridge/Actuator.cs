using System;
using System.Globalization;
using Newtonsoft.Json.Linq;
using Timberborn.TimeSystem;
using UnityEngine;

namespace TimberBridge {

  // Executes /act commands. Every call runs on the Unity main thread (via
  // MainThreadDispatcher), so touching game services is safe. Returns a JSON
  // string result. Placement / save / designate commands are added next.
  public class Actuator {

    private readonly SpeedManager _speed;

    public Actuator(SpeedManager speed) {
      _speed = speed;
    }

    public string Act(string command, JObject args) {
      try {
        switch (command) {
          case "set_speed":
            return SetSpeed(GetFloat(args, "speed", 1f));
          case "pause":
            return SetSpeed(0f);
          default:
            return "{\"ok\":false,\"error\":\"not_implemented\",\"command\":\"" + Escape(command) + "\"}";
        }
      } catch (Exception e) {
        Debug.LogError("[TimberBridge] act '" + command + "' failed: " + e);
        return "{\"ok\":false,\"error\":\"exception\",\"command\":\"" + Escape(command) + "\"}";
      }
    }

    private string SetSpeed(float speed) {
      _speed.ChangeSpeed(speed);
      return "{\"ok\":true,\"applied\":{\"command\":\"set_speed\",\"speed\":"
           + speed.ToString(CultureInfo.InvariantCulture) + "}}";
    }

    private static float GetFloat(JObject args, string key, float fallback) {
      JToken t = args?[key];
      return t != null ? t.ToObject<float>() : fallback;
    }

    private static string Escape(string s) {
      return s == null ? "" : s.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }

  }

}
