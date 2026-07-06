using System;
using Timberborn.CameraSystem;
using UnityEngine;

namespace TimberBridge {

  // Captures the current game view as a downscaled PNG, in-process. CapturePng
  // MUST run on the Unity main thread at end-of-frame (drained from BridgePump's
  // LateUpdate), or the backbuffer is only partially rendered.
  //
  // Approach: ScreenCapture.CaptureScreenshotAsTexture() grabs the actual
  // composited backbuffer (what the player sees), then Graphics.Blit downscales
  // to <= maxWidth and ImageConversion.EncodeToPNG() encodes it. CameraService is
  // injected but only needed if we later switch to on-demand camera rendering.
  public class ScreenshotReader {

    private readonly CameraService _camera;

    public ScreenshotReader(CameraService camera) {
      _camera = camera;
    }

    public byte[] CapturePng(int maxWidth) {
      if (maxWidth <= 0) maxWidth = 640;
      Texture2D full = ScreenCapture.CaptureScreenshotAsTexture();
      try {
        return DownscaleAndEncode(full, maxWidth);
      } finally {
        if (full != null) UnityEngine.Object.Destroy(full);
      }
    }

    private static byte[] DownscaleAndEncode(Texture2D source, int maxWidth) {
      if (source == null) return null;
      int sw = source.width;
      int sh = source.height;
      ComputeTargetSize(sw, sh, maxWidth, out int dw, out int dh);

      if (dw == sw && dh == sh) {
        return source.EncodeToPNG();
      }

      RenderTexture rt = RenderTexture.GetTemporary(dw, dh, 0, RenderTextureFormat.ARGB32);
      RenderTexture prevActive = RenderTexture.active;
      Texture2D resized = null;
      try {
        Graphics.Blit(source, rt);
        RenderTexture.active = rt;
        resized = new Texture2D(dw, dh, TextureFormat.RGBA32, false);
        resized.ReadPixels(new Rect(0, 0, dw, dh), 0, 0);
        resized.Apply();
        return resized.EncodeToPNG();
      } finally {
        RenderTexture.active = prevActive;
        RenderTexture.ReleaseTemporary(rt);
        if (resized != null) UnityEngine.Object.Destroy(resized);
      }
    }

    // Preserve aspect ratio; clamp width to maxWidth; never upscale.
    private static void ComputeTargetSize(int sw, int sh, int maxWidth, out int dw, out int dh) {
      if (sw <= maxWidth) { dw = sw; dh = sh; return; }
      dw = maxWidth;
      dh = Mathf.Max(1, Mathf.RoundToInt(sh * (maxWidth / (float)sw)));
    }

  }

}
